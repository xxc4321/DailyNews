from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path


JOB_NAME = "JNBY Daily Intelligence"


@dataclass(frozen=True)
class CronDesired:
    name: str = JOB_NAME
    schedule: str = "0 8 * * *"
    script: str = "jnby-news-watch.py"
    no_agent: bool = True
    deliver: str = "feishu"
    workdir: str = "E:\\My_workspace\\JNBY"
    enabled: bool = True


@dataclass(frozen=True)
class CronPlan:
    action: str
    job_id: str | None
    changes: tuple[str, ...] = ()


def _schedule(job: dict) -> str:
    value = job.get("schedule", "")
    if isinstance(value, dict):
        return str(value.get("expr") or value.get("expression") or "")
    return str(value)


def reconcile_cron(jobs: list[dict], desired: CronDesired | None = None) -> CronPlan:
    wanted = desired or CronDesired()
    matches = [job for job in jobs if job.get("name") == wanted.name]
    if len(matches) > 1:
        raise ValueError(
            f"multiple cron jobs named {wanted.name!r}; refusing to modify or delete any"
        )
    if not matches:
        return CronPlan("create", None)
    job = matches[0]
    changes = []
    checks = {
        "schedule": (_schedule(job), wanted.schedule),
        "script": (str(job.get("script") or ""), wanted.script),
        "no_agent": (bool(job.get("no_agent")), wanted.no_agent),
        "deliver": (str(job.get("deliver") or ""), wanted.deliver),
        "workdir": (str(job.get("workdir") or ""), wanted.workdir),
        "enabled": (bool(job.get("enabled")), wanted.enabled),
    }
    for field, (actual, expected) in checks.items():
        if actual != expected:
            changes.append(field)
    return CronPlan("update" if changes else "noop", str(job.get("id")), tuple(changes))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    payload = json.loads(args.jobs.read_text(encoding="utf-8-sig"))
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    plan = reconcile_cron(list(jobs), CronDesired(workdir=args.workdir))
    print(
        json.dumps(
            {"action": plan.action, "job_id": plan.job_id, "changes": list(plan.changes)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
