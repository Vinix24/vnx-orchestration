"""atomic_io.py — Shared atomic file write and NDJSON append helpers.

Consolidates the tmp+os.replace pattern (4 inline implementations across
state_writer.py, worker_permission_relay.py, tmux_interactive_dispatch.py,
intelligence_dashboard.py) and the fcntl.flock NDJSON append used in
governance_emit.py.

ADR-005: ledger-first write ordering — appenders raise on OSError, never
silently drop events.

ADR-021: exception discipline — broad except only with explicit log+re-raise
or # noqa: vnx-silent-except with reason= string. AttributeError never
caught silently.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text file atomically via temp file + os.replace.

    Writes to a sibling temp file in the same directory, fsyncs, then
    renames atomically. Preserves the target file's permission mode if the
    target already exists. Cleans up the temp file on any failure.

    Raises:
        OSError: on write or rename failure (never partially-overwrites target)
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_mode: int | None = None
    if path.exists():
        existing_mode = stat.S_IMODE(path.stat().st_mode)

    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())

        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)

        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: dict, indent: int = 2) -> None:
    """Write JSON file atomically via temp file + os.replace.

    Delegates to atomic_write_text. Raises OSError on write failure.
    """
    atomic_write_text(path, json.dumps(payload, indent=indent, ensure_ascii=False))


def audit_event_append(events_dir: Path, event_type: str, payload: dict) -> None:
    """Append one audit event line to events_dir/<event_type>.ndjson.

    Auto-injects timestamp, pid, and actor fields into the record. Uses
    fcntl.flock(LOCK_EX) for concurrent-writer safety. Creates the events
    directory if absent.

    ADR-005: raises OSError on write failure — never silently drops events.

    Args:
        events_dir: directory for event NDJSON files (e.g. .vnx-data/events/)
        event_type: determines the filename (<event_type>.ndjson)
        payload: caller-supplied fields merged into the event record
    """
    events_dir.mkdir(parents=True, exist_ok=True)
    target = events_dir / f"{event_type}.ndjson"

    record: dict = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
        "actor": os.environ.get("VNX_ACTOR", "system"),
        **payload,
    }

    with open(target, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
