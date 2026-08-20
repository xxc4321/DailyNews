from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

from .extract import extract_article
from .sources import RawCandidate, TimeWindow


def _domain_matches(hostname: str, approved: tuple[str, ...]) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in approved)


class DiscoveryVerifier:
    def __init__(
        self,
        fetcher,
        *,
        s0_domains: list[str],
        s1_domains: list[str],
        candidate_cap: int = 30,
        max_workers: int = 6,
    ):
        self.fetcher = fetcher
        self.s0_domains = tuple(value.lower() for value in s0_domains)
        self.s1_domains = tuple(value.lower() for value in s1_domains)
        self.candidate_cap = max(0, candidate_cap)
        self.max_workers = max(1, max_workers)

    def verify(self, item: RawCandidate, window: TimeWindow) -> RawCandidate:
        if item.source_tier != "S2":
            return item
        try:
            response = self.fetcher.fetch(item.original_url)
            article = extract_article(response.body, response.headers, response.url)
            canonical = urljoin(response.url, article.canonical_url)
            hostname = (urlsplit(canonical).hostname or "").lower()
            if _domain_matches(hostname, self.s0_domains):
                tier = "S0"
            elif _domain_matches(hostname, self.s1_domains):
                tier = "S1"
            else:
                return item
            if article.published_at is None or not window.since <= article.published_at <= window.until:
                return item
            if article.security_flags or len(article.text) < 200:
                return item
            metadata = dict(item.metadata)
            metadata.update(
                {
                    "body": article.text[:6000],
                    "safe_clean": True,
                    "discovered_url": item.original_url,
                    "verified_domain": hostname,
                }
            )
            return replace(
                item,
                title=article.title or item.title,
                original_url=canonical,
                published_at=article.published_at,
                source_name=hostname,
                source_tier=tier,
                channel="news",
                language=article.language,
                summary=article.text[:1000],
                verified=True,
                metadata=metadata,
            )
        except Exception as exc:
            metadata = dict(item.metadata)
            metadata["verification_error"] = type(exc).__name__
            return replace(item, metadata=metadata)

    def verify_batch(
        self, items: list[RawCandidate], window: TimeWindow
    ) -> list[RawCandidate]:
        direct = [item for item in items if item.source_tier != "S2"]
        all_discovery = sorted(
            (item for item in items if item.source_tier == "S2"),
            key=lambda item: item.published_at,
            reverse=True,
        )
        discovery = all_discovery[: self.candidate_cap]
        overflow = all_discovery[self.candidate_cap :]
        if not discovery:
            return [*direct, *overflow]
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(discovery)),
            thread_name_prefix="jnby-verify",
        ) as pool:
            verified = list(pool.map(lambda item: self.verify(item, window), discovery))
        return [*direct, *verified, *overflow]
