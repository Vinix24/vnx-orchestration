"""worker_runner — minimal queue runner for benchmark task 01.

Queue names are no longer hardcoded. They resolve at import time via
``load_queues()`` with the following precedence:

    1. env var ``WORKER_QUEUES`` (comma-separated) — operator override
    2. ``config/worker_queues.yaml`` (top-level ``queues:`` list)
    3. graceful default ``["default"]``

A missing or malformed YAML file never crashes the runner; it falls back
to the default. ``worker_runner.QUEUES`` remains importable as a top-level
list attribute for backwards compatibility.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# Config lives next to this module so resolution works from any CWD.
_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "worker_queues.yaml"

DEFAULT_QUEUES = ["default"]


def _queues_from_env() -> list[str] | None:
    """Parse WORKER_QUEUES (comma-separated). Returns None if unset/empty."""
    raw = os.environ.get("WORKER_QUEUES")
    if raw is None:
        return None
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return parsed or None


def _queues_from_yaml(path: Path = _CONFIG_PATH) -> list[str] | None:
    """Parse the YAML config. Returns None if absent, malformed, or invalid."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    queues = data.get("queues")
    if not isinstance(queues, list) or not queues:
        return None
    return [str(q) for q in queues]


def load_queues() -> list[str]:
    """Resolve queue names: env var > YAML config > default ``["default"]``."""
    env_queues = _queues_from_env()
    if env_queues is not None:
        return env_queues
    yaml_queues = _queues_from_yaml()
    if yaml_queues is not None:
        return yaml_queues
    return list(DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
