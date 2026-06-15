"""worker_runner — minimal queue runner for benchmark task 01.

Queue names are resolved at import time via load_queues():
  1. env var WORKER_QUEUES (comma-separated) wins if set
  2. config/worker_queues.yaml if present and parseable
  3. ["default"] as final fallback
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT_QUEUES = ["default"]


def load_queues() -> list[str]:
    env_val = os.environ.get("WORKER_QUEUES")
    if env_val:
        return [q.strip() for q in env_val.split(",") if q.strip()]

    yaml_path = Path("config/worker_queues.yaml")
    if yaml_path.is_file():
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("queues"), list):
                return data["queues"]
        except yaml.YAMLError:
            return list(_DEFAULT_QUEUES)

    return list(_DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
