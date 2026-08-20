from datetime import datetime, timezone

from jnby_news_watch.security import FetchResult
from jnby_news_watch.sources import RawCandidate, TimeWindow
from jnby_news_watch.verify import DiscoveryVerifier


class Fetcher:
    def __init__(self, final_url="https://reuters.com/world/fashion-story"):
        self.final_url = final_url

    def fetch(self, url):
        body = b"""<!doctype html><html lang='en'><head>
        <title>Verified apparel retail story</title>
        <meta property='article:published_time' content='2026-08-19T08:00:00Z'>
        <link rel='canonical' href='/world/fashion-story'>
        </head><body><article>""" + (b"Verified reporting on apparel retail and Paris store operations. " * 8) + b"</article></body></html>"
        return FetchResult(
            self.final_url,
            body,
            {"content-type": "text/html; charset=utf-8"},
            200,
        )


def candidate():
    return RawCandidate(
        id="d1",
        title="Search result",
        original_url="https://discovery.example/result",
        published_at=datetime(2026, 8, 19, 8, tzinfo=timezone.utc),
        source_id="discovery",
        source_name="Discovery",
        source_tier="S2",
        channel="discovery",
        verified=False,
    )


def window():
    return TimeWindow(
        datetime(2026, 8, 18, tzinfo=timezone.utc),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def test_discovery_item_promotes_only_after_trusted_original_is_opened():
    verifier = DiscoveryVerifier(
        Fetcher(), s0_domains=[], s1_domains=["reuters.com"]
    )
    result = verifier.verify(candidate(), window())
    assert result.source_tier == "S1"
    assert result.verified is True
    assert result.original_url == "https://reuters.com/world/fashion-story"
    assert result.metadata["verified_domain"] == "reuters.com"


def test_unapproved_domain_remains_discovery_only():
    verifier = DiscoveryVerifier(
        Fetcher("https://unknown.example/story"),
        s0_domains=[],
        s1_domains=["reuters.com"],
    )
    result = verifier.verify(candidate(), window())
    assert result.source_tier == "S2"
    assert result.verified is False
