"""worker_runner — minimal queue runner for benchmark task 01.

Queue names resolve at import time via load_queues(): WORKER_QUEUES env
var wins, then config/worker_queues.yaml, then a ["default"] fallback.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_PATH = Path("config") / "worker_queues.yaml"
DEFAULT_QUEUES = ["default"]


def load_queues() -> list[str]:
    env_value = os.environ.get("WORKER_QUEUES")
    if env_value:
        parsed = [q.strip() for q in env_value.split(",") if q.strip()]
        if parsed:
            return parsed

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return list(DEFAULT_QUEUES)

    if isinstance(data, dict):
        queues = data.get("queues")
        if isinstance(queues, list) and queues:
            return [str(q) for q in queues]

    return list(DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
