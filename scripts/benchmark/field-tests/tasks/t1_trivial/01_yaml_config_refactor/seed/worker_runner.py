"""worker_runner — minimal queue runner for benchmark task 01.

Queue names are resolved at import time by :func:`load_queues`. Resolution
order (first match wins):

1. ``WORKER_QUEUES`` env var (comma-separated string).
2. ``config/worker_queues.yaml`` (top-level ``queues:`` list), located next
   to this module.
3. Default fallback of ``["default"]``.

A missing config file, a malformed YAML file, or a config without a usable
``queues`` list all degrade gracefully to the default rather than crashing.
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_QUEUES = ["default"]
CONFIG_PATH = Path(__file__).resolve().parent / "config" / "worker_queues.yaml"


def _queues_from_env() -> list[str] | None:
    """Parse ``WORKER_QUEUES`` (comma-separated) into a list, or None if unset."""
    import os

    raw = os.environ.get("WORKER_QUEUES")
    if raw is None:
        return None
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return parsed or None


def _queues_from_yaml() -> list[str] | None:
    """Read the YAML config's ``queues:`` list, or None if absent/malformed."""
    if not CONFIG_PATH.exists():
        return None
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    queues = data.get("queues")
    if not isinstance(queues, list) or not queues:
        return None
    return [str(item) for item in queues]


def load_queues() -> list[str]:
    """Resolve the queue list following env -> yaml -> default precedence."""
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
