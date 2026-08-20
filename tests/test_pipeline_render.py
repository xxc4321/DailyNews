from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
SKILL_ROOT = Path(__file__).parents[1] / "skills" / "jnby-news-watch"


class FixtureAdapter:
    name = "fixture"
    channel = "news"

    def __init__(self, news_count=12, review_count=6):
        self.news_count = news_count
        self.review_count = review_count

    def collect(self, source, queries, time_window):
        candidates = tuple(
            RawCandidate(
                id=f"news-{i}",
                title=f"JNBY retail development number {i} in Paris",
                original_url=f"https://media{i}.example.com/news/{i}",
                published_at=NOW - timedelta(hours=i),
                source_id=f"media-{i}",
                source_name=f"Trusted Media {i}",
                source_tier="S1",
                channel="news",
                language="en" if i % 2 else "fr",
                summary=f"Verified retail development {i} with customer training details.",
                verified=True,
                metadata={"body": f"unique event body token-{i}", "entities": ["JNBY", "Paris"]},
            )
            for i in range(self.news_count)
        )
        reviews = tuple(
            RawReview(
                id=f"review-{i}",
                platform="shop" if i % 2 else "social",
                original_url=f"https://reviews.example.com/{i}",
                published_at=NOW - timedelta(hours=i),
                author_key=f"anon-{i}",
                text=f"Specific customer observation {i} about fitting and service",
                language="en",
                source_id="authorized-reviews",
                metadata={"access": "authorized", "theme_terms": [f"theme-{i}"]},
            )
            for i in range(self.review_count)
        )
        return SourceResult(
            source_id=source["id"],
            ok=True,
            candidates=candidates,
            reviews=reviews,
            health=SourceHealth(source["id"], "ok", latency_ms=3),
        )


class Enricher:
    def __init__(self, fallback=False):
        self.fallback = fallback

    def enrich(self, batch, mode, *, now=None):
        if self.fallback:
            return EnrichmentResult(
                (), Usage(), "deepseek-v4-flash", True, 2, error="fixture invalid JSON"
            )
        return EnrichmentResult(
            tuple(
                {
                    "id": item["id"],
                    "summary_zh": f"中文摘要：{item['title']}",
                    "semantic_tags": ["零售"],
                    "conflict_flags": [],
                    "impact_class": "low",
                }
                for item in batch
            ),
            Usage(100, 200, 50),
            "deepseek-v4-flash",
            False,
            1,
        )


class FailingAdapter:
    name = "fixture"
    channel = "news"

    def collect(self, source, queries, time_window):
        raise TimeoutError("all sources unavailable")


def runtime(tmp_path, adapter_name="fixture") -> RuntimeConfig:
    profile = yaml.safe_load(
        (SKILL_ROOT / "assets" / "default-profile.yaml").read_text(encoding="utf-8")
    )
    pricing = yaml.safe_load(
        (SKILL_ROOT / "assets" / "deepseek-pricing.yaml").read_text(encoding="utf-8")
    )
    return RuntimeConfig(
        skill_root=SKILL_ROOT,
        home=tmp_path / "home",
        profile=profile,
        sources={"version": 1, "sources": [{"id": "fixture-source", "adapter": adapter_name}]},
        focus={"version": 1, "focuses": []},
        pricing=pricing,
    )


def make_pipeline(tmp_path, *, news=12, reviews=6, fallback=False, failing=False):
    config = runtime(tmp_path)
    adapter = FailingAdapter() if failing else FixtureAdapter(news, reviews)
    return Pipeline(
        config,
        StateStore(config.home / "state.sqlite3"),
        {"fixture": adapter},
        enricher=Enricher(fallback=fallback),
        report_root=tmp_path / "reports",
        clock=lambda: NOW,
    )


def test_pipeline_outputs_separate_ranked_sections_and_links(tmp_path):
    pipeline = make_pipeline(tmp_path)

    result = pipeline.run(RunRequest(mode="manual", news_limit=10, review_limit=5))

    assert len(result.news) == 10
    assert len(result.customer_voice) == 5
    assert all(item.original_url.startswith("https://") for item in result.news)
    assert "Top 10 新闻" in result.feishu_pages[0]
    assert "Customer Voice" in "\n".join(result.feishu_pages)
    assert "https://media" in "\n".join(result.feishu_pages)
    assert (result.report_dir / "digest.json").is_file()
    assert (result.report_dir / "digest.md").is_file()


def test_custom_20_10_uses_pages_of_at_most_five_entries(tmp_path):
    pipeline = make_pipeline(tmp_path, news=22, reviews=12)

    result = pipeline.run(RunRequest(mode="manual", news_limit=20, review_limit=10))

    news_pages = [page for page in result.feishu_pages if page.startswith("## 新闻")]
    review_pages = [page for page in result.feishu_pages if page.startswith("## Customer Voice")]
    assert len(news_pages) == 4
    assert len(review_pages) == 2
    assert all(page.count("### ") <= 5 for page in news_pages + review_pages)


def test_deepseek_failure_keeps_deterministic_digest_with_warning(tmp_path):
    result = make_pipeline(tmp_path, fallback=True).run(
        RunRequest(mode="manual", news_limit=3, review_limit=2)
    )

    assert len(result.news) == 3
    assert any("DeepSeek" in warning for warning in result.warnings)
    assert "语义增强不可用" in result.feishu_pages[0]


def test_all_network_sources_failed_is_not_delivery_eligible(tmp_path):
    result = make_pipeline(tmp_path, failing=True).run(
        RunRequest(mode="scheduled", news_limit=10, review_limit=5)
    )

    assert result.delivery_eligible is False
    assert result.news == ()
    assert any("全部信源" in warning for warning in result.warnings)
