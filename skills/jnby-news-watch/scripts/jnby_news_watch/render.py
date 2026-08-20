from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class DigestNews:
    cluster_id: str
    content_hash: str
    title: str
    original_url: str
    published_at: datetime
    language: str
    publisher: str
    score: float
    evidence_grade: str
    summary_zh: str
    score_explanation: str
    score_contributions: dict[str, float] = field(default_factory=dict)
    independent_sources: int = 1
    locations: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    gate_reason: str = ""
    already_sent_today: bool = False


@dataclass(frozen=True)
class DigestVoice:
    cluster_id: str
    label: str
    score: float
    confidence: str
    summary_zh: str
    action_hint: str
    independent_reviews: int
    platforms: tuple[str, ...]
    representative_urls: tuple[str, ...]


@dataclass(frozen=True)
class DigestResult:
    report_id: str
    generated_at: datetime
    window_since: datetime
    window_until: datetime
    news_limit: int
    review_limit: int
    news: tuple[DigestNews, ...]
    customer_voice: tuple[DigestVoice, ...]
    candidates: tuple[DigestNews, ...]
    feishu_pages: tuple[str, ...]
    report_dir: Path
    delivery_eligible: bool
    warnings: tuple[str, ...] = ()
    health: tuple[dict, ...] = ()
    cost: dict = field(default_factory=dict)


def _safe_text(value: str, limit: int = 800) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()[:limit]
    text = text.replace("<", "＜").replace(">", "＞")
    for char in "\\`*_{}[]()#+-.!|":
        text = text.replace(char, "\\" + char)
    return text


def _news_entry(item: DigestNews, index: int) -> str:
    locations = "、".join(item.locations) if item.locations else "未标注"
    sources = [item.original_url, *[url for url in item.source_urls if url != item.original_url]]
    source_lines = [f"[原文 {number}](<{url}>)" for number, url in enumerate(sources[:2], 1)]
    sent = "｜今日已发过" if item.already_sent_today else ""
    return "\n".join(
        [
            f"### {index}. {_safe_text(item.title, 240)}",
            f"{_safe_text(item.publisher, 100)}｜{item.published_at.date().isoformat()}｜{_safe_text(item.language, 20)}｜{_safe_text(locations, 120)}",
            f"相关度 {item.score:.1f}｜证据 {item.evidence_grade}{sent}",
            _safe_text(item.summary_zh, 600),
            f"加分依据：{_safe_text(item.score_explanation, 400)}",
            "｜".join(source_lines),
        ]
    )


def _voice_entry(item: DigestVoice, index: int) -> str:
    links = "｜".join(
        f"[代表原帖 {number}](<{url}>)"
        for number, url in enumerate(item.representative_urls[:3], 1)
    )
    return "\n".join(
        [
            f"### {index}. {_safe_text(item.label, 180)}",
            f"信号分 {item.score:.1f}｜置信度 {_safe_text(item.confidence, 40)}｜独立评论 {item.independent_reviews}",
            f"平台：{_safe_text('、'.join(item.platforms), 160)}",
            _safe_text(item.summary_zh, 600),
            f"可行动启示：{_safe_text(item.action_hint, 400)}",
            links,
        ]
    )


def render_feishu_pages(
    *,
    generated_at: datetime,
    window_since: datetime,
    window_until: datetime,
    news_limit: int,
    review_limit: int,
    news: tuple[DigestNews, ...],
    voices: tuple[DigestVoice, ...],
    candidates: tuple[DigestNews, ...],
    warnings: tuple[str, ...],
    health: tuple[dict, ...],
    cost: dict,
) -> tuple[str, ...]:
    healthy = sum(1 for item in health if item.get("status") in {"ok", "empty"})
    failed = sum(1 for item in health if item.get("status") == "failed")
    overview = [
        "## JNBY 海外零售每日情报",
        f"Top {news_limit} 新闻 + Top {review_limit} Customer Voice",
        f"窗口：{window_since.isoformat()} → {window_until.isoformat()}",
        f"本次正式输出：{len(news)} 条新闻、{len(voices)} 个客评主题；候补 {len(candidates)} 条",
        f"信源健康：{healthy} 正常，{failed} 失败",
    ]
    if cost:
        overview.append(
            f"DeepSeek：{cost.get('model', 'unknown')}｜{cost.get('period', 'unknown')}｜估算 ${cost.get('total_usd', 0):.6f}"
        )
    if warnings:
        overview.append("警告：" + "；".join(_safe_text(value, 240) for value in warnings))
    pages = ["\n\n".join(overview)]
    for offset in range(0, len(news), 5):
        chunk = news[offset : offset + 5]
        pages.append(
            f"## 新闻 {offset + 1}–{offset + len(chunk)}\n\n"
            + "\n\n".join(_news_entry(item, offset + index + 1) for index, item in enumerate(chunk))
        )
    for offset in range(0, len(voices), 5):
        chunk = voices[offset : offset + 5]
        pages.append(
            f"## Customer Voice {offset + 1}–{offset + len(chunk)}\n\n"
            + "\n\n".join(_voice_entry(item, offset + index + 1) for index, item in enumerate(chunk))
        )
    if candidates:
        body = []
        for index, item in enumerate(candidates[:10], 1):
            body.append(
                f"{index}. [{_safe_text(item.title, 180)}](<{item.original_url}>)｜证据 {item.evidence_grade}｜"
                f"{_safe_text(item.gate_reason, 240)}"
            )
        pages.append("## 候补区（未进入正式榜）\n\n" + "\n".join(body))
    return tuple(pages)


def _news_dict(item: DigestNews) -> dict:
    payload = asdict(item)
    payload["published_at"] = item.published_at.isoformat()
    return payload


def digest_to_dict(result: DigestResult) -> dict:
    return {
        "schema_version": 1,
        "report_id": result.report_id,
        "generated_at": result.generated_at.isoformat(),
        "window_since": result.window_since.isoformat(),
        "window_until": result.window_until.isoformat(),
        "news_limit": result.news_limit,
        "review_limit": result.review_limit,
        "delivery_eligible": result.delivery_eligible,
        "warnings": list(result.warnings),
        "health": list(result.health),
        "cost": result.cost,
        "news": [_news_dict(item) for item in result.news],
        "customer_voice": [asdict(item) for item in result.customer_voice],
        "candidates": [_news_dict(item) for item in result.candidates],
        "feishu_pages": list(result.feishu_pages),
    }


def _full_markdown(result: DigestResult) -> str:
    sections = [result.feishu_pages[0]]
    if result.news:
        sections.append(
            "# Top 新闻\n\n"
            + "\n\n".join(_news_entry(item, index) for index, item in enumerate(result.news, 1))
        )
    if result.customer_voice:
        sections.append(
            "# Customer Voice\n\n"
            + "\n\n".join(
                _voice_entry(item, index) for index, item in enumerate(result.customer_voice, 1)
            )
        )
    if result.candidates:
        sections.append(result.feishu_pages[-1])
    return "\n\n---\n\n".join(sections) + "\n"


def write_report_bundle(result: DigestResult) -> None:
    result.report_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        result.report_dir / "digest.json": json.dumps(
            digest_to_dict(result), ensure_ascii=False, indent=2
        )
        + "\n",
        result.report_dir / "digest.md": _full_markdown(result),
    }
    for path, content in payloads.items():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
