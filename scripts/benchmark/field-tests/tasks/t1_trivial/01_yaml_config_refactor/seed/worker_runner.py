"""worker_runner — minimal queue runner for benchmark task 01.

The hardcoded QUEUES list below must be refactored to load from a YAML
config file (`config/worker_queues.yaml`) with env-var override
(`WORKER_QUEUES` comma-separated) and a safe fallback.

Public contract:
    worker_runner.QUEUES  -> list[str]  (resolved at import time)
"""
from __future__ import annotations


QUEUES = ["default", "scoring", "ingestion", "indexing"]


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
