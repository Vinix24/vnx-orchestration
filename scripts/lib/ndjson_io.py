"""ndjson_io.py — durable NDJSON append fsync + torn-tail-safe read.

Two primitives shared by the governance audit trail (``t0_receipts.ndjson`` and
its sibling gate/event NDJSON streams):

  fsync_fileno(fh)      Best-effort ``os.fsync`` of an open append handle so the
                        record is on disk before the ``fcntl.flock`` releases.
                        Degrades gracefully (logged warning, returns False) on a
                        filesystem that rejects fsync — never raises, so it can
                        sit inside an existing write error-contract untouched.

  iter_ndjson(path)     Iterate parsed records, skipping a torn/partial final
  read_ndjson(path)     line left by a crash mid-append instead of raising. A
                        partial last record must never break the reader.

  read_ndjson_strict    Same parsing, but only the torn tail may be unparseable:
                        any other broken line raises ``NdjsonCorruptionError``,
                        since a file with a hole in it is not evidence.

Posix-only paths (the audit trail is written with ``fcntl.flock`` on posix);
``os.fsync`` is available on every platform this repo targets.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator, List, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]", Path]


def fsync_fileno(fh: Any, *, context: str = "") -> bool:
    """Best-effort ``os.fsync`` of an open file handle.

    Durability step for a locked NDJSON append: flushes the OS page cache to
    stable storage so an appended record survives a crash before the append
    lock is released. On a filesystem that does not support fsync (rare — some
    network/overlay mounts raise ``OSError``) the failure is logged and
    swallowed so it never escalates past the caller's existing error contract.

    Returns True when the sync succeeded, False when it degraded.
    """
    try:
        os.fsync(fh.fileno())
        return True
    except (OSError, ValueError) as exc:
        logger.warning(
            "ndjson_io: fsync failed%s (durability degraded, record kept): %s",
            f" for {context}" if context else "",
            exc,
        )
        return False


def iter_ndjson(path: PathLike) -> Iterator[Any]:
    """Yield parsed JSON records from an NDJSON file, tolerating a torn tail.

    A crash mid-append can leave the file's final record partial (truncated
    JSON, or missing its trailing newline). That torn tail is skipped instead
    of raising, so a reader is never broken by an incomplete last line. Blank
    lines are skipped. A malformed line that is NOT the final record is genuine
    mid-file corruption: it is skipped with a WARNING (the reader stays alive
    and the anomaly is surfaced) rather than silently accepted. A missing file
    yields nothing.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    if not text:
        return

    lines = text.splitlines()

    # Index of the final non-blank line. A crash mid-append leaves its partial
    # record here, so this is the only line permitted to be unparseable.
    last_content_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_content_idx = i
            break

    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if idx == last_content_idx:
                # Torn tail — expected after a crash mid-append. Skip quietly.
                logger.debug("ndjson_io: skipping torn tail line in %s", p)
            else:
                logger.warning(
                    "ndjson_io: skipping malformed NDJSON line %d in %s", idx + 1, p
                )


def read_ndjson(path: PathLike) -> List[Any]:
    """Eager list form of :func:`iter_ndjson` (torn-tail-safe)."""
    return list(iter_ndjson(path))


class NdjsonCorruptionError(ValueError):
    """An unparseable line that is NOT the torn tail: the file cannot be read as evidence."""

    def __init__(self, path: PathLike, line_number: int, detail: str):
        self.path, self.line_number, self.detail = path, line_number, detail
        super().__init__(f"unparseable line {line_number} of {path} "
                         f"(interior corruption, not a torn tail): {detail}")


def read_ndjson_strict(path: PathLike) -> List[Any]:
    """Parsed records; only the torn tail may be unparseable — an interior break raises.

    A line that is not UTF-8 is unparseable too, so a byte-level break refuses, never U+FFFD.
    """
    p = Path(path)
    try:
        lines = p.read_bytes().split(b"\n")
    except FileNotFoundError:
        return []
    last_content_idx = max((i for i, raw in enumerate(lines) if raw.strip()), default=-1)
    records: List[Any] = []
    for idx, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw.decode("utf-8").strip()))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if idx != last_content_idx:
                raise NdjsonCorruptionError(p, idx + 1, str(exc)) from exc
            logger.debug("ndjson_io: skipping torn tail line in %s", p)
    return records


__all__ = ["NdjsonCorruptionError", "fsync_fileno", "iter_ndjson", "read_ndjson",
           "read_ndjson_strict"]
