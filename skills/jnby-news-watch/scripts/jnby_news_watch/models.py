from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


RunMode = Literal["scheduled", "manual", "deep_dive"]
CostMode = Literal["immediate", "budget", "urgent"]


def _require_aware(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class RunRequest:
    mode: RunMode
    news_limit: int = 10
    review_limit: int = 5
    since: datetime | None = None
    until: datetime | None = None
    focus_terms: tuple[str, ...] = ()
    cost_mode: CostMode = "immediate"
    dry_run: bool = False

    def validate(self) -> None:
        if self.mode not in {"scheduled", "manual", "deep_dive"}:
            raise ValueError("unsupported run mode")
        if not 1 <= self.news_limit <= 100:
            raise ValueError("news_limit must be between 1 and 100")
        if not 0 <= self.review_limit <= 100:
            raise ValueError("review_limit must be between 0 and 100")
        if self.cost_mode not in {"immediate", "budget", "urgent"}:
            raise ValueError("unsupported cost_mode")
        _require_aware(self.since, "since")
        _require_aware(self.until, "until")
        if self.since and self.until and self.since > self.until:
            raise ValueError("since must not be after until")

    def to_dict(self) -> dict:
        result = asdict(self)
        for key in ("since", "until"):
            if result[key] is not None:
                result[key] = result[key].isoformat()
        return result


@dataclass(frozen=True)
class RuntimeConfig:
    skill_root: Path
    home: Path
    profile: dict
    sources: dict
    focus: dict
    pricing: dict


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    canonical_url: str
    publisher: str
    published_at: datetime
    language: str = "en"
    source_tier: str = "S2"
    summary: str = ""
    score: float = 0.0
    evidence_grade: str = "C"
    metadata: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.canonical_url.startswith(("https://", "http://")):
            raise ValueError("canonical_url must be HTTP(S)")
        _require_aware(self.published_at, "published_at")
        if not 0 <= self.score <= 100:
            raise ValueError("score must be between 0 and 100")
        if self.evidence_grade not in {"A", "B", "C"}:
            raise ValueError("invalid evidence_grade")


@dataclass(frozen=True)
class ReviewItem:
    id: str
    platform: str
    original_url: str
    published_at: datetime
    author_key: str
    text: str
    language: str = "en"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeCluster:
    id: str
    label: str
    review_ids: tuple[str, ...]
    signal_score: float
    confidence: str
    representative_urls: tuple[str, ...]
