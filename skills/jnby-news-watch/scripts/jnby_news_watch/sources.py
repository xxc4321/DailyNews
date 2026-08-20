from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
from html.parser import HTMLParser
import io
import json
import os
import time
from typing import Callable, Literal, Protocol, TypeVar
from urllib.error import HTTPError
from urllib.parse import quote_plus, urljoin

import feedparser

from .security import SafeFetcher


Channel = Literal["news", "review", "discovery"]
T = TypeVar("T")


@dataclass(frozen=True)
class Query:
    text: str
    language: str = "en"


@dataclass(frozen=True)
class TimeWindow:
    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("time window must be timezone-aware")
        if self.since > self.until:
            raise ValueError("time window is reversed")


@dataclass(frozen=True)
class RawCandidate:
    id: str
    title: str
    original_url: str
    published_at: datetime
    source_id: str
    source_name: str
    source_tier: str
    channel: Channel
    language: str = "en"
    summary: str = ""
    verified: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RawReview:
    id: str
    platform: str
    original_url: str
    published_at: datetime
    author_key: str
    text: str
    language: str = "en"
    source_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    status: Literal["ok", "empty", "failed", "disabled"]
    latency_ms: int = 0
    attempts: int = 1
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class SourceResult:
    source_id: str
    ok: bool
    candidates: tuple[RawCandidate, ...] = ()
    reviews: tuple[RawReview, ...] = ()
    health: SourceHealth | None = None
    error: str = ""


@dataclass(frozen=True)
class CollectionBatch:
    results: tuple[SourceResult, ...]

    @property
    def candidates(self) -> tuple[RawCandidate, ...]:
        return tuple(item for result in self.results for item in result.candidates)

    @property
    def reviews(self) -> tuple[RawReview, ...]:
        return tuple(item for result in self.results for item in result.reviews)


class SourceAdapter(Protocol):
    name: str
    channel: Channel

    def collect(
        self, source: dict, queries: list[Query], time_window: TimeWindow
    ) -> SourceResult: ...


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, HTTPError) and (exc.code == 429 or exc.code >= 500)


