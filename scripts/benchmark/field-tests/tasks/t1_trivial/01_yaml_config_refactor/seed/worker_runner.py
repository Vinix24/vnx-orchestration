"""worker_runner — minimal queue runner for benchmark task 01.

Public contract:
    worker_runner.QUEUES  -> list[str]  (resolved at import time)
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml


_DEFAULT = ["default"]
_CONFIG_PATH = Path("config/worker_queues.yaml")


def load_queues() -> list[str]:
    env_val = os.environ.get("WORKER_QUEUES")
    if env_val:
        return [q.strip() for q in env_val.split(",") if q.strip()]
    if _CONFIG_PATH.exists():
        try:
            data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
            return list(data["queues"])
        except Exception:
            return _DEFAULT
    return _DEFAULT


QUEUES: list[str] = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
