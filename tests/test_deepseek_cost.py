from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from jnby_news_watch.cost import estimate_cost, is_off_peak, next_off_peak
from jnby_news_watch.deepseek import DeepSeekClient


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
        self.payloads = []

    def post_json(self, payload, *, api_key, timeout):
        self.call_count += 1
        self.payloads.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def batch():
    return [
        {
            "id": "n1",
            "title": "Paris apparel store opening",
            "excerpt": "A designer retailer is preparing a Paris launch.",
            "deterministic_score": 78,
        }
    ]


def valid_response():
    content = {
        "items": [
            {
                "id": "n1",
                "summary_zh": "一家设计师品牌正在筹备巴黎门店开业。",
                "semantic_tags": ["巴黎", "线下开店"],
                "conflict_flags": [],
                "impact_class": "medium",
            }
        ]
    }
    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 200,
            "completion_tokens": 50,
        },
    }


def pricing():
    path = (
        Path(__file__).parents[1]
        / "skills"
        / "jnby-news-watch"
        / "assets"
        / "deepseek-pricing.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_beijing_0800_is_off_peak():
    value = datetime(2026, 8, 20, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_off_peak(value) is True


def test_beijing_1000_is_peak():
    value = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert is_off_peak(value) is False


def test_budget_mode_can_compute_next_off_peak():
    value = datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert next_off_peak(value).astimezone(ZoneInfo("Asia/Shanghai")).hour == 12


def test_cost_uses_separate_cache_hit_miss_and_output_rates():
    when = datetime(2026, 8, 20, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = estimate_cost(
        pricing(),
        model="deepseek-v4-flash",
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        output_tokens=1_000_000,
        at=when,
    )

    assert result.period == "off_peak"
    assert result.cache_hit_usd == 0.007
    assert result.cache_miss_usd == 0.22
    assert result.output_usd == 0.66
    assert result.total_usd == 0.887


def test_invalid_json_retries_once_then_falls_back():
    transport = FakeTransport(["not json", "still not json"])
    client = DeepSeekClient(transport, api_key="secret-value")

    result = client.enrich(batch(), "immediate")

    assert result.used_fallback is True
    assert result.items == ()
    assert transport.call_count == 2
    assert "secret-value" not in result.error


def test_valid_json_usage_and_non_thinking_mode():
    transport = FakeTransport([valid_response()])
    client = DeepSeekClient(transport, api_key="secret-value")

    result = client.enrich(batch(), "immediate")

    assert result.used_fallback is False
    assert result.items[0]["id"] == "n1"
    assert result.usage.cache_hit_tokens == 100
    assert transport.payloads[0]["model"] == "deepseek-v4-flash"
    assert transport.payloads[0]["thinking"] == {"type": "disabled"}
    assert transport.payloads[0]["response_format"] == {"type": "json_object"}


def test_unknown_id_or_model_supplied_url_is_rejected():
    bad = valid_response()
    bad["choices"][0]["message"]["content"] = json.dumps(
        {
            "items": [
                {
                    "id": "unknown",
                    "summary_zh": "摘要",
                    "semantic_tags": [],
                    "conflict_flags": [],
                    "impact_class": "low",
                    "url": "https://attacker.example/",
                }
            ]
        }
    )
    transport = FakeTransport([bad, bad])

    result = DeepSeekClient(transport, api_key="secret-value").enrich(batch(), "urgent")

    assert result.used_fallback is True
    assert transport.call_count == 2
