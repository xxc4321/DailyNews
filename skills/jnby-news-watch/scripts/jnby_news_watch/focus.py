from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import yaml


@dataclass(frozen=True)
class FocusRecord:
    id: str
    label: str
    terms: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    decay_days: int = 7
    strength: float = 100.0
    status: str = "proposed"
    notes: str = ""

    def validate(self) -> None:
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("focus dates must be timezone-aware")
        if self.valid_from >= self.valid_until:
            raise ValueError("focus validity window is invalid")
        if not self.terms:
            raise ValueError("focus requires at least one term")
        if self.decay_days < 0:
            raise ValueError("decay_days must not be negative")
        if not 0 <= self.strength <= 100:
            raise ValueError("strength must be between 0 and 100")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["terms"] = list(self.terms)
        result["valid_from"] = self.valid_from.isoformat()
        result["valid_until"] = self.valid_until.isoformat()
        return result

    @classmethod
    def from_dict(cls, payload: dict) -> "FocusRecord":
        return cls(
            id=str(payload["id"]),
            label=str(payload["label"]),
            terms=tuple(str(value) for value in payload.get("terms", ())),
            valid_from=datetime.fromisoformat(str(payload["valid_from"])),
            valid_until=datetime.fromisoformat(str(payload["valid_until"])),
            decay_days=int(payload.get("decay_days", 7)),
            strength=float(payload.get("strength", 100)),
            status=str(payload.get("status", "proposed")),
            notes=str(payload.get("notes", "")),
        )


def effective_focus_strength(focus: FocusRecord, at: datetime) -> float:
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if focus.status != "approved" or at < focus.valid_from or at >= focus.valid_until:
        return 0.0
    if focus.decay_days == 0:
        return focus.strength
    decay_start = focus.valid_until - timedelta(days=focus.decay_days)
    if at <= decay_start:
        return focus.strength
    remaining = (focus.valid_until - at).total_seconds()
    duration = timedelta(days=focus.decay_days).total_seconds()
    return max(0.0, focus.strength * remaining / duration)


class FocusStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.proposals_dir = self.root / "proposals" / "focus"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.active_path = self.root / "config" / "focus.yaml"
        self.history_path = self.root / "data" / "focus-history.sqlite3"
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS focus_history ("
                "history_id TEXT PRIMARY KEY, action TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.history_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        result = value or datetime.now(timezone.utc)
        if result.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return result

    def _write_yaml(self, path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        temporary.replace(path)

    def propose(
        self,
        label: str,
        *,
        terms: list[str],
        days: int = 30,
        decay_days: int = 7,
        strength: float = 100,
        notes: str = "",
        now: datetime | None = None,
    ) -> FocusRecord:
        start = self._now(now)
        focus = FocusRecord(
            id=f"focus-{uuid4().hex[:12]}",
            label=label,
            terms=tuple(dict.fromkeys(term.strip() for term in terms if term.strip())),
            valid_from=start,
            valid_until=start + timedelta(days=days),
            decay_days=decay_days,
            strength=strength,
            status="proposed",
            notes=notes,
        )
        return self.propose_record(focus)

    def propose_record(self, focus: FocusRecord) -> FocusRecord:
        proposed = replace(focus, status="proposed")
        proposed.validate()
        self._write_yaml(self.proposals_dir / f"{proposed.id}.yaml", proposed.to_dict())
        return proposed

    def _load_proposal(self, focus_id: str) -> FocusRecord:
        path = self.proposals_dir / f"{focus_id}.yaml"
        if not path.exists():
            raise KeyError(f"unknown focus proposal: {focus_id}")
        return FocusRecord.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    def _load_active_all(self) -> list[FocusRecord]:
        if not self.active_path.exists():
            return []
        payload = yaml.safe_load(self.active_path.read_text(encoding="utf-8")) or {}
        return [FocusRecord.from_dict(item) for item in payload.get("focuses", [])]

    def _save_active(self, focuses: list[FocusRecord]) -> None:
        self._write_yaml(
            self.active_path,
            {"version": 1, "focuses": [focus.to_dict() for focus in focuses]},
        )

    def _snapshot(self, action: str, now: datetime) -> str:
        history_id = uuid4().hex
        payload = {"focuses": [focus.to_dict() for focus in self._load_active_all()]}
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO focus_history VALUES (?, ?, ?, ?)",
                (history_id, action, json.dumps(payload, ensure_ascii=False), now.isoformat()),
            )
        return history_id

    def approve(self, focus_id: str, *, now: datetime | None = None) -> str:
        approved_at = self._now(now)
        proposal = self._load_proposal(focus_id)
        approved = replace(proposal, status="approved")
        active = [focus for focus in self._load_active_all() if focus.id != focus_id]
        active.append(approved)
        self._save_active(active)
        return self._snapshot(f"approve:{focus_id}", approved_at)

    def disable(self, focus_id: str, *, now: datetime | None = None) -> str:
        disabled_at = self._now(now)
        active = [focus for focus in self._load_active_all() if focus.id != focus_id]
        self._save_active(active)
        return self._snapshot(f"disable:{focus_id}", disabled_at)

    def rollback(self, history_id: str, *, now: datetime | None = None) -> str:
        rolled_back_at = self._now(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM focus_history WHERE history_id = ?", (history_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown focus history: {history_id}")
        payload = json.loads(row["payload_json"])
        focuses = [FocusRecord.from_dict(item) for item in payload.get("focuses", [])]
        self._save_active(focuses)
        return self._snapshot(f"rollback:{history_id}", rolled_back_at)

    def active(self, *, at: datetime | None = None) -> list[FocusRecord]:
        checked_at = self._now(at)
        return [
            focus
            for focus in self._load_active_all()
            if effective_focus_strength(focus, checked_at) > 0
        ]

    def proposals(self) -> list[FocusRecord]:
        records = []
        for path in sorted(self.proposals_dir.glob("*.yaml")):
            records.append(
                FocusRecord.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
            )
        return records
