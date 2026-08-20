from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from .cli import build_pipeline
from .models import RunRequest
from .state import StateStore


JOB_NAME = "JNBY Daily Intelligence"
TARGET = "feishu-private"


def _read_json(path: Path, fallback):
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _job(jobs_path: Path) -> dict | None:
    payload = _read_json(jobs_path, {})
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    matches = [item for item in jobs if item.get("name") == JOB_NAME]
    if len(matches) > 1:
        raise RuntimeError("duplicate JNBY cron jobs; delivery reconciliation stopped")
    return matches[0] if matches else None


def reconcile_pending(
    *,
    state: StateStore,
    pending_path: Path,
    jobs_path: Path,
) -> dict:
    records = _read_json(pending_path, [])
    job = _job(jobs_path)
    if not job or not job.get("last_run_at"):
        return {"confirmed": 0, "failed": 0, "remaining": len(records)}
    last_run = datetime.fromisoformat(str(job["last_run_at"]).replace("Z", "+00:00"))
    confirmed = failed = 0
    remaining = []
    for record in records:
        created = datetime.fromisoformat(record["created_at"])
        if last_run <= created:
            remaining.append(record)
            continue
        if job.get("last_status") == "ok" and not job.get("last_delivery_error"):
            state.mark_delivery_success(record["idempotency_key"], last_run)
            for cluster in record.get("clusters", []):
                state.mark_cluster_delivered(
                    TARGET,
                    cluster["cluster_id"],
                    cluster["content_hash"],
                    last_run,
                )
            confirmed += 1
        else:
            state.mark_delivery_failure(record["idempotency_key"])
            failed += 1
    _write_json(pending_path, remaining)
    return {"confirmed": confirmed, "failed": failed, "remaining": len(remaining)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--reconcile-only", action="store_true")
    parser.add_argument("--mark-pending-failed", action="store_true")
    args = parser.parse_args(argv)
    home = args.home.resolve()
    hermes_home = (args.hermes_home or Path(os.environ.get("HERMES_HOME", ""))).resolve()
    if not str(hermes_home):
        raise ValueError("HERMES_HOME is required")
    state = StateStore(home / "data" / "state.sqlite3")
    pending_path = home / "data" / "pending-deliveries.json"
    jobs_path = hermes_home / "cron" / "jobs.json"
    if args.mark_pending_failed:
        records = _read_json(pending_path, [])
        for record in records:
            state.mark_delivery_failure(record["idempotency_key"])
        _write_json(pending_path, [])
        print(json.dumps({"marked_failed": len(records)}))
        return 0
    reconciliation = reconcile_pending(
        state=state, pending_path=pending_path, jobs_path=jobs_path
    )
    if args.reconcile_only:
        print(json.dumps(reconciliation, ensure_ascii=False))
        return 0

    pipeline = build_pipeline(home)
    result = pipeline.run(
        RunRequest(
            mode="scheduled",
            news_limit=10,
            review_limit=5,
            cost_mode="immediate",
        )
    )
    if not result.delivery_eligible:
        print("JNBY Daily Intelligence: all sources failed; delivery suppressed", file=sys.stderr)
        return 3
    key = hashlib.sha256(f"{TARGET}:{result.report_id}".encode()).hexdigest()
    if not state.record_delivery(result.report_id, TARGET, key):
        try:
            existing = state.get_delivery(key)
        except KeyError:
            return 0
        if existing["status"] != "failed" or not state.retry_failed_delivery(key):
            return 0
    pending = _read_json(pending_path, [])
    pending.append(
        {
            "report_id": result.report_id,
            "idempotency_key": key,
            "created_at": result.generated_at.isoformat(),
            "clusters": [
                {"cluster_id": item.cluster_id, "content_hash": item.content_hash}
                for item in result.news
            ],
        }
    )
    _write_json(pending_path, pending)
    print("\n\n--- PAGE BREAK ---\n\n".join(result.feishu_pages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
