from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
from html.parser import HTMLParser
import io
import json
import os
import re
import time
from typing import Callable, Literal, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin

import feedparser

from .security import DnsResolutionError, SafeFetcher


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
    if isinstance(exc, DnsResolutionError):
        return True
    if isinstance(exc, HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, URLError)


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


def _date_from_listing_title(value: str) -> datetime | None:
    match = re.search(r"(?<!\d)(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})日?(?!\d)", value)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=timezone(timedelta(hours=8)),
        )
    except ValueError:
        return None


def _feed_datetime(entry: dict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return _parse_datetime(entry.get("published") or entry.get("updated"))


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]


def _has_marketing_pattern(value: str) -> bool:
    lowered = value.casefold()
    terms = ("buy now", "limited offer", "promo code", "折扣码", "立即购买")
    return any(term in lowered for term in terms)


class RssAdapter:
    name = "rss"
    channel: Channel = "news"
    query_dependent = False

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
            query_list: list[Query | None] = queries if self.query_dependent else [None]
            if not query_list:
                query_list = [None]
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
    query_dependent = True

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
            candidates_list = []
            include_fragments = tuple(str(value) for value in source.get("include_url_contains", []))
            require_date = bool(source.get("require_date", False))
            for href, title in parser.links:
                absolute_url = urljoin(result.url, href)
                published_at = _date_from_listing_title(title)
                if not title or not href or href.lower().startswith(("javascript:", "mailto:")):
                    continue
                if include_fragments and not any(fragment in absolute_url for fragment in include_fragments):
                    continue
                if require_date and published_at is None:
                    continue
                effective_date = published_at or time_window.until
                if not time_window.since <= effective_date <= time_window.until:
                    continue
                candidates_list.append(
                    RawCandidate(
                    id=_stable_id(source["id"], urljoin(result.url, href)),
                    title=title,
                    original_url=absolute_url,
                    published_at=effective_date,
                    source_id=source["id"],
                    source_name=source.get("name", source["id"]),
                    source_tier=source.get("tier", "S0"),
                    channel="news",
                    language=(source.get("languages") or ["en"])[0],
                    verified=True,
                    metadata={
                        "listing_url": result.url,
                        "date_inferred": published_at is None,
                        "date_verified": published_at is not None,
                        "date_source": "official_listing_text" if published_at else "run_window",
                    },
                )
                )
            candidates = tuple(candidates_list)
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
            for query in queries[: int(source.get("max_queries", 4))]:
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
                        metadata={"access": "authorized"},
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


class BlueskyPublicAdapter:
    name = "bluesky_public"
    channel: Channel = "review"

    def __init__(self, fetcher: SafeFetcher, *, author_salt: str, max_per_query: int = 25):
        self.fetcher = fetcher
        self.author_salt = author_salt
        self.max_per_query = min(max(1, max_per_query), 100)

    def _author_key(self, value: str) -> str:
        return hashlib.sha256(f"{self.author_salt}:{value}".encode()).hexdigest()[:16]

    def collect(self, source: dict, queries: list[Query], time_window: TimeWindow) -> SourceResult:
        if not source.get("approved", False):
            message = "public social source must be explicitly approved"
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "disabled", error_code="NotApproved", message=message),
                error=message,
            )
        started = time.perf_counter()
        reviews: list[RawReview] = []
        seen: set[str] = set()
        attempts = 0
        query_texts = [str(value) for value in source.get("queries", [])]
        if not query_texts:
            query_texts = [query.text for query in queries[:2]]
        try:
            for query_text in query_texts:
                url = (
                    "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q="
                    f"{quote_plus(query_text)}&limit={self.max_per_query}&sort=latest"
                )
                attempts += 1
                response = with_retry(lambda url=url: self.fetcher.fetch(url))
                payload = json.loads(response.body)
                for post in payload.get("posts", []):
                    uri = str(post.get("uri", ""))
                    record = post.get("record") or {}
                    text_value = str(record.get("text", "")).strip()
                    created_at = _parse_datetime(record.get("createdAt") or post.get("indexedAt"))
                    if not uri or not text_value or uri in seen:
                        continue
                    if not time_window.since <= created_at <= time_window.until:
                        continue
                    parts = uri.split("/")
                    if len(parts) < 5 or parts[-2] != "app.bsky.feed.post":
                        continue
                    author = post.get("author") or {}
                    actor = str(author.get("handle") or author.get("did") or parts[2])
                    original_url = f"https://bsky.app/profile/{actor}/post/{parts[-1]}"
                    languages = record.get("langs") or ["und"]
                    link_count = text_value.lower().count("http://") + text_value.lower().count("https://")
                    marketing = link_count >= 2 or _has_marketing_pattern(text_value)
                    reviews.append(
                        RawReview(
                            id=_stable_id(source["id"], uri),
                            platform="Bluesky",
                            original_url=original_url,
                            published_at=created_at,
                            author_key=self._author_key(str(author.get("did") or actor)),
                            text=text_value,
                            language=str(languages[0]),
                            source_id=source["id"],
                            metadata={
                                "access": "public",
                                "engagement": int(post.get("likeCount", 0))
                                + int(post.get("replyCount", 0))
                                + int(post.get("repostCount", 0)),
                                "marketing": marketing,
                                "public_uri": uri,
                            },
                        )
                    )
                    seen.add(uri)
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], True, reviews=tuple(reviews),
                health=SourceHealth(source["id"], "ok" if reviews else "empty", latency, max(attempts, 1)),
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return SourceResult(
                source["id"], False,
                health=SourceHealth(source["id"], "failed", latency, max(attempts, 1), type(exc).__name__, str(exc)),
                error=str(exc),
            )


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
    max_workers: int = 4,
) -> CollectionBatch:
    def collect_one(source: dict) -> SourceResult:
        adapter_name = source.get("adapter", "")
        adapter = adapters.get(adapter_name)
        if adapter is None:
            message = f"adapter not registered: {adapter_name}"
            return SourceResult(
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
        try:
            return adapter.collect(source, queries, time_window)
        except Exception as exc:
            source_id = source.get("id", adapter_name)
            return SourceResult(
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
    if not sources:
        return CollectionBatch(())
    workers = min(max(1, max_workers), len(sources))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jnby-source") as pool:
        results = list(pool.map(collect_one, sources))
    return CollectionBatch(tuple(results))
