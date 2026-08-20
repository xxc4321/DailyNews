from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from jnby_news_watch.dedupe import cluster_reviews
from jnby_news_watch.focus import FocusRecord, FocusStore, effective_focus_strength
from jnby_news_watch.score import score_news, score_review_theme
from jnby_news_watch.sources import RawCandidate, RawReview


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def profile() -> dict:
    path = (
        Path(__file__).parents[1]
        / "skills"
        / "jnby-news-watch"
        / "assets"
        / "default-profile.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def article(text: str, *, metadata: dict | None = None) -> RawCandidate:
    return RawCandidate(
        id=text[:12],
        title=text,
        original_url="https://example.com/story",
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
        source_id="trusted",
        source_name="Trusted Media",
        source_tier="S1",
        channel="news",
        verified=True,
        metadata=metadata or {},
    )


def paris_focus() -> FocusRecord:
    return FocusRecord(
        id="focus-paris",
        label="Paris new store",
        terms=("Paris", "巴黎", "store opening", "线下开店", "logistics", "物流", "tariff", "关税"),
        valid_from=START,
        valid_until=START + timedelta(days=30),
        decay_days=7,
        strength=100,
        status="approved",
    )


def test_paris_focus_boosts_opening_logistics_and_tariff_news():
    focus = paris_focus()
    at = START + timedelta(days=2)

    paris = score_news(
        article("JNBY Paris store opening customs tariff logistics"), profile(), [focus], at=at
    )
    generic = score_news(article("general fashion trend"), profile(), [focus], at=at)

    assert paris.total > generic.total
    assert paris.contributions["focus"] <= 25
    assert paris.contributions["supplier_logistics_tariff_inventory"] > 0
    assert "Paris new store" in paris.explanation


def test_base_weights_are_explainable_and_evidence_is_not_a_score():
    result = score_news(
        article("JNBY Paris apparel retail logistics wholesale training", metadata={"evidence_grade": "A"}),
        profile(),
        [],
    )

    assert result.contributions["company_brand_competitor"] == 22
    assert result.contributions["market_location"] == 18
    assert "evidence" not in result.contributions
    assert 0 <= result.total <= 100


def test_risk_penalty_is_capped_at_twenty():
    result = score_news(
        article("JNBY Paris store", metadata={"spam_risk": 99, "manipulation_risk": 99}),
        profile(),
        [],
    )

    assert result.penalties["pollution_risk"] == 20


def test_unapproved_focus_does_not_change_scores(tmp_path):
    store = FocusStore(tmp_path)
    proposal = store.propose(
        "Paris store",
        terms=["Paris", "store opening"],
        days=30,
        now=START,
    )

    assert store.active(at=START + timedelta(days=1)) == []
    store.approve(proposal.id, now=START)
    assert store.active(at=START + timedelta(days=1))[0].id == proposal.id


def test_focus_decays_and_can_rollback(tmp_path):
    focus = paris_focus()
    end = focus.valid_until

    assert effective_focus_strength(focus, START + timedelta(days=22)) == 100
    assert effective_focus_strength(focus, START + timedelta(days=26)) == 400 / 7
    assert effective_focus_strength(focus, end) == 0

    store = FocusStore(tmp_path)
    store.propose_record(focus)
    approved_history_id = store.approve(focus.id, now=START)
    store.disable(focus.id, now=START + timedelta(days=2))
    store.rollback(approved_history_id, now=START + timedelta(days=3))

    assert [item.id for item in store.active(at=START + timedelta(days=1))] == [focus.id]


def test_customer_voice_score_keeps_confidence_separate_from_sentiment():
    items = [
        RawReview(
            id=f"r{i}",
            platform="shop" if i < 3 else "social",
            original_url=f"https://reviews.example.com/{i}",
            published_at=datetime.now(timezone.utc) - timedelta(hours=i),
            author_key=f"author-{i}",
            text="Paris store pickup was slow but the linen fabric feels premium",
            metadata={"theme_terms": ["pickup", "fabric"], "access": "public", "sentiment": -0.8},
        )
        for i in range(5)
    ]
    theme = cluster_reviews(items)[0]

    result = score_review_theme(theme, profile())

    assert result.confidence == "high"
    assert "sentiment" not in result.contributions
    assert 0 <= result.total <= 100
