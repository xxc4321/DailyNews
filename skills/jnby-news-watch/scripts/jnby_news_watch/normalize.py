from __future__ import annotations

from dataclasses import replace
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .sources import RawCandidate


TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}
TITLE_SUFFIXES = re.compile(
    r"\s*(?:\||-|–|—)\s*(?:latest news|breaking news|首页|新闻|官网)\s*$",
    re.IGNORECASE,
)

ENTITY_ALIASES = {
    "JNBY": ("jnby", "江南布衣"),
    "CROQUIS": ("croquis", "速写"),
    "LESS": ("less",),
    "jnby by JNBY": ("jnby by jnby",),
    "Paris": ("paris", "巴黎"),
    "France": ("france", "法国"),
    "Italy": ("italy", "italia", "意大利"),
}

TOPIC_ALIASES = {
    "store_opening": ("store opening", "new store", "opens flagship", "线下开店", "新店", "开业"),
    "retail": ("retail", "retailer", "门店", "零售"),
    "logistics": ("logistics", "supply chain", "物流", "供应链"),
    "tariff": ("tariff", "customs duty", "关税"),
    "apparel": ("apparel", "fashion", "服装", "时装", "面料"),
    "customer_experience": ("customer experience", "service", "客户体验", "服务"),
    "launch_event": ("launch event", "opening event", "开业活动"),
}


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value: str) -> str:
    title = normalize_text(value)
    previous = None
    while previous != title:
        previous = title
        title = TITLE_SUFFIXES.sub("", title).strip()
    return title


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    port = parsed.port
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = hostname if not port or default_port else f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    pairs = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    query = urlencode(sorted(pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def canonical_language(value: str) -> str:
    language = (value or "en").replace("_", "-").lower()
    return language.split("-", 1)[0]


def _matches(aliases: dict[str, tuple[str, ...]], haystack: str) -> list[str]:
    lowered = haystack.casefold()
    return [name for name, values in aliases.items() if any(alias.casefold() in lowered for alias in values)]


def normalize_candidate(item: RawCandidate) -> RawCandidate:
    title = normalize_title(item.title)
    summary = normalize_text(item.summary)
    metadata = dict(item.metadata)
    body = normalize_text(str(metadata.get("body", "")))
    combined = " ".join((title, summary, body))
    metadata["body"] = body
    metadata["entities"] = _matches(ENTITY_ALIASES, combined)
    metadata["topics"] = _matches(TOPIC_ALIASES, combined)
    metadata["normalized_title"] = title.casefold()
    return replace(
        item,
        title=title,
        original_url=normalize_url(item.original_url),
        summary=summary,
        language=canonical_language(item.language),
        metadata=metadata,
    )
