from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .models import RunRequest


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    config_version TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    target TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(target, idempotency_key)
);
CREATE TABLE IF NOT EXISTS news_items (
    item_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    cluster_id TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
    item_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    cluster_id TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS focus_history (
    history_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivered_clusters (
    target TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(target, cluster_id, content_hash)
);
"""


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def begin_run(self, request: RunRequest, config_version: str) -> str:
        request.validate()
        run_id = uuid4().hex
        now = datetime.now().astimezone().isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, request_json, config_version, status, started_at) VALUES (?, ?, ?, 'running', ?)",
                (run_id, json.dumps(request.to_dict(), ensure_ascii=False), config_version, now),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, metrics: dict) -> None:
        now = datetime.now().astimezone().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, metrics_json = ?, finished_at = ? WHERE run_id = ?",
                (status, json.dumps(metrics, ensure_ascii=False), now, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run_id: {run_id}")

    def get_run(self, run_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return {
            "run_id": row["run_id"],
            "request": json.loads(row["request_json"]),
            "config_version": row["config_version"],
            "status": row["status"],
            "metrics": json.loads(row["metrics_json"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def record_delivery(
        self, report_id: str, target: str, idempotency_key: str
    ) -> bool:
        now = datetime.now().astimezone().isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO deliveries(report_id, target, idempotency_key, created_at) VALUES (?, ?, ?, ?)",
                    (report_id, target, idempotency_key, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_delivery_success(
        self, idempotency_key: str, delivered_at: datetime
    ) -> None:
        if delivered_at.tzinfo is None or delivered_at.utcoffset() is None:
            raise ValueError("delivered_at must be timezone-aware")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'success', delivered_at = ? WHERE idempotency_key = ?",
                (delivered_at.isoformat(), idempotency_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown idempotency_key: {idempotency_key}")

    def mark_delivery_failure(self, idempotency_key: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'failed' WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown idempotency_key: {idempotency_key}")

    def get_delivery(self, idempotency_key: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown idempotency_key: {idempotency_key}")
        return dict(row)

    def retry_failed_delivery(self, idempotency_key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deliveries SET status = 'pending', delivered_at = NULL "
                "WHERE idempotency_key = ? AND status = 'failed'",
                (idempotency_key,),
            )
        return cursor.rowcount == 1

    def last_successful_delivery(self, target: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT delivered_at FROM deliveries WHERE target = ? AND status = 'success' ORDER BY delivered_at DESC LIMIT 1",
                (target,),
            ).fetchone()
        return datetime.fromisoformat(row["delivered_at"]) if row else None

    def upsert_item(
        self,
        *,
        channel: str,
        item_id: str,
        payload: dict,
        cluster_id: str,
        seen_at: datetime,
    ) -> None:
        if seen_at.tzinfo is None or seen_at.utcoffset() is None:
            raise ValueError("seen_at must be timezone-aware")
        table = {"news": "news_items", "review": "review_items"}.get(channel)
        if table is None:
            raise ValueError("unsupported item channel")
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {table}(item_id, payload_json, cluster_id, first_seen_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                "payload_json=excluded.payload_json, cluster_id=excluded.cluster_id",
                (
                    item_id,
                    json.dumps(payload, ensure_ascii=False),
                    cluster_id,
                    seen_at.isoformat(),
                ),
            )

    def upsert_cluster(
        self, *, cluster_id: str, channel: str, payload: dict, updated_at: datetime
    ) -> None:
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO clusters(cluster_id, channel, payload_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET channel=excluded.channel, "
                "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (
                    cluster_id,
                    channel,
                    json.dumps(payload, ensure_ascii=False),
                    updated_at.isoformat(),
                ),
            )

    def record_source_health(self, source_id: str, payload: dict, checked_at: datetime) -> None:
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO source_health(source_id, payload_json, checked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(source_id) DO UPDATE SET payload_json=excluded.payload_json, checked_at=excluded.checked_at",
                (source_id, json.dumps(payload, ensure_ascii=False), checked_at.isoformat()),
            )

    def mark_cluster_delivered(
        self,
        target: str,
        cluster_id: str,
        content_hash: str,
        delivered_at: datetime,
    ) -> None:
        if delivered_at.tzinfo is None or delivered_at.utcoffset() is None:
            raise ValueError("delivered_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO delivered_clusters VALUES (?, ?, ?, ?)",
                (target, cluster_id, content_hash, delivered_at.isoformat()),
            )

    def cluster_delivery_time(
        self, target: str, cluster_id: str, content_hash: str | None = None
    ) -> datetime | None:
        sql = "SELECT delivered_at FROM delivered_clusters WHERE target = ? AND cluster_id = ?"
        params: tuple = (target, cluster_id)
        if content_hash is not None:
            sql += " AND content_hash = ?"
            params += (content_hash,)
        sql += " ORDER BY delivered_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return datetime.fromisoformat(row["delivered_at"]) if row else None

    def get_cluster(self, cluster_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT channel, payload_json, updated_at FROM clusters WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown cluster_id: {cluster_id}")
        return {
            "cluster_id": cluster_id,
            "channel": row["channel"],
            "payload": json.loads(row["payload_json"]),
            "updated_at": row["updated_at"],
        }
