from __future__ import annotations

from datetime import datetime, timezone
import json

from jnby_news_watch.cli import build_parser, main, request_from_args
from jnby_news_watch.focus import FocusStore


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def test_digest_custom_counts_and_duration():
    args = build_parser().parse_args(
        [
            "digest",
            "--news",
            "20",
            "--reviews",
            "10",
            "--since",
            "7d",
            "--dry-run",
        ]
    )

    request = request_from_args(args, now=NOW, timezone_name="Asia/Shanghai")

    assert request.news_limit == 20
    assert request.review_limit == 10
    assert request.since == NOW.replace() - __import__("datetime").timedelta(days=7)
    assert request.dry_run is True


def test_temporary_focus_is_in_request_but_not_persisted():
    args = build_parser().parse_args(
        ["digest", "--focus", "Paris", "--focus", "logistics"]
    )
    request = request_from_args(args, now=NOW, timezone_name="Asia/Shanghai")
    assert request.focus_terms == ("Paris", "logistics")


def test_focus_proposal_requires_explicit_approval(tmp_path, capsys):
    exit_code = main(
        [
            "--home",
            str(tmp_path),
            "focus",
            "propose",
            "--text",
            "Paris store",
            "--term",
            "Paris",
            "--term",
            "store opening",
            "--days",
            "30",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "proposed"
    assert FocusStore(tmp_path).active(at=NOW) == []


def test_health_is_redacted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-api-value")
    monkeypatch.setenv("FEISHU_APP_SECRET", "super-secret-feishu-value")

    exit_code = main(["--home", str(tmp_path), "health", "--json"])

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)
    assert exit_code == 0
    assert payload["deepseek_api_key_configured"] is True
    assert "super-secret" not in stdout
    assert "super-secret" not in json.dumps(payload)
