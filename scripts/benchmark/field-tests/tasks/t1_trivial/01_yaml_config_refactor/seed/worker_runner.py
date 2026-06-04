"""worker_runner — minimal queue runner for benchmark task 01.

Public contract:
    worker_runner.QUEUES  -> list[str]  (resolved at import time)

Queue resolution order (highest precedence first):
    1. WORKER_QUEUES env var (comma-separated)
    2. config/worker_queues.yaml  (top-level ``queues:`` key)
    3. ["default"]
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


_DEFAULT: list[str] = ["default"]
_CONFIG_PATH = Path("config/worker_queues.yaml")


def _from_env() -> list[str] | None:
    """Parse WORKER_QUEUES if set; return None when unset/empty."""
    raw = os.environ.get("WORKER_QUEUES")
    if not raw:
        return None
    queues = [q.strip() for q in raw.split(",") if q.strip()]
    return queues or None


def _from_yaml() -> list[str] | None:
    """Parse the YAML config if present and valid; return None otherwise."""
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    queues = data.get("queues")
    if not isinstance(queues, list) or not queues:
        return None
    return [str(q) for q in queues]


def load_queues() -> list[str]:
    """Resolve the active queue list from env, then YAML config, then default."""
    return _from_env() or _from_yaml() or list(_DEFAULT)


QUEUES: list[str] = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
