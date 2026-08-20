from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import yaml

from .models import RuntimeConfig
from .state import StateStore


DEFAULT_RUNTIME_NAME = ".jnby-news-watch"


def resolve_runtime_home(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    configured = os.environ.get("JNBY_NEWS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_NAME).resolve()


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be a mapping: {path}")
    return data


def initialize_runtime(
    skill_root: Path, runtime_home: Path | None = None
) -> RuntimeConfig:
    skill_root = skill_root.resolve()
    home = resolve_runtime_home(runtime_home)
    config_dir = home / "config"
    for path in (
        config_dir,
        home / "proposals" / "focus",
        home / "proposals" / "sources",
        home / "data",
        home / "reports",
        home / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)

    defaults = {
        "profile.yaml": skill_root / "assets" / "default-profile.yaml",
        "sources.yaml": skill_root / "assets" / "default-sources.yaml",
        "pricing.yaml": skill_root / "assets" / "deepseek-pricing.yaml",
    }
    for name, source in defaults.items():
        target = config_dir / name
        if not target.exists():
            shutil.copyfile(source, target)
    focus_path = config_dir / "focus.yaml"
    if not focus_path.exists():
        focus_path.write_text("version: 1\nactive: []\nhistory: []\n", encoding="utf-8")

    StateStore(home / "data" / "state.sqlite3")
    return RuntimeConfig(
        skill_root=skill_root,
        home=home,
        profile=_load_yaml(config_dir / "profile.yaml"),
        sources=_load_yaml(config_dir / "sources.yaml"),
        focus=_load_yaml(focus_path),
        pricing=_load_yaml(config_dir / "pricing.yaml"),
    )


def config_version(config: RuntimeConfig) -> str:
    payload = json.dumps(
        {
            "profile": config.profile,
            "sources": config.sources,
            "focus": config.focus,
            "pricing": config.pricing,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return sha256(payload).hexdigest()[:16]
