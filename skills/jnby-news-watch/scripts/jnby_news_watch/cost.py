from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone


PEAK_WINDOWS_UTC = ((time(1, 0), time(4, 0)), (time(6, 0), time(10, 0)))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def is_off_peak(value: datetime) -> bool:
    utc_time = _aware(value).astimezone(timezone.utc).time().replace(tzinfo=None)
    return not any(start <= utc_time < end for start, end in PEAK_WINDOWS_UTC)


def next_off_peak(value: datetime) -> datetime:
    original_tz = _aware(value).tzinfo
    utc_value = value.astimezone(timezone.utc)
    current = utc_value.time().replace(tzinfo=None)
    for start, end in PEAK_WINDOWS_UTC:
        if start <= current < end:
            boundary = datetime.combine(utc_value.date(), end, tzinfo=timezone.utc)
            return boundary.astimezone(original_tz)
    return value


@dataclass(frozen=True)
class CostEstimate:
    model: str
    period: str
    cache_hit_usd: float
    cache_miss_usd: float
    output_usd: float
    total_usd: float
    pricing_checked_at: str


def estimate_cost(
    pricing: dict,
    *,
    model: str,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
    at: datetime,
) -> CostEstimate:
    if min(cache_hit_tokens, cache_miss_tokens, output_tokens) < 0:
        raise ValueError("token counts must not be negative")
    period = "off_peak" if is_off_peak(at) else "peak"
    try:
        rates = pricing["per_million_tokens"][model][period]
    except KeyError as exc:
        raise ValueError(f"pricing unavailable for {model}/{period}") from exc
    hit = cache_hit_tokens / 1_000_000 * float(rates["cache_hit_input"])
    miss = cache_miss_tokens / 1_000_000 * float(rates["cache_miss_input"])
    output = output_tokens / 1_000_000 * float(rates["output"])
    return CostEstimate(
        model=model,
        period=period,
        cache_hit_usd=round(hit, 9),
        cache_miss_usd=round(miss, 9),
        output_usd=round(output, 9),
        total_usd=round(hit + miss + output, 9),
        pricing_checked_at=str(pricing.get("checked_at", "unknown")),
    )
