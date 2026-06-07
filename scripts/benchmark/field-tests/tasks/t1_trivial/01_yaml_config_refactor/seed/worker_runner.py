"""worker_runner — minimal queue runner for benchmark task 01.

Seed state: queue names are hardcoded below. The assignment (see
instruction.md) is to move them to config/worker_queues.yaml with an
env-var override and a graceful default — tests/test_worker_runner.py
encodes that contract and fails against this seed.
"""
from __future__ import annotations

QUEUES = ["default", "scoring", "ingestion", "indexing"]


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
