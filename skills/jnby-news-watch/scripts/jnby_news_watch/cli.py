from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
from zoneinfo import ZoneInfo

from .config import initialize_runtime
from .cost import is_off_peak, next_off_peak
from .focus import FocusStore
from .models import RunRequest
from .pipeline import Pipeline
from .render import digest_to_dict
from .security import SafeFetcher
from .sources import (
    BlueskyPublicAdapter,
    CsvReviewAdapter,
    GdeltAdapter,
    GoogleNewsRssAdapter,
    JsonReviewAdapter,
    PageLinksAdapter,
    PublicPostDiscoveryAdapter,
    RedditOAuthAdapter,
    RssAdapter,
    TavilyAdapter,
    YouTubeApiAdapter,
)
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jnby-news-watch",
        description="Verified JNBY news and Customer Voice intelligence.",
    )
    parser.add_argument("--home", type=Path, help="Runtime state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    digest = subparsers.add_parser("digest", help="Generate a ranked intelligence digest")
    digest.add_argument("--news", type=int, default=10, help="Formal news count (1-100)")
    digest.add_argument("--reviews", type=int, default=5, help="Customer Voice count (0-100)")
    digest.add_argument("--since", help="Duration such as 24h/7d, ISO date, or timestamp")
    digest.add_argument("--until", help="ISO date or timestamp")
    digest.add_argument("--focus", action="append", default=[], help="Temporary focus term; repeatable")
    digest.add_argument(
        "--cost-mode", choices=("immediate", "budget", "urgent"), default="immediate"
    )
    digest.add_argument("--scheduled", action="store_true", help="Suppress unchanged delivered events")
    digest.add_argument("--dry-run", action="store_true", help="Never confirm external delivery")
    digest.add_argument("--json", action="store_true", help="Print machine-readable result")

    focus = subparsers.add_parser("focus", help="Propose or manage dynamic work focus")
    focus_commands = focus.add_subparsers(dest="focus_command", required=True)
    propose = focus_commands.add_parser("propose")
    propose.add_argument("--text", required=True, help="Human-readable focus label")
    propose.add_argument("--term", action="append", default=[], help="Weighted match term; repeatable")
    propose.add_argument("--days", type=int, default=30)
    propose.add_argument("--decay-days", type=int, default=7)
    propose.add_argument("--strength", type=float, default=100)
    propose.add_argument("--notes", default="")
    for name in ("approve", "disable"):
        command = focus_commands.add_parser(name)
        command.add_argument("id")
    rollback = focus_commands.add_parser("rollback")
    rollback.add_argument("history_id")
    listing = focus_commands.add_parser("list")
    listing.add_argument("--active", action="store_true")

    deepen = subparsers.add_parser("deepen", help="Show one stored event/theme record")
    deepen.add_argument("id")
    deepen.add_argument("--json", action="store_true")

    health = subparsers.add_parser("health", help="Show redacted integration health")
    health.add_argument("--json", action="store_true")
    return parser


def _parse_time(value: str | None, *, now: datetime, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    duration = value.strip().lower()
    if duration.endswith("h") and duration[:-1].isdigit():
        return now - timedelta(hours=int(duration[:-1]))
    if duration.endswith("d") and duration[:-1].isdigit():
        return now - timedelta(days=int(duration[:-1]))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def request_from_args(
    args: argparse.Namespace,
    *,
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> RunRequest:
    called_at = now or datetime.now(timezone.utc)
    request = RunRequest(
        mode="scheduled" if args.scheduled else "manual",
        news_limit=args.news,
        review_limit=args.reviews,
        since=_parse_time(args.since, now=called_at, timezone_name=timezone_name),
        until=_parse_time(args.until, now=called_at, timezone_name=timezone_name),
        focus_terms=tuple(args.focus),
        cost_mode=args.cost_mode,
        dry_run=args.dry_run,
    )
    request.validate()
    return request


def _review_salt(home: Path) -> str:
    path = home / "data" / "review-author-salt"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="ascii")
    return path.read_text(encoding="ascii").strip()


def _adapters(home: Path) -> dict:
    fetcher = SafeFetcher()
    return {
        "rss": RssAdapter(fetcher),
        "page_links": PageLinksAdapter(fetcher),
        "google_news_rss": GoogleNewsRssAdapter(fetcher),
        "gdelt": GdeltAdapter(fetcher),
        "json_review": JsonReviewAdapter(fetcher, author_salt=_review_salt(home)),
        "csv_review": CsvReviewAdapter(fetcher, author_salt=_review_salt(home)),
        "public_post_discovery": PublicPostDiscoveryAdapter(fetcher),
        "tavily": TavilyAdapter(),
        "bluesky_public": BlueskyPublicAdapter(),
        "youtube_api": YouTubeApiAdapter(),
        "reddit_oauth": RedditOAuthAdapter(),
    }


def build_pipeline(home: Path | None = None) -> Pipeline:
    skill_root = Path(__file__).parents[2]
    config = initialize_runtime(skill_root, home)
    state = StateStore(config.home / "data" / "state.sqlite3")
    return Pipeline(config, state, _adapters(config.home))


def _configured_keys_from_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() and value.strip():
                keys.add(key.strip())
    except OSError:
        pass
    return keys


def health_report(home: Path | None = None) -> dict:
    skill_root = Path(__file__).parents[2]
    config = initialize_runtime(skill_root, home)
    hermes_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser()
    file_keys = set()
    for candidate in (hermes_home / ".env", hermes_home / "config" / ".env"):
        file_keys.update(_configured_keys_from_file(candidate))

    def present(key: str) -> bool:
        return bool(os.environ.get(key)) or key in file_keys

    now = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "runtime_home": str(config.home),
        "state_database_exists": (config.home / "data" / "state.sqlite3").is_file(),
        "configured_sources": len(config.sources.get("sources", [])),
        "approved_sources": sum(
            1 for source in config.sources.get("sources", []) if source.get("approved")
        ),
        "deepseek_api_key_configured": present("DEEPSEEK_API_KEY"),
        "deepseek_model": "deepseek-v4-flash",
        "current_pricing_period": "off_peak" if is_off_peak(now) else "peak",
        "next_off_peak": next_off_peak(now).isoformat(),
        "hermes_executable_found": shutil.which("hermes") is not None,
        "hermes_home_exists": hermes_home.is_dir(),
        "feishu_app_id_configured": present("FEISHU_APP_ID"),
        "feishu_app_secret_configured": present("FEISHU_APP_SECRET"),
        "feishu_home_channel_configured": present("FEISHU_HOME_CHANNEL"),
        "secrets_redacted": True,
    }


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _run_focus(args: argparse.Namespace, home: Path) -> int:
    store = FocusStore(home)
    command = args.focus_command
    if command == "propose":
        if args.days <= 0:
            raise ValueError("days must be positive")
        terms = args.term or [args.text]
        result = store.propose(
            args.text,
            terms=terms,
            days=args.days,
            decay_days=args.decay_days,
            strength=args.strength,
            notes=args.notes,
        )
        _print_json(result.to_dict())
    elif command == "approve":
        _print_json({"history_id": store.approve(args.id), "approved": args.id})
    elif command == "disable":
        _print_json({"history_id": store.disable(args.id), "disabled": args.id})
    elif command == "rollback":
        _print_json(
            {"history_id": store.rollback(args.history_id), "restored": args.history_id}
        )
    elif command == "list":
        records = store.active() if args.active else store.proposals()
        _print_json({"items": [record.to_dict() for record in records]})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        skill_root = Path(__file__).parents[2]
        config = initialize_runtime(skill_root, args.home)
        if args.command == "digest":
            request = request_from_args(
                args, timezone_name=config.profile.get("timezone", "Asia/Shanghai")
            )
            result = build_pipeline(config.home).run(request)
            if args.json:
                _print_json(digest_to_dict(result))
            else:
                print("\n\n--- PAGE BREAK ---\n\n".join(result.feishu_pages))
            return 0 if result.delivery_eligible else 3
        if args.command == "focus":
            return _run_focus(args, config.home)
        if args.command == "deepen":
            state = StateStore(config.home / "data" / "state.sqlite3")
            record = state.get_cluster(args.id)
            if args.json:
                _print_json(record)
            else:
                print(f"# {record['channel']} {record['cluster_id']}\n\n{json.dumps(record['payload'], ensure_ascii=False, indent=2)}")
            return 0
        if args.command == "health":
            report = health_report(config.home)
            if args.json:
                _print_json(report)
            else:
                for key, value in report.items():
                    print(f"{key}: {value}")
            return 0
        parser.error("unknown command")
    except (KeyError, OSError, ValueError) as exc:
        print(f"jnby-news-watch error: {exc}", file=sys.stderr)
        return 2
    return 2
