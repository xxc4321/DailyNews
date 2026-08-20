from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import re
from urllib.parse import urlsplit

from .normalize import normalize_candidate, normalize_text
from .sources import RawCandidate, RawReview


TOKEN_RE = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in TOKEN_RE.findall(normalize_text(value))
        if len(token) > 1 or token.isdigit()
    }


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _content_hash(item: RawCandidate) -> str:
    body = str(item.metadata.get("body") or item.summary or item.title)
    return hashlib.sha256(normalize_text(body).casefold().encode()).hexdigest()


def _independence_key(item: RawCandidate) -> str:
    declared = (
        item.metadata.get("syndication_origin")
        or item.metadata.get("original_publisher")
        or item.metadata.get("ownership_group")
    )
    if declared:
        return str(declared).casefold()
    return (urlsplit(item.original_url).hostname or item.source_id).casefold()


@dataclass(frozen=True)
class NewsCluster:
    id: str
    items: tuple[RawCandidate, ...]
    merge_reasons: tuple[str, ...]
    independent_source_count: int
    trusted_independent_source_count: int

    @property
    def primary(self) -> RawCandidate:
        tiers = {"S0": 0, "S1": 1, "S2": 2}
        return sorted(self.items, key=lambda item: (tiers.get(item.source_tier, 9), -len(item.summary)))[0]


def _news_merge_reason(item: RawCandidate, cluster: list[RawCandidate]) -> str | None:
    for existing in cluster:
        if item.original_url == existing.original_url:
            return "same_canonical_url"
        close_in_time = abs(item.published_at - existing.published_at) <= timedelta(hours=72)
        same_content = _content_hash(item) == _content_hash(existing)
        title_similarity = _similarity(item.title, existing.title)
        item_body = str(item.metadata.get("body") or item.summary)
        existing_body = str(existing.metadata.get("body") or existing.summary)
        body_similarity = _similarity(item_body, existing_body)
        shared_entities = bool(
            set(item.metadata.get("entities", ())) & set(existing.metadata.get("entities", ()))
        )
        if close_in_time and (
            same_content
            or (title_similarity >= 0.85 and body_similarity >= 0.55)
            or (shared_entities and title_similarity >= 0.72 and body_similarity >= 0.72)
        ):
            return "near_duplicate"
    return None


def cluster_news(items: list[RawCandidate]) -> list[NewsCluster]:
    groups: list[list[RawCandidate]] = []
    reasons: list[set[str]] = []
    for raw in items:
        item = normalize_candidate(raw)
        for index, group in enumerate(groups):
            reason = _news_merge_reason(item, group)
            if reason:
                group.append(item)
                reasons[index].add(reason)
                break
        else:
            groups.append([item])
            reasons.append(set())
    clusters: list[NewsCluster] = []
    for group, group_reasons in zip(groups, reasons):
        cluster_id = hashlib.sha256(group[0].original_url.encode()).hexdigest()[:24]
        clusters.append(
            NewsCluster(
                cluster_id,
                tuple(group),
                tuple(sorted(group_reasons)),
                len({_independence_key(item) for item in group}),
                len(
                    {
                        _independence_key(item)
                        for item in group
                        if item.source_tier in {"S0", "S1"} and item.verified
                    }
                ),
            )
        )
    return clusters


@dataclass(frozen=True)
class ReviewCluster:
    id: str
    items: tuple[RawReview, ...]
    theme_terms: tuple[str, ...]
    independent_author_count: int
    platform_count: int
    confidence: str
    merge_reasons: tuple[str, ...]


def _review_theme(item: RawReview) -> tuple[str, ...]:
    declared = tuple(str(value).casefold() for value in item.metadata.get("theme_terms", ()))
    return declared or tuple(sorted(_tokens(item.text)))


def cluster_reviews(items: list[RawReview]) -> list[ReviewCluster]:
    groups: list[list[RawReview]] = []
    group_themes: list[set[str]] = []
    for item in items:
        theme = set(_review_theme(item))
        for index, known in enumerate(group_themes):
            if theme and known and len(theme & known) / len(theme | known) >= 0.4:
                groups[index].append(item)
                known.update(theme)
                break
        else:
            groups.append([item])
            group_themes.append(theme)
    output: list[ReviewCluster] = []
    for group, themes in zip(groups, group_themes):
        authors = {item.author_key for item in group if item.author_key}
        platforms = {item.platform.casefold() for item in group if item.platform}
        if len(authors) >= 5 and (
            len(platforms) >= 2
            or (max(item.published_at for item in group) - min(item.published_at for item in group))
            >= timedelta(hours=24)
        ):
            confidence = "high"
        elif len(authors) >= 3:
            confidence = "emerging"
        else:
            confidence = "individual"
        cluster_id = hashlib.sha256("|".join(sorted(themes)).encode()).hexdigest()[:24]
        output.append(
            ReviewCluster(
                cluster_id,
                tuple(group),
                tuple(sorted(themes)),
                len(authors),
                len(platforms),
                confidence,
                ("shared_theme",) if len(group) > 1 else (),
            )
        )
    return output
