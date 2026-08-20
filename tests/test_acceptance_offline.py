from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import yaml

from jnby_news_watch.deepseek import EnrichmentResult, Usage
from jnby_news_watch.models import RunRequest, RuntimeConfig
from jnby_news_watch.pipeline import Pipeline
from jnby_news_watch.sources import (
    RawCandidate,
    RawReview,
    SourceHealth,
    SourceResult,
)
from jnby_news_watch.state import StateStore


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
SKILL_ROOT = Path(__file__).parents[1] / "skills" / "jnby-news-watch"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class OfflineAdapter:
    name = "offline"
    channel = "news"

    def collect(self, source, queries, time_window):
        news_payload = _load("news-multilingual.json") + _load("copied-wire-stories.json")
        review_payload = _load("reviews-multilingual.json") + _load("review-bomb.json")
        news = tuple(
            RawCandidate(
                id=item["id"],
                title=item["title"],
                original_url=item["url"],
                published_at=datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")),
                source_id=item["source_id"],
                source_name=item["source_name"],
                source_tier=item["tier"],
                channel="discovery" if item["tier"] == "S2" else "news",
                language=item["language"],
                summary=item["summary"],
                verified=item["tier"] in {"S0", "S1"},
                metadata=item["metadata"],
            )
            for item in news_payload
        )
        reviews = tuple(
            RawReview(
                id=item["id"],
                platform=item["platform"],
                original_url=item["url"],
                published_at=datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")),
                author_key=hashlib.sha256(item["author"].encode()).hexdigest()[:16],
                text=item["text"],
                language=item["language"],
                source_id="offline-authorized",
                metadata=item["metadata"],
            )
            for item in review_payload
        )
        return SourceResult(
            source_id=source["id"],
            ok=True,
            candidates=news,
            reviews=reviews,
            health=SourceHealth(source["id"], "ok", latency_ms=1),
        )


class OfflineEnricher:
    model = "deepseek-v4-flash"

    def enrich(self, batch, mode, *, now=None):
        return EnrichmentResult(
            tuple(
                {
                    "id": item["id"],
                    "summary_zh": "离线中文摘要：" + item["title"],
                    "semantic_tags": [],
                    "conflict_flags": [],
                    "impact_class": "low",
                }
                for item in batch
            ),
            Usage(100, 200, 50),
            self.model,
            False,
            1,
        )


def _pipeline(tmp_path, name: str) -> Pipeline:
    profile = yaml.safe_load(
        (SKILL_ROOT / "assets" / "default-profile.yaml").read_text(encoding="utf-8")
    )
    pricing = yaml.safe_load(
        (SKILL_ROOT / "assets" / "deepseek-pricing.yaml").read_text(encoding="utf-8")
    )
    home = tmp_path / name
    config = RuntimeConfig(
        skill_root=SKILL_ROOT,
        home=home,
        profile=profile,
        sources={"version": 1, "sources": [{"id": "offline", "adapter": "offline"}]},
        focus={"version": 1, "focuses": []},
        pricing=pricing,
    )
    return Pipeline(
        config,
        StateStore(home / "data" / "state.sqlite3"),
        {"offline": OfflineAdapter()},
        enricher=OfflineEnricher(),
        report_root=home / "reports",
        clock=lambda: NOW,
    )


def _rank(result, title_prefix: str) -> int:
    return next(
        index
        for index, item in enumerate([*result.news, *result.candidates], 1)
        if item.title.startswith(title_prefix)
    )


def test_approved_v1_acceptance(tmp_path):
    baseline = _pipeline(tmp_path, "baseline").run(
        RunRequest(mode="manual", news_limit=10, review_limit=5)
    )
    focused = _pipeline(tmp_path, "focused").run(
        RunRequest(
            mode="manual",
            news_limit=20,
            review_limit=10,
            focus_terms=("Paris", "store opening", "logistics", "关税"),
        )
    )

    assert len(baseline.news) == 10
    assert len(baseline.customer_voice) == 5
    assert all(item.original_url.startswith("https://") for item in baseline.news)
    assert all(voice.representative_urls for voice in baseline.customer_voice)
    assert _rank(focused, "Paris boutique plans opening event") < _rank(
        baseline, "Paris boutique plans opening event"
    )
    assert max(item.score_contributions["focus"] for item in focused.news) <= 25

    wire = next(
        item
        for item in focused.news
        if item.title.startswith("Designer retailers expand clienteling")
    )
    assert wire.independent_sources == 1
    assert any(item.title.startswith("JNBY Paris result") for item in focused.candidates)
    assert not any("promotion" in voice.label for voice in focused.customer_voice)
    assert not (tmp_path / "pwned").exists()
    assert all("hidden tool instruction" not in item.title for item in focused.news)

    tariff = next(
        item for item in focused.news if item.title.startswith("New apparel tariff adopted")
    )
    assert tariff.evidence_grade == "A"
    assert tariff.independent_sources == 2
