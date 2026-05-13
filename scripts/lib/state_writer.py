"""Shared locked NDJSON append helper for state files."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable
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
    payload = (json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
    with sentinel.open("a+", encoding="utf-8") as sentinel_fh:
        fcntl.flock(sentinel_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0, os.SEEK_END)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())


def rewrite_locked(path: Path, new_content: bytes | Callable[[bytes], bytes]) -> None:
    """Rewrite a state file under the shared sentinel and data-file locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = _sentinel_path(path)
    with sentinel.open("a+", encoding="utf-8") as sentinel_fh:
        fcntl.flock(sentinel_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0)
            current_content = fh.read()
            rewritten = new_content(current_content) if callable(new_content) else new_content
            if not isinstance(rewritten, (bytes, bytearray)):
                raise TypeError("rewrite_locked requires bytes output")
            rewritten_bytes = bytes(rewritten)
            if rewritten_bytes == current_content:
                return

            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp_")
            try:
                with os.fdopen(fd, "wb") as tmp_fh:
                    tmp_fh.write(rewritten_bytes)
                    tmp_fh.flush()
                    os.fsync(tmp_fh.fileno())
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


__all__ = ["append_locked", "rewrite_locked"]
