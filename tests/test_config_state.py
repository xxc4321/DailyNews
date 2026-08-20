from datetime import UTC, datetime
from pathlib import Path

import pytest

from jnby_news_watch.config import initialize_runtime
from jnby_news_watch.models import RunRequest
from jnby_news_watch.state import StateStore


ROOT = Path(__file__).parents[1]
SKILL_ROOT = ROOT / "skills" / "jnby-news-watch"


def test_runtime_is_initialized_outside_skill(tmp_path: Path) -> None:
    cfg = initialize_runtime(SKILL_ROOT, tmp_path)
    assert cfg.home == tmp_path
    assert (tmp_path / "config" / "profile.yaml").is_file()
    assert (tmp_path / "config" / "sources.yaml").is_file()
    assert (tmp_path / "data" / "state.sqlite3").is_file()
    assert not (SKILL_ROOT / "state.sqlite3").exists()


def test_runtime_initialization_does_not_overwrite_user_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    profile = config_dir / "profile.yaml"
    profile.write_text("version: user-owned\n", encoding="utf-8")
    initialize_runtime(SKILL_ROOT, tmp_path)
    assert profile.read_text(encoding="utf-8") == "version: user-owned\n"


def test_run_request_validates_limits_and_timezone() -> None:
    RunRequest(mode="manual", news_limit=20, review_limit=10).validate()
    with pytest.raises(ValueError, match="news_limit"):
        RunRequest(mode="manual", news_limit=0).validate()
    with pytest.raises(ValueError, match="timezone-aware"):
        RunRequest(mode="manual", since=datetime(2026, 8, 20)).validate()


def test_delivery_success_is_idempotent(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    assert store.record_delivery("digest-1", "feishu", "idem-1") is True
    assert store.record_delivery("digest-1", "feishu", "idem-1") is False
    store.mark_delivery_success("idem-1", datetime(2026, 8, 20, 0, 0, tzinfo=UTC))
    assert store.last_successful_delivery("feishu") == datetime(
        2026, 8, 20, 0, 0, tzinfo=UTC
    )


def test_run_lifecycle_persists_metrics(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    request = RunRequest(mode="scheduled")
    run_id = store.begin_run(request, "config-v1")
    store.finish_run(run_id, "completed", {"formal_news": 7})
    saved = store.get_run(run_id)
    assert saved["status"] == "completed"
    assert saved["metrics"]["formal_news"] == 7
