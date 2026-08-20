from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = ROOT / "skills" / "jnby-news-watch" / "scripts"
sys.path.insert(0, str(PACKAGE_ROOT))

from jnby_news_watch.cli import build_pipeline  # noqa: E402
from jnby_news_watch.config import initialize_runtime  # noqa: E402
from jnby_news_watch.cost import estimate_cost  # noqa: E402
from jnby_news_watch.deepseek import DeepSeekClient  # noqa: E402
from jnby_news_watch.models import RunRequest  # noqa: E402


def _env_value(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def _load_deepseek_key(hermes_home: Path) -> str:
    value = os.environ.get("DEEPSEEK_API_KEY", "")
    if value:
        return value
    for path in (hermes_home / ".env", hermes_home / "config" / ".env"):
        value = _env_value(path, "DEEPSEEK_API_KEY")
        if value:
            os.environ["DEEPSEEK_API_KEY"] = value
            return value
    raise RuntimeError("DEEPSEEK_API_KEY is not configured")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe_title(value: str) -> str:
    return (
        value.replace("[", "［")
        .replace("]", "］")
        .replace("<", "＜")
        .replace(">", "＞")[:180]
    )


def _deepseek_smoke(api_key: str, pricing: dict, called_at: datetime) -> list[dict]:
    client = DeepSeekClient(api_key=api_key, max_tokens=500)
    batch = [
        {
            "id": "smoke-1",
            "title": "JNBY Paris store opening logistics",
            "excerpt": "A verified test record about retail training, logistics and a Paris launch.",
            "deterministic_score": 82,
        }
    ]
    evidence = []
    for sequence in (1, 2):
        result = client.enrich(batch, "immediate", now=called_at)
        if result.used_fallback or not result.items:
            raise RuntimeError(f"DeepSeek smoke {sequence} failed strict validation: {result.error}")
        cost = estimate_cost(
            pricing,
            model=result.model,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cache_miss_tokens=result.usage.cache_miss_tokens,
            output_tokens=result.usage.completion_tokens,
            at=called_at,
        )
        evidence.append(
            {
                "sequence": sequence,
                "model": result.model,
                "strict_json_valid": True,
                "attempts": result.attempts,
                "usage": asdict(result.usage),
                "cost": asdict(cost),
                "returned_ids": [item["id"] for item in result.items],
            }
        )
    return evidence


def _send_feishu(hermes: str, *, message: str | None = None, file: Path | None = None) -> dict:
    command = [hermes, "send", "--to", "feishu", "--json"]
    if file is not None:
        command.extend(["--file", str(file)])
    elif message is not None:
        command.append(message)
    else:
        raise ValueError("message or file is required")
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=45)
    if completed.returncode != 0:
        raise RuntimeError(f"Hermes Feishu send failed with exit code {completed.returncode}")
    receipt = completed.stdout.strip()
    return {
        "success": True,
        "receipt_sha256": hashlib.sha256(receipt.encode()).hexdigest(),
        "receipt_bytes": len(receipt.encode()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deepseek-only", action="store_true")
    mode.add_argument("--send-feishu-test", action="store_true")
    parser.add_argument("--hermes-home", type=Path, required=True)
    args = parser.parse_args(argv)

    hermes_home = args.hermes_home.resolve()
    api_key = _load_deepseek_key(hermes_home)
    called_at = datetime.now(timezone.utc)
    date = called_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    evidence_dir = ROOT / ".jnby-news-watch" / "reports" / date
    evidence_path = evidence_dir / "live-smoke.json"
    existing = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else {}

    scratch_home = ROOT / ".jnby-news-watch" / "live-runtime"
    config = initialize_runtime(ROOT / "skills" / "jnby-news-watch", scratch_home)
    evidence = {
        **existing,
        "schema_version": 1,
        "checked_at": called_at.isoformat(),
        "credential_presence": {"deepseek": True, "feishu_via_hermes": True},
        "deepseek": _deepseek_smoke(api_key, config.pricing, called_at),
    }
    serialized = json.dumps(evidence, ensure_ascii=False)
    if api_key in serialized:
        raise RuntimeError("secret leak guard rejected smoke evidence")
    _atomic_json(evidence_path, evidence)

    if args.deepseek_only:
        print(
            json.dumps(
                {
                    "success": True,
                    "evidence": str(evidence_path),
                    "runs": evidence["deepseek"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    pipeline = build_pipeline(scratch_home)
    result = pipeline.run(
        RunRequest(
            mode="manual",
            news_limit=3,
            review_limit=2,
            since=called_at - timedelta(hours=72),
            until=called_at,
            cost_mode="immediate",
            dry_run=True,
        )
    )
    news_items = [*result.news, *result.candidates][:3]
    if not news_items:
        raise RuntimeError("live collection returned no traceable news or candidates")
    digest_lines = ["[TEST] JNBY News Watch 小型日报", "", "## 新闻"]
    digest_lines.extend(
        f"{index}. [{_safe_title(item.title)}](<{item.original_url}>)｜相关度 {item.score:.1f}｜证据 {item.evidence_grade}"
        for index, item in enumerate(news_items, 1)
    )
    digest_lines.extend(["", "## Customer Voice"])
    if result.customer_voice:
        for index, voice in enumerate(result.customer_voice[:2], 1):
            url = voice.representative_urls[0]
            digest_lines.append(
                f"{index}. [{_safe_title(voice.label)}](<{url}>)｜信号分 {voice.score:.1f}｜{voice.confidence}"
            )
    else:
        digest_lines.append("本次窗口没有通过安全门的公开客评；未使用私密或绕过访问控制的数据。")
    digest_file = evidence_dir / "test-digest.md"
    digest_file.parent.mkdir(parents=True, exist_ok=True)
    digest_file.write_text("\n".join(digest_lines) + "\n", encoding="utf-8")

    hermes = str(hermes_home / "hermes-agent" / "venv" / "Scripts" / "hermes.exe")
    send_state = existing.get("feishu", {})
    if not send_state.get("connectivity", {}).get("success"):
        send_state["connectivity"] = _send_feishu(
            hermes,
            message="[TEST] JNBY News Watch 已连通。此消息由已批准的本地 Hermes 冒烟测试发送。",
        )
        evidence["feishu"] = send_state
        _atomic_json(evidence_path, evidence)
    if not send_state.get("small_digest", {}).get("success"):
        send_state["small_digest"] = _send_feishu(hermes, file=digest_file)
        evidence["feishu"] = send_state
        _atomic_json(evidence_path, evidence)

    evidence["collection"] = {
        "report_id": result.report_id,
        "formal_news": len(result.news),
        "customer_voice": len(result.customer_voice),
        "candidates": len(result.candidates),
        "source_health": [
            {key: value for key, value in item.items() if key != "message"}
            for item in result.health
        ],
        "report_dir": str(result.report_dir),
    }
    _atomic_json(evidence_path, evidence)
    print(
        json.dumps(
            {
                "success": True,
                "evidence": str(evidence_path),
                "feishu": send_state,
                "collection": evidence["collection"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
