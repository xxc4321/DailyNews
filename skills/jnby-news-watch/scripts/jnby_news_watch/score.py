from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .dedupe import ReviewCluster
from .focus import FocusRecord, effective_focus_strength
from .normalize import normalize_text
from .sources import RawCandidate


CATEGORY_TERMS = {
    "company_brand_competitor": (
        "jnby", "江南布衣", "croquis", "速写", "less", "pomme de terre", "designer brand", "competitor"
    ),
    "market_location": (
        "china", "中国", "germany", "德国", "indonesia", "印尼", "lithuania", "立陶宛",
        "georgia", "格鲁吉亚", "australia", "澳大利亚", "malaysia", "马来西亚", "france", "法国",
        "paris", "巴黎", "italy", "italia", "意大利", "united kingdom", "英国", "europe", "欧洲",
    ),
    "apparel_design_material": (
        "apparel", "fashion", "garment", "design", "fabric", "linen", "服装", "时装", "设计", "面料"
    ),
    "retail_store_customer": (
        "retail", "store", "shop", "flagship", "customer", "pickup", "零售", "门店", "开店", "顾客", "客户体验"
    ),
    "supplier_logistics_tariff_inventory": (
        "supplier", "logistics", "customs", "tariff", "inventory", "supply chain", "供应商", "物流", "关税", "库存", "供应链"
    ),
    "wholesale_storytelling_training_data": (
        "wholesale", "storytelling", "training", "coaching", "data", "styling", "批发", "品牌故事", "培训", "数据", "搭配"
    ),
}


@dataclass(frozen=True)
class ScoreResult:
    total: float
    contributions: dict[str, float]
    penalties: dict[str, float]
    explanation: str


@dataclass(frozen=True)
class ReviewScoreResult:
    total: float
    contributions: dict[str, float]
    penalties: dict[str, float]
    confidence: str
    explanation: str


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(term.casefold() in lowered for term in terms)


def score_news(
    item: RawCandidate,
    profile: dict,
    focuses: list[FocusRecord],
    *,
    at: datetime | None = None,
) -> ScoreResult:
    checked_at = at or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    text = normalize_text(
        f"{item.title} {item.summary} {item.metadata.get('body', '')} "
        f"{' '.join(str(x) for x in item.metadata.get('entities', []))} "
        f"{' '.join(str(x) for x in item.metadata.get('topics', []))}"
    )
    weights = profile["weights"]
    base: dict[str, float] = {
        category: float(weights[category]) if _has_any(text, terms) else 0.0
        for category, terms in CATEGORY_TERMS.items()
    }
    age_hours = max(0.0, (checked_at - item.published_at).total_seconds() / 3600)
    freshness_weight = float(weights["freshness_business_impact"])
    base["freshness_business_impact"] = (
        freshness_weight if age_hours <= 24 else freshness_weight * 0.625 if age_hours <= 72 else 0.0
    )
    base_total = sum(base.values())

    active_focuses = [focus for focus in focuses if effective_focus_strength(focus, checked_at) > 0]
    focus_value = 0.0
    focus_labels: list[str] = []
    if active_focuses:
        matches: list[float] = []
        for focus in active_focuses:
            matched = sum(1 for term in focus.terms if term.casefold() in text.casefold())
            if matched:
                ratio = min(1.0, matched / max(1, len(focus.terms)))
                strength = effective_focus_strength(focus, checked_at) / 100
                matches.append(ratio * strength)
                focus_labels.append(focus.label)
        focus_value = min(25.0, 25.0 * max(matches, default=0.0))
        contributions = {key: round(value * 0.75, 3) for key, value in base.items()}
    else:
        contributions = {key: round(value, 3) for key, value in base.items()}
    contributions["focus"] = round(focus_value, 3)

    high_impact = _has_any(
        text,
        ("closure", "disruption", "recall", "boycott", "关闭", "中断", "召回", "抵制"),
    )
    medium_impact = _has_any(text, ("tariff", "customs", "关税", "海关"))
    impact_bonus = 10.0 if high_impact else 5.0 if medium_impact else 0.0
    contributions["impact_bonus"] = impact_bonus

    pollution = min(
        20.0,
        max(0.0, float(item.metadata.get("spam_risk", 0)))
        + max(0.0, float(item.metadata.get("manipulation_risk", 0))),
    )
    penalties = {"pollution_risk": pollution}
    total = min(100.0, max(0.0, sum(contributions.values()) - pollution))
    explanation = (
        "基础命中："
        + "、".join(key for key, value in base.items() if value)
        + (f"；动态焦点：{'、'.join(focus_labels)}" if focus_labels else "；无动态焦点命中")
    )
    return ScoreResult(round(total, 2), contributions, penalties, explanation)


def score_review_theme(theme: ReviewCluster, profile: dict) -> ReviewScoreResult:
    weights = profile["review_weights"]
    text = " ".join(item.text for item in theme.items).casefold()
    contributions = {
        "relevance": float(weights["relevance"])
        if _has_any(text, CATEGORY_TERMS["retail_store_customer"] + CATEGORY_TERMS["apparel_design_material"])
        else 0.0,
        "specificity": float(weights["specificity"]) if sum(len(item.text) for item in theme.items) / len(theme.items) >= 30 else float(weights["specificity"]) / 2,
        "recurrence": float(weights["recurrence"]) * min(1.0, theme.independent_author_count / 5),
        "actionability": float(weights["actionability"])
        if _has_any(text, ("pickup", "service", "size", "fabric", "delivery", "取货", "服务", "尺码", "面料", "配送"))
        else 0.0,
        "freshness": float(weights["freshness"]),
        "engagement": min(
            float(weights["engagement"]),
            sum(float(item.metadata.get("engagement", 0)) for item in theme.items) / max(1, len(theme.items)),
        ),
    }
    manipulation = min(
        30.0,
        sum(float(item.metadata.get("manipulation_risk", 0)) for item in theme.items)
        / max(1, len(theme.items)),
    )
    penalties = {"manipulation_risk": manipulation}
    total = min(100.0, max(0.0, sum(contributions.values()) - manipulation))
    explanation = (
        f"{theme.independent_author_count} 个独立账号，{theme.platform_count} 个平台；"
        "情绪仅用于描述，不计入证据或相关度。"
    )
    return ReviewScoreResult(
        round(total, 2),
        {key: round(value, 3) for key, value in contributions.items()},
        penalties,
        theme.confidence,
        explanation,
    )
