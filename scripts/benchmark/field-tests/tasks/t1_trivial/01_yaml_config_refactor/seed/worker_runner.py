"""worker_runner — minimal queue runner for benchmark task 01.

Queue names resolve in this order:
  1. env var WORKER_QUEUES (comma-separated)
  2. config/worker_queues.yaml (top-level `queues:` list, CWD-relative)
  3. ["default"]
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT_QUEUES = ["default"]
_CONFIG_PATH = Path("config") / "worker_queues.yaml"


def load_queues() -> list[str]:
    env_value = os.environ.get("WORKER_QUEUES")
    if env_value:
        parsed = [item.strip() for item in env_value.split(",") if item.strip()]
        if parsed:
            return parsed

    if _CONFIG_PATH.is_file():
        try:
            with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (yaml.YAMLError, OSError):
            return list(_DEFAULT_QUEUES)
        if isinstance(data, dict):
            queues = data.get("queues")
            if isinstance(queues, list) and all(isinstance(q, str) for q in queues):
                return list(queues)

    return list(_DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
