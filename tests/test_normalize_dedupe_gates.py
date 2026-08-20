from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jnby_news_watch.dedupe import cluster_news, cluster_reviews
from jnby_news_watch.gates import GatePolicy, evaluate_news_gate, evaluate_review_gate
from jnby_news_watch.normalize import normalize_candidate, normalize_url
from jnby_news_watch.sources import RawCandidate, RawReview


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def news(
    source_id: str,
    url: str,
    *,
    title: str = "Paris apparel retailer opens flagship store",
    tier: str = "S1",
    verified: bool = True,
    metadata: dict | None = None,
) -> RawCandidate:
    return RawCandidate(
        id=f"{source_id}-1",
        title=title,
        original_url=url,
        published_at=NOW,
        source_id=source_id,
        source_name=source_id,
        source_tier=tier,
        channel="news" if tier != "S2" else "discovery",
        summary="The retailer is opening in Paris with a local logistics partner.",
        verified=verified,
        metadata=metadata or {"body": "same syndicated story body"},
    )


def review(review_id: str, author: str, platform: str = "shop") -> RawReview:
    return RawReview(
        id=review_id,
        platform=platform,
        original_url=f"https://reviews.example.com/{review_id}",
        published_at=NOW,
        author_key=author,
        text="The fabric feels premium but store pickup was slow",
        source_id="authorized-export",
        metadata={"access": "authorized", "theme_terms": ["fabric", "pickup"]},
    )


def test_url_and_multilingual_aliases_are_normalized():
    assert normalize_url("HTTPS://Example.COM/x/?utm_source=rss&b=2&a=1#frag") == (
        "https://example.com/x?a=1&b=2"
    )
    item = news(
        "media",
        "https://example.com/x?gclid=123",
        title="  江南布衣 巴黎 新店 | Latest News  ",
        metadata={"body": "线下开店与供应链物流"},
    )

    normalized = normalize_candidate(item)

    assert normalized.original_url == "https://example.com/x"
    assert normalized.title == "江南布衣 巴黎 新店"
    assert "JNBY" in normalized.metadata["entities"]
    assert "Paris" in normalized.metadata["entities"]
    assert "store_opening" in normalized.metadata["topics"]
    assert "logistics" in normalized.metadata["topics"]


def test_syndicated_copies_count_as_one_independent_source():
    copies = [
        news(
            "site-a",
            "https://a.example.com/story",
            metadata={"body": "identical wire copy", "syndication_origin": "Reuters"},
        ),
        news(
            "site-b",
            "https://b.example.com/reprint",
            metadata={"body": "identical wire copy", "syndication_origin": "Reuters"},
        ),
    ]

    clusters = cluster_news(copies)

    assert len(clusters) == 1
    assert clusters[0].independent_source_count == 1
    assert "near_duplicate" in clusters[0].merge_reasons


def test_false_updated_timestamp_does_not_create_new_event():
    original = news("official", "https://example.com/event", metadata={"body": "unchanged"})
    touched = RawCandidate(
        **{
            **original.__dict__,
            "id": "official-2",
            "published_at": NOW + timedelta(hours=2),
            "metadata": {"body": "unchanged", "updated_at": NOW.isoformat()},
        }
    )

    clusters = cluster_news([original, touched])

    assert len(clusters) == 1
    assert "same_canonical_url" in clusters[0].merge_reasons


def test_high_impact_news_requires_two_independent_sources():
    cluster = cluster_news(
        [news("trade-media", "https://media.example.com/tariff", title="New apparel tariff in France")]
    )[0]

    decision = evaluate_news_gate(cluster, GatePolicy())

    assert decision.eligible is False
    assert decision.bucket == "candidate"
    assert decision.evidence_grade == "B"
    assert "two independent" in " ".join(decision.reasons).lower()


def test_search_only_item_is_candidate_grade_c():
    cluster = cluster_news(
        [
            news(
                "search",
                "https://news.example.com/search-result",
                tier="S2",
                verified=False,
                metadata={"snippet_only": True},
            )
        ]
    )[0]

    decision = evaluate_news_gate(cluster, GatePolicy())

    assert decision.eligible is False
    assert decision.bucket == "candidate"
    assert decision.evidence_grade == "C"


def test_discovery_copy_does_not_satisfy_high_impact_two_source_rule():
    cluster = cluster_news(
        [
            news("trade-media", "https://media.example.com/tariff", title="New apparel tariff in France"),
            news(
                "search-copy",
                "https://search.example.com/tariff",
                title="New apparel tariff in France",
                tier="S2",
                verified=False,
                metadata={"body": "same syndicated story body", "snippet_only": True},
            ),
        ]
    )[0]

    decision = evaluate_news_gate(cluster, GatePolicy())

    assert cluster.independent_source_count == 2
    assert cluster.trusted_independent_source_count == 1
    assert decision.eligible is False


def test_official_source_is_grade_a_and_eligible():
    cluster = cluster_news(
        [news("jnby-official", "https://jnby.com/news/store", tier="S0")]
    )[0]

    decision = evaluate_news_gate(cluster, GatePolicy())

    assert decision.eligible is True
    assert decision.bucket == "formal"
    assert decision.evidence_grade == "A"


def test_two_independent_sources_upgrade_high_impact_news_to_a():
    items = [
        news(
            "trade-a",
            "https://a.example.com/tariff",
            title="New apparel tariff in France",
            metadata={"body": "confirmed tariff event"},
        ),
        news(
            "trade-b",
            "https://b.example.com/tariff",
            title="New apparel tariff in France",
            metadata={"body": "confirmed tariff event"},
        ),
    ]
    decision = evaluate_news_gate(cluster_news(items)[0], GatePolicy())
    assert decision.eligible is True
    assert decision.evidence_grade == "A"


def test_review_gate_requires_original_trace_and_anonymized_author():
    valid = review("r1", "a13f8c91d7b2e444")
    invalid = RawReview(**{**valid.__dict__, "id": "r2", "author_key": ""})

    assert evaluate_review_gate(valid).eligible is True
    rejected = evaluate_review_gate(invalid)
    assert rejected.eligible is False
    assert rejected.bucket == "rejected"


def test_review_confidence_uses_accounts_and_platforms_not_sentiment():
    items = [
        review("r1", "author-1", "shop"),
        review("r2", "author-2", "shop"),
        review("r3", "author-3", "social"),
        review("r4", "author-4", "social"),
        review("r5", "author-5", "social"),
    ]

    clusters = cluster_reviews(items)

    assert len(clusters) == 1
    assert clusters[0].confidence == "high"
    assert clusters[0].independent_author_count == 5
