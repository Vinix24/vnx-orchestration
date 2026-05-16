"""receipt_tail.py — Wave 5 PR-5.6: per-project NDJSON tail-reader.

Streams per-project t0_receipts.ndjson files and merges them into a single
ordered event sequence sorted by (timestamp, project_id, sequence).

Each project's file is read from a byte-offset that survives polling cycles
so that resume-after-restart works correctly. The ring-buffer truncation
pattern (T{n}.ndjson truncated post-dispatch) is handled: when the observed
file size shrinks, the offset is reset to 0 (the file was truncated/replaced).

Malformed JSON lines emit a WARNING and are skipped; they do not stop the
stream.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    root: Path
    ndjson_path: Optional[Path] = None

    def receipt_path(self) -> Path:
        if self.ndjson_path is not None:
            return self.ndjson_path
        return self.root / ".vnx-data" / "state" / "t0_receipts.ndjson"


@dataclass
class MergedEvent:
    project_id: str
    timestamp: str
    sequence: int
    raw: Dict
    dispatch_id: str = ""
    event_type: str = ""

    def __lt__(self, other: "MergedEvent") -> bool:
        return (self.timestamp, self.project_id, self.sequence) < (
            other.timestamp,
            other.project_id,
            other.sequence,
        )


@dataclass
class _ProjectState:
    config: ProjectConfig
    offset: int = 0
    sequence: int = 0
    last_size: int = 0
    malformed_skip_count: int = 0


class ReceiptTail:
    """Tail-reader that merges per-project receipt NDJSON streams.

    Usage::

        tail = ReceiptTail(projects=[...], poll_interval=1.0)
        for event in tail.stream():
            process(event)
        tail.stop()

    ``stream()`` blocks indefinitely and is safe to run from a background
    thread. Call ``stop()`` from another thread to terminate.
    """

    def __init__(
        self,
        projects: List[ProjectConfig],
        poll_interval: float = 1.0,
    ) -> None:
        self._projects: List[_ProjectState] = [
            _ProjectState(config=p) for p in projects
        ]
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def stream(self) -> Iterator[MergedEvent]:
        while not self._stop_event.is_set():
            batch: List[MergedEvent] = []
            for ps in self._projects:
                batch.extend(self._poll_project(ps))
            batch.sort()
            yield from batch
            self._stop_event.wait(timeout=self._poll_interval)

    def _poll_project(self, ps: _ProjectState) -> List[MergedEvent]:
        path = ps.config.receipt_path()
        if not path.exists():
            return []

        try:
            size = path.stat().st_size
        except OSError:
            return []

        if size < ps.last_size:
            log.debug(
                "receipt_tail: %s truncated (was %d, now %d) — resetting offset",
                path,
                ps.last_size,
                size,
            )
            ps.offset = 0

        ps.last_size = size

        if ps.offset >= size:
            return []

        events: List[MergedEvent] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in self._read_new_lines(ps, fh):
                    ps.sequence += 1
                    ts = raw.get("timestamp") or ""
                    events.append(
                        MergedEvent(
                            project_id=ps.config.project_id,
                            timestamp=ts,
                            sequence=ps.sequence,
                            raw=raw,
                            dispatch_id=raw.get("dispatch_id") or "",
                            event_type=raw.get("event_type") or raw.get("event", ""),
                        )
                    )
        except OSError as exc:
            log.warning("receipt_tail: cannot read %s: %s", path, exc)
            return []

        return events

    def _read_new_lines(self, ps: _ProjectState, fh) -> Iterator[Dict]:
        """Read new lines, advancing offset only after successful parse.

        Partial write (no trailing newline): rewinds to line start, stops
        reading — the incomplete line is retried on the next poll cycle.
        Genuine malformed JSON (full line, bad content): logs warning,
        advances past the bad line, continues — prevents infinite loop.
        """
        fh.seek(ps.offset)
        while True:
            line_start = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                # Writer hasn't flushed newline yet — preserve offset for retry
                fh.seek(line_start)
                break
            stripped = line.strip()
            if not stripped:
                ps.offset = fh.tell()
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                log.warning(
                    "receipt_tail: malformed JSON in %s at offset %d, skipping line",
                    ps.config.receipt_path(),
                    line_start,
                )
                ps.malformed_skip_count += 1
                ps.offset = fh.tell()
                continue
            ps.offset = fh.tell()
            yield raw
