from datetime import datetime, timedelta, timezone
import json

from jnby_news_watch.scheduled_runner import reconcile_pending
from jnby_news_watch.state import StateStore


def test_previous_successful_hermes_delivery_is_confirmed(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    created = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    key = "idem-1"
    assert state.record_delivery("report-1", "feishu-private", key)
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            [
                {
                    "report_id": "report-1",
                    "idempotency_key": key,
                    "created_at": created.isoformat(),
                    "clusters": [{"cluster_id": "cluster-1", "content_hash": "hash-1"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "JNBY Daily Intelligence",
                        "last_run_at": (created + timedelta(minutes=2)).isoformat(),
                        "last_status": "ok",
                        "last_delivery_error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_pending(state=state, pending_path=pending, jobs_path=jobs)

    assert result == {"confirmed": 1, "failed": 0, "remaining": 0}
    assert state.get_delivery(key)["status"] == "success"
    assert state.cluster_delivery_time("feishu-private", "cluster-1", "hash-1") is not None


def test_failed_delivery_is_not_marked_success(tmp_path):
    state = StateStore(tmp_path / "state.sqlite3")
    created = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    key = "idem-failed"
    state.record_delivery("report-failed", "feishu-private", key)
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            [
                {
                    "report_id": "report-failed",
                    "idempotency_key": key,
                    "created_at": created.isoformat(),
                    "clusters": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "JNBY Daily Intelligence",
                        "last_run_at": (created + timedelta(minutes=2)).isoformat(),
                        "last_status": "error",
                        "last_delivery_error": "platform rejected message",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_pending(state=state, pending_path=pending, jobs_path=jobs)

    assert result["failed"] == 1
    assert state.get_delivery(key)["status"] == "failed"
    assert state.cluster_delivery_time("feishu-private", "cluster-1") is None
    assert state.retry_failed_delivery(key) is True
    assert state.get_delivery(key)["status"] == "pending"
    assert state.retry_failed_delivery(key) is False