def with_retry(
    operation: Callable[[], T],
    *,
    retries: int = 2,
    delay_seconds: float = 0.25,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= retries or not _retryable(exc):
                raise
            retry_after = 0.0
            if isinstance(exc, HTTPError) and exc.headers:
                try:
                    retry_after = float(exc.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    retry_after = 0.0
            sleeper(max(retry_after, delay_seconds * (2**attempt)))
    raise AssertionError("unreachable")


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        parsed = feedparser._parse_date(value)
        if parsed is None:
            return datetime.now(timezone.utc)
        result = datetime(*parsed[:6], tzinfo=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _feed_datetime(entry: dict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return _parse_datetime(entry.get("published") or entry.get("updated"))


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]


class RssAdapter:
    name = "rss"
    channel: Channel = "news"

    def __init__(self, fetcher: SafeFetcher, *, retries: int = 2):
        self.fetcher = fetcher
        self.retries = retries

    def _feed_url(self, source: dict, query: Query | None) -> str:
        return source["url"]

    def collect(
        self, source: dict, queries: list[Query], time_window: TimeWindow
    ) -> SourceResult:
        started = time.perf_counter()
        attempts = 0

        def fetch(url: str):
            nonlocal attempts

            def once():
                nonlocal attempts
                attempts += 1
                return self.fetcher.fetch(url)

            return with_retry(once, retries=self.retries)

        try:
            query_list: list[Query | None] = queries or [None]
            candidates: list[RawCandidate] = []
            seen: set[str] = set()
            for query in query_list:
                result = fetch(self._feed_url(source, query))
                feed = feedparser.parse(result.body)
                if getattr(feed, "bozo", False) and not feed.entries:
                    raise ValueError("invalid RSS/Atom payload")
                for entry in feed.entries:
                    url = entry.get("link", "").strip()
                    title = entry.get("title", "").strip()
                    if not url or not title or url in seen:
                        continue
                    published_at = _feed_datetime(entry)
                    if not time_window.since <= published_at <= time_window.until:
                        continue
                    seen.add(url)
                    tier = source.get("tier", "S1")
                    channel: Channel = source.get("channel", self.channel)
                    candidates.append(
                        RawCandidate(
                            id=_stable_id(source["id"], entry.get("id", url)),
                            title=title,
                            original_url=url,
                            published_at=published_at,
                            source_id=source["id"],
                            source_name=source.get("name", source["id"]),
                            source_tier=tier,
                            channel=channel,
                            language=(query.language if query else source.get("language", "en")),
                            summary=entry.get("summary", ""),
                            verified=tier in {"S0", "S1"} and channel != "discovery",
                            metadata={"feed_url": result.url},
                        )
                    )
            latency = int((time.perf_counter() - started) * 1000)
            status = "ok" if candidates else "empty"
            health = SourceHealth(source["id"], status, latency, max(attempts, 1))
            return SourceResult(
                source_id=source["id"],
                ok=True,
                candidates=tuple(candidates),
                health=health,
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            health = SourceHealth(
                source["id"],
                "failed",
                latency,
                max(attempts, 1),
                type(exc).__name__,
                str(exc),
            )
            return SourceResult(source["id"], False, health=health, error=str(exc))


class GoogleNewsRssAdapter(RssAdapter):
    name = "google_news_rss"
    channel: Channel = "discovery"

    def _feed_url(self, source: dict, query: Query | None) -> str:
        if query is None:
            raise ValueError("Google News discovery requires a query")
        language = query.language.lower()
        locale = {
            "zh": ("zh-CN", "CN", "CN:zh-Hans"),
            "fr": ("fr", "FR", "FR:fr"),
            "it": ("it", "IT", "IT:it"),
        }.get(language, ("en-US", "US", "US:en"))
        return (
            "https://news.google.com/rss/search?q="
            f"{quote_plus(query.text)}&hl={locale[0]}&gl={locale[1]}&ceid={locale[2]}"
        )


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data: str):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = ""
            self._text = []


class PageLinksAdapter:
    name = "page_links"
    channel: Channel = "news"

    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        started = time.perf_counter()
        try:
            result = with_retry(lambda: self.fetcher.fetch(source["url"]))
            parser = _LinkParser()
            parser.feed(result.body.decode("utf-8", errors="replace"))
            candidates = tuple(
                RawCandidate(
                    id=_stable_id(source["id"], urljoin(result.url, href)),
                    title=title,
                    original_url=urljoin(result.url, href),
                    published_at=time_window.until,
                    source_id=source["id"],
                    source_name=source.get("name", source["id"]),
                    source_tier=source.get("tier", "S0"),
                    channel="news",
                    language=(source.get("languages") or ["en"])[0],
                    verified=True,
                    metadata={"listing_url": result.url, "date_inferred": True},
                )
                for href, title in parser.links
                if title and href and not href.lower().startswith(("javascript:", "mailto:"))
            )
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"],
                True,
                candidates=candidates,
                health=SourceHealth(source["id"], "ok" if candidates else "empty", latency),
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"],
                False,
                health=SourceHealth(source["id"], "failed", latency, error_code=type(exc).__name__, message=str(exc)),
                error=str(exc),
            )


class GdeltAdapter:
    name = "gdelt"
    channel: Channel = "discovery"

    def __init__(self, fetcher: SafeFetcher):
        self.fetcher = fetcher

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        started = time.perf_counter()
        candidates: list[RawCandidate] = []
        attempts = 0
        try:
            for query in queries:
                url = (
                    "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json&maxrecords=50&query="
                    + quote_plus(query.text)
                )
                attempts += 1
                response = with_retry(lambda url=url: self.fetcher.fetch(url))
                payload = json.loads(response.body)
                for article in payload.get("articles", []):
                    original_url = article.get("url", "")
                    title = article.get("title", "")
                    if not original_url or not title:
                        continue
                    published_at = _parse_datetime(article.get("seendate"))
                    if time_window.since <= published_at <= time_window.until:
                        candidates.append(
                            RawCandidate(
                                id=_stable_id(source["id"], original_url),
                                title=title,
                                original_url=original_url,
                                published_at=published_at,
                                source_id=source["id"],
                                source_name=source.get("name", source["id"]),
                                source_tier="S2",
                                channel="discovery",
                                language=article.get("language", query.language),
                                verified=False,
                                metadata={"domain": article.get("domain", "")},
                            )
                        )
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], True, candidates=tuple(candidates),
                health=SourceHealth(source["id"], "ok" if candidates else "empty", latency, max(attempts, 1))
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "failed", latency, max(attempts, 1), type(exc).__name__, str(exc)),
                error=str(exc),
            )


