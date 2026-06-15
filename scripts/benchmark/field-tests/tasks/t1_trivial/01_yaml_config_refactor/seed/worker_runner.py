"""worker_runner — minimal queue runner for benchmark task 01.

Queue names are resolved at import time via :func:`load_queues`:

1. ``WORKER_QUEUES`` env var (comma-separated) wins if set.
2. Else ``config/worker_queues.yaml`` (relative to CWD) is parsed; the
   list under the top-level ``queues:`` key is used.
3. Else (file missing, malformed, or otherwise unreadable) the default
   ``["default"]`` is returned.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


def load_queues() -> list[str]:
    env_value = os.environ.get("WORKER_QUEUES")
    if env_value:
        return [q.strip() for q in env_value.split(",") if q.strip()]

    config_path = Path("config") / "worker_queues.yaml"
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            queues = (data or {}).get("queues")
            if isinstance(queues, list) and queues:
                return [str(q) for q in queues]
        except (yaml.YAMLError, OSError):
            pass

    return ["default"]


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
