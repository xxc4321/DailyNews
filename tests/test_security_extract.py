from pathlib import Path

import pytest

from jnby_news_watch.extract import extract_article, read_bounded
from jnby_news_watch.security import UnsafeUrlError, validate_public_url


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "https://user:password@example.com/",
    ],
)
def test_private_local_or_credentialed_urls_are_blocked(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


def test_public_resolved_address_is_allowed() -> None:
    def resolver(host: str, port: int, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    parsed = validate_public_url("https://example.com/article", resolver=resolver)
    assert parsed.hostname == "example.com"
    assert parsed.addresses == ("93.184.216.34",)


def test_hidden_prompt_injection_is_removed_and_flagged() -> None:
    payload = (FIXTURES / "injection.html").read_bytes()
    article = extract_article(
        payload,
        {"content-type": "text/html; charset=utf-8"},
        "https://example.com/review",
    )
    assert "new store experience" in article.text
    assert "ignore previous" not in article.text.lower()
    assert "sendCredentials" not in article.text
    assert "hidden_instruction" in article.security_flags


def test_article_metadata_is_extracted() -> None:
    payload = (FIXTURES / "article.html").read_bytes()
    article = extract_article(
        payload,
        {"content-type": "text/html; charset=utf-8"},
        "https://example.com/discovery?id=123",
    )
    assert article.title == "Une nouvelle boutique à Paris"
    assert article.canonical_url == "https://example.com/paris-store"
    assert article.language == "fr"
    assert article.published_at.isoformat() == "2026-08-19T07:30:00+02:00"
    assert article.author == "Example Fashion Desk"
    assert "logistique locale" in article.text


def test_response_size_limit_fails_closed() -> None:
    with pytest.raises(ValueError, match="response exceeds"):
        read_bounded([b"1234", b"5678"], max_bytes=6)