class JsonReviewAdapter:
    name = "json_review"
    channel: Channel = "review"

    def __init__(self, fetcher: SafeFetcher, *, author_salt: str):
        self.fetcher = fetcher
        self.author_salt = author_salt

    def _author_key(self, value: str) -> str:
        return hashlib.sha256(f"{self.author_salt}:{value}".encode()).hexdigest()[:16]

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        if not source.get("approved", False):
            message = "review source must be explicitly approved"
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "disabled", error_code="NotApproved", message=message),
                error=message,
            )
        started = time.perf_counter()
        try:
            response = with_retry(lambda: self.fetcher.fetch(source["url"]))
            payload = json.loads(response.body)
            reviews: list[RawReview] = []
            for item in payload.get("items", []):
                published_at = _parse_datetime(item.get("published_at"))
                if not time_window.since <= published_at <= time_window.until:
                    continue
                url = str(item.get("url", "")).strip()
                text = str(item.get("text", "")).strip()
                if not url or not text:
                    continue
                reviews.append(
                    RawReview(
                        id=str(item.get("id") or _stable_id(source["id"], url, text)),
                        platform=str(item.get("platform") or source.get("name", source["id"])),
                        original_url=url,
                        published_at=published_at,
                        author_key=self._author_key(str(item.get("author", "anonymous"))),
                        text=text,
                        language=str(item.get("language", "en")),
                        source_id=source["id"],
                    )
                )
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], True, reviews=tuple(reviews),
                health=SourceHealth(source["id"], "ok" if reviews else "empty", latency),
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "failed", latency, error_code=type(exc).__name__, message=str(exc)),
                error=str(exc),
            )


class CsvReviewAdapter(JsonReviewAdapter):
    name = "csv_review"

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        if not source.get("approved", False):
            return super().collect(source, queries, time_window)
        started = time.perf_counter()
        try:
            response = with_retry(lambda: self.fetcher.fetch(source["url"]))
            rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
            payload = json.dumps({"items": rows}).encode()
            original_fetcher = self.fetcher

            class _PayloadFetcher:
                def fetch(self, url: str):
                    from .security import FetchResult
                    return FetchResult(url, payload, {"content-type": "application/json"}, 200)

            self.fetcher = _PayloadFetcher()  # type: ignore[assignment]
            try:
                return super().collect(source, queries, time_window)
            finally:
                self.fetcher = original_fetcher
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "failed", latency, error_code=type(exc).__name__, message=str(exc)),
                error=str(exc),
            )


class DisabledUnlessConfiguredAdapter:
    channel: Channel = "discovery"
    env_key = ""
    name = "optional"

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        if not os.environ.get(self.env_key):
            message = f"{self.env_key} is not configured"
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "disabled", error_code="NotConfigured", message=message),
                error=message,
            )
        message = f"{self.name} transport must be supplied by the host workflow"
        return SourceResult(
            source["id"], False,
            health=SourceHealth(source["id"], "disabled", error_code="TransportRequired", message=message),
            error=message,
        )


class TavilyAdapter(DisabledUnlessConfiguredAdapter):
    name = "tavily"
    env_key = "TAVILY_API_KEY"


class BlueskyPublicAdapter(DisabledUnlessConfiguredAdapter):
    name = "bluesky_public"
    env_key = "BLUESKY_PUBLIC_ENABLED"


class YouTubeApiAdapter(DisabledUnlessConfiguredAdapter):
    name = "youtube_api"
    env_key = "YOUTUBE_API_KEY"


class RedditOAuthAdapter(DisabledUnlessConfiguredAdapter):
    name = "reddit_oauth"
    env_key = "REDDIT_CLIENT_ID"


class PublicPostDiscoveryAdapter(RssAdapter):
    name = "public_post_discovery"
    channel: Channel = "discovery"


def collect_all(
    *,
    sources: list[dict],
    adapters: dict[str, SourceAdapter],
    queries: list[Query],
    time_window: TimeWindow,
) -> CollectionBatch:
    results: list[SourceResult] = []
    for source in sources:
        adapter_name = source.get("adapter", "")
        adapter = adapters.get(adapter_name)
        if adapter is None:
            message = f"adapter not registered: {adapter_name}"
            results.append(
                SourceResult(
                    source.get("id", adapter_name or "unknown"),
                    False,
                    health=SourceHealth(
                        source.get("id", "unknown"),
                        "disabled",
                        error_code="MissingAdapter",
                        message=message,
                    ),
                    error=message,
                )
            )
            continue
        try:
            results.append(adapter.collect(source, queries, time_window))
        except Exception as exc:
            source_id = source.get("id", adapter_name)
            results.append(
                SourceResult(
                    source_id,
                    False,
                    health=SourceHealth(
                        source_id,
                        "failed",
                        error_code=type(exc).__name__,
                        message=str(exc),
                    ),
                    error=str(exc),
                )
            )
    return CollectionBatch(tuple(results))
