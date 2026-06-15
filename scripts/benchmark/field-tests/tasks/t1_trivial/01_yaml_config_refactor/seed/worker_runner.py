"""worker_runner — minimal queue runner for benchmark task 01.

Queue names now live in config/worker_queues.yaml so operators can edit the
queue list without code changes. Resolution order (see load_queues):

  1. Env var WORKER_QUEUES (comma-separated) — highest priority.
  2. config/worker_queues.yaml (top-level ``queues:`` key) relative to CWD.
  3. Default ["default"] — used when neither source resolves a list.

The public interface is unchanged: ``worker_runner.QUEUES`` remains an
importable top-level list, assigned from ``load_queues()`` at import time.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path("config") / "worker_queues.yaml"
DEFAULT_QUEUES = ["default"]


def load_queues() -> list[str]:
    """Resolve the active queue list.

    Order: WORKER_QUEUES env var > config/worker_queues.yaml > default.
    A missing, unreadable, or malformed config falls back to the default
    rather than raising.
    """
    import os

    env_value = os.environ.get("WORKER_QUEUES")
    if env_value is not None:
        queues = [q.strip() for q in env_value.split(",") if q.strip()]
        if queues:
            return queues

    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (yaml.YAMLError, OSError):
            return list(DEFAULT_QUEUES)
        queues = (data or {}).get("queues") if isinstance(data, dict) else None
        if isinstance(queues, list) and queues:
            return [str(q) for q in queues]
        return list(DEFAULT_QUEUES)

    return list(DEFAULT_QUEUES)


QUEUES = load_queues()


def run_one(queue_name: str) -> str:
    """Stub runner — picks one item from named queue. Returns the queue name."""
    if queue_name not in QUEUES:
        raise ValueError(f"unknown queue: {queue_name}")
    return queue_name
