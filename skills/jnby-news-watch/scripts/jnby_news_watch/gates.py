from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .dedupe import NewsCluster
from .sources import RawReview


@dataclass(frozen=True)
class GatePolicy:
    high_impact_terms: tuple[str, ...] = (
        "tariff",
        "customs",
        "关税",
        "供应链中断",
        "supply chain disruption",
        "store closure",
        "门店关闭",
        "recall",
        "boycott",
        "歧视",
    )
    high_impact_min_sources: int = 2


@dataclass(frozen=True)
class GateDecision:
    eligible: bool
    bucket: Literal["formal", "candidate", "rejected"]
    evidence_grade: Literal["A", "B", "C", "V"]
    reasons: tuple[str, ...]


def _high_impact(cluster: NewsCluster, policy: GatePolicy) -> bool:
    text = " ".join(
        f"{item.title} {item.summary} {item.metadata.get('body', '')}" for item in cluster.items
    ).casefold()
    return any(term.casefold() in text for term in policy.high_impact_terms)


def evaluate_news_gate(cluster: NewsCluster, policy: GatePolicy) -> GateDecision:
    trusted = [item for item in cluster.items if item.source_tier in {"S0", "S1"} and item.verified]
    official = any(item.source_tier == "S0" and item.verified for item in cluster.items)
    high_impact = _high_impact(cluster, policy)
    corroborated_high_impact = (
        high_impact
        and cluster.trusted_independent_source_count >= policy.high_impact_min_sources
    )
    if official or corroborated_high_impact:
        grade: Literal["A", "B", "C"] = "A"
    elif trusted:
        grade = "B"
    else:
        grade = "C"

    reasons: list[str] = []
    if grade == "C":
        reasons.append("No trusted original source; discovery/search-only evidence")
        return GateDecision(False, "candidate", grade, tuple(reasons))

    if any(item.metadata.get("safe_clean") is False for item in cluster.items):
        reasons.append("Safety cleaning failed")
        return GateDecision(False, "rejected", grade, tuple(reasons))

    if any(item.metadata.get("content_farm") or item.metadata.get("aggregate_only") for item in cluster.items):
        reasons.append("Aggregate or content-farm source")
        return GateDecision(False, "candidate", grade, tuple(reasons))

    if high_impact and cluster.trusted_independent_source_count < policy.high_impact_min_sources:
        reasons.append("High-impact news requires two independent trusted sources")
        return GateDecision(False, "candidate", grade, tuple(reasons))

    reasons.append("Trusted original evidence gate passed")
    return GateDecision(True, "formal", grade, tuple(reasons))


def evaluate_review_gate(review: RawReview) -> GateDecision:
    reasons: list[str] = []
    access = str(review.metadata.get("access", "")).lower()
    if access not in {"public", "authorized"}:
        reasons.append("Review is not a public original or authorized export")
    if not review.original_url.startswith(("https://", "http://")):
        reasons.append("Original review trace is missing")
    if not review.platform:
        reasons.append("Platform is missing")
    if review.published_at.tzinfo is None:
        reasons.append("Timezone-aware review time is missing")
    if not review.author_key:
        reasons.append("Anonymized author key is missing")
    if review.metadata.get("spam") or review.metadata.get("marketing"):
        reasons.append("Spam or marketing pattern detected")
    if reasons:
        return GateDecision(False, "rejected", "V", tuple(reasons))
    return GateDecision(True, "formal", "V", ("Customer Voice signal gate passed",))
