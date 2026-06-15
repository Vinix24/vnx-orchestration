"""worker_runner — minimal queue runner for benchmark task 01.

Queue list resolved at import time via load_queues():
  1. env var WORKER_QUEUES (comma-separated) wins if set
  2. config/worker_queues.yaml if present and parseable
  3. fallback: ["default"]
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

_DEFAULT_QUEUES = ["default"]
_CONFIG_PATH = Path("config/worker_queues.yaml")


def load_queues():
    env_val = os.environ.get("WORKER_QUEUES")
    if env_val:
        return [q.strip() for q in env_val.split(",") if q.strip()]

    if _CONFIG_PATH.is_file():
        try:
            data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
            queues = (data or {}).get("queues")
            if isinstance(queues, list) and queues:
                return queues
        except yaml.YAMLError:
            return list(_DEFAULT_QUEUES)

    return list(_DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
