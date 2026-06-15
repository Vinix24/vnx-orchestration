"""worker_runner — minimal queue runner for benchmark task 01.

Queue names resolve at import time from (in order): the WORKER_QUEUES env
var (comma-separated), then config/worker_queues.yaml, then a safe default.
See instruction.md for the full contract; tests/test_worker_runner.py
encodes it.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULT_QUEUES = ["default"]
CONFIG_PATH = Path("config/worker_queues.yaml")


def load_queues() -> list[str]:
    """Resolve the active queue list.

    Resolution order:
      1. WORKER_QUEUES env var (comma-separated) wins when it yields names.
      2. config/worker_queues.yaml ``queues:`` list when the file parses.
      3. Fall back to ["default"] when neither yields a usable list.

    Malformed YAML and a missing config file both degrade to the default
    rather than raising, so an operator typo never takes the runner down.
    """
    env_value = os.environ.get("WORKER_QUEUES")
    if env_value is not None:
        env_queues = [q.strip() for q in env_value.split(",") if q.strip()]
        if env_queues:
            return env_queues

    try:
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return list(DEFAULT_QUEUES)
    except yaml.YAMLError:
        return list(DEFAULT_QUEUES)

    queues = data.get("queues") if isinstance(data, dict) else None
    if isinstance(queues, list) and queues:
        return [str(q) for q in queues]
    return list(DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
