from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from jnby_news_watch.security import FetchResult
from jnby_news_watch.sources import (
    BlueskyPublicAdapter,
    JsonReviewAdapter,
    PageLinksAdapter,
    Query,
    RssAdapter,
    SourceResult,
    TimeWindow,
    collect_all,
    with_retry,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureFetcher:
    def __init__(self, payload: bytes, content_type: str):
        self.payload = payload
        self.content_type = content_type
        self.calls = 0

    def fetch(self, url: str) -> FetchResult:
        self.calls += 1
        return FetchResult(
            url=url,
            body=self.payload,
            headers={"content-type": self.content_type},
            status=200,
        )


def window() -> TimeWindow:
    return TimeWindow(
        since=datetime(2026, 8, 18, tzinfo=timezone.utc),
        until=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def test_rss_adapter_emits_traceable_candidates():
    fetcher = FixtureFetcher(
        (FIXTURES / "news.rss").read_bytes(), "application/rss+xml"
    )
    source = {
        "id": "fashion-wire",
        "name": "Fashion Wire",
        "channel": "discovery",
        "tier": "S2",
        "url": "https://example.com/feed.xml",
    }

    result = RssAdapter(fetcher).collect(
        source,
        [Query("paris retail"), Query("supply chain")],
        window(),
    )

    assert result.ok is True
    assert len(result.candidates) == 1
    item = result.candidates[0]
    assert item.title == "Paris fashion retailer opens a new store"
    assert item.original_url.startswith("https://example.com/paris-store")
    assert item.source_id == "fashion-wire"
    assert item.source_tier == "S2"
    assert item.verified is False
    assert item.published_at.tzinfo is not None
    assert fetcher.calls == 1


def test_authorized_json_adapter_anonymizes_review_author():
    fetcher = FixtureFetcher(
        (FIXTURES / "reviews.json").read_bytes(), "application/json"
    )
    source = {
        "id": "authorized-reviews",
        "name": "Authorized Reviews",
        "channel": "review",
        "tier": "V",
        "url": "https://reviews.example.com/export.json",
        "approved": True,
    }

    result = JsonReviewAdapter(fetcher, author_salt="test-salt").collect(
        source, [Query("JNBY")], window()
    )

    assert result.ok is True
    assert len(result.reviews) == 1
    review = result.reviews[0]
    assert review.platform == "authorized-shop"
    assert review.original_url == "https://reviews.example.com/r/1"
    assert review.author_key != "public-user-123"
    assert len(review.author_key) == 16
    assert "fabric feels premium" in review.text


def test_json_review_adapter_rejects_unapproved_source():
    fetcher = FixtureFetcher(json.dumps({"items": []}).encode(), "application/json")
    source = {
        "id": "unknown-export",
        "channel": "review",
        "tier": "V",
        "url": "https://reviews.example.com/export.json",
        "approved": False,
    }

    result = JsonReviewAdapter(fetcher, author_salt="test-salt").collect(
        source, [], window()
    )

    assert result.ok is False
    assert "approved" in result.error.lower()
    assert fetcher.calls == 0


def test_collect_all_isolates_a_failed_source():
    class Working:
        def collect(self, source, queries, time_window):
            return SourceResult(source_id=source["id"], ok=True)

    class Broken:
        def collect(self, source, queries, time_window):
            raise TimeoutError("source timed out")

    sources = [
        {"id": "good", "adapter": "working"},
        {"id": "bad", "adapter": "broken"},
    ]
    batch = collect_all(
        sources=sources,
        adapters={"working": Working(), "broken": Broken()},
        queries=[Query("retail")],
        time_window=window(),
    )

    assert [result.ok for result in batch.results] == [True, False]
    assert batch.results[1].source_id == "bad"
    assert "timed out" in batch.results[1].error


def test_retry_stops_after_configured_attempts():
    calls = 0

    def always_fails():
        nonlocal calls
        calls += 1
        raise TimeoutError("temporary")

    try:
        with_retry(always_fails, retries=2, delay_seconds=0)
    except TimeoutError:
        pass
    else:
        raise AssertionError("expected TimeoutError")

    assert calls == 3


def test_bluesky_public_posts_keep_original_link_and_anonymize_author():
    payload = {
        "posts": [
            {
                "uri": "at://did:plc:abc/app.bsky.feed.post/3xyz",
                "author": {"did": "did:plc:abc", "handle": "shopper.example"},
                "record": {
                    "text": "JNBY Paris fitting experience and fabric review",
                    "createdAt": "2026-08-19T10:00:00Z",
                    "langs": ["en"],
                },
                "likeCount": 2,
                "replyCount": 1,
                "repostCount": 0,
            }
        ]
    }
    fetcher = FixtureFetcher(json.dumps(payload).encode(), "application/json")
    source = {
        "id": "bluesky-public",
        "approved": True,
        "queries": ["JNBY"],
    }

    result = BlueskyPublicAdapter(fetcher, author_salt="salt").collect(
        source, [], window()
    )

    assert result.ok is True
    assert len(result.reviews) == 1
    review = result.reviews[0]
    assert review.original_url == "https://bsky.app/profile/shopper.example/post/3xyz"
    assert review.author_key != "did:plc:abc"
    assert review.metadata["access"] == "public"
    assert review.metadata["engagement"] == 3


def test_official_listing_requires_visible_date_and_url_pattern():
    html = b"""<html><body>
    <a href='/nav'>Home</a>
    <a href='/group/s/1/news_detail/10'>Official store update 2026/08/19</a>
    <a href='/group/s/1/news_detail/11'>Future update 2026/09/01</a>
    </body></html>"""
    fetcher = FixtureFetcher(html, "text/html; charset=utf-8")
    source = {
        "id": "official-news",
        "name": "Official",
        "url": "https://official.example/list",
        "tier": "S0",
        "require_date": True,
        "include_url_contains": ["/news_detail/"],
        "languages": ["en"],
    }

    result = PageLinksAdapter(fetcher).collect(source, [], window())

    assert result.ok is True
    assert len(result.candidates) == 1
    assert result.candidates[0].original_url.endswith("/news_detail/10")
    assert result.candidates[0].metadata["date_verified"] is True
