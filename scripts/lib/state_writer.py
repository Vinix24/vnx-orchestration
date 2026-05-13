"""Shared locked NDJSON append helper for state files."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

_SENTINEL_REGISTRY = {
    "dispatch_register.ndjson": ".state.lock",
    "receipts.ndjson": "append_receipt.lock",
    "t0_receipts.ndjson": "append_receipt.lock",
}


def _sentinel_path(data_path: Path) -> Path:
    lock_name = _SENTINEL_REGISTRY.get(
        data_path.name,
        f".{data_path.name}.sentinel.lock",
    )
    return data_path.parent / lock_name


def append_locked(path: Path, record: dict) -> None:
    """Append one JSON record under the shared sentinel and data-file locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = _sentinel_path(path)
    with sentinel.open("a+", encoding="utf-8") as sentinel_fh:
        fcntl.flock(sentinel_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")


__all__ = ["append_locked"]
