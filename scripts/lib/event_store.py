#!/usr/bin/env python3
"""EventStore — NDJSON persistence for agent stream events.

Stores one NDJSON file per terminal at .vnx-data/events/{terminal}.ndjson.
Supports atomic append, tail-with-since, and clear (per-dispatch retention).

File locking via fcntl.flock ensures concurrent write safety.

BILLING SAFETY: No Anthropic SDK imports. Local filesystem only.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# Size warning threshold (10 MB per contract)
_SIZE_WARNING_BYTES = 10 * 1024 * 1024


def _events_dir() -> Path:
    """Resolve the events directory from environment or default."""
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data).expanduser().resolve() / "events"
    # Fallback: .vnx-data/events relative to repo root
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent / ".vnx-data" / "events"


class EventStore:
    """NDJSON event store with per-terminal files and file locking."""

    def __init__(self, events_dir: Optional[Path] = None) -> None:
        self._events_dir = events_dir or _events_dir()
        self._sequences: Dict[str, int] = {}

    def _terminal_path(self, terminal: str) -> Path:
        return self._events_dir / f"{terminal}.ndjson"

    def _next_sequence(self, terminal: str) -> int:
        seq = self._sequences.get(terminal, 0) + 1
        self._sequences[terminal] = seq
        return seq

    def append(self, terminal: str, event: Dict[str, Any], dispatch_id: str = "") -> None:
        """Append a single event as an atomic NDJSON line.

        Uses LOCK_EX for write safety. The line is written in a single write()
        call including the trailing newline to prevent partial reads.
        """
        self._events_dir.mkdir(parents=True, exist_ok=True)
        path = self._terminal_path(terminal)

        # Build envelope per contract
        envelope: Dict[str, Any] = {
            "type": event.get("type", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "dispatch_id": dispatch_id or event.get("dispatch_id", ""),
            "terminal": terminal,
            "sequence": self._next_sequence(terminal),
            "data": event.get("data", event),
        }

        line = json.dumps(envelope, separators=(",", ":")) + "\n"

        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # Size warning
        try:
            if path.stat().st_size > _SIZE_WARNING_BYTES:
                logger.warning(
                    "event_store: %s exceeds %d bytes — operator intervention recommended",
                    path,
                    _SIZE_WARNING_BYTES,
                )
        except OSError:
            pass

    def tail(self, terminal: str, since: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Yield events since a timestamp (ISO 8601 string).

        Uses LOCK_SH for read safety. Events are yielded in file order.
        If since is None, all events are returned.
        """
        path = self._terminal_path(terminal)
        if not path.exists():
            return

        with open(path, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                for raw_line in f:
                    raw_line = raw_line.rstrip("\n")
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line)
                    except json.JSONDecodeError:
                        logger.warning("event_store: malformed line in %s (skipped)", path)
                        continue

                    if since and event.get("timestamp", "") <= since:
                        continue

                    yield event
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def clear(self, terminal: str) -> None:
        """Truncate the event file for a terminal (new dispatch clears old events).

        Also resets the sequence counter.
        """
        path = self._terminal_path(terminal)
        self._sequences.pop(terminal, None)

        if not path.exists():
            return

        with open(path, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.truncate(0)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def event_count(self, terminal: str) -> int:
        """Count events in the NDJSON file for a terminal."""
        path = self._terminal_path(terminal)
        if not path.exists():
            return 0
        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def last_event(self, terminal: str) -> Optional[Dict[str, Any]]:
        """Return the last event for a terminal, or None."""
        path = self._terminal_path(terminal)
        if not path.exists():
            return None
        last = None
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.rstrip("\n")
                if not raw_line:
                    continue
                try:
                    last = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
        return last
