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

  scan_ndjson_lines     The one torn-tail rule, per line; its readers (door,
                        finalizer, append tail, sweep, report) never re-derive it.

Posix-only paths (the audit trail is written with ``fcntl.flock`` on posix);
``os.fsync`` is available on every platform this repo targets.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union

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


def scan_ndjson_lines(data: bytes) -> Iterator[Tuple[int, bytes, Any, Optional[Exception], bool]]:
    """(number, raw, record, error, torn) per line — the ONE torn-tail rule.

    ``torn`` is only the final newline-unterminated fragment that failed to parse;
    every other failure is interior corruption, terminated final lines included.
    """
    parts = data.split(b"\n")
    for index, raw in enumerate(parts):
        if not raw.strip():
            continue
        try:
            yield index + 1, raw, json.loads(raw.decode("utf-8").strip()), None, False
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            yield index + 1, raw, None, exc, index == len(parts) - 1


def read_ndjson_strict_bytes(data: bytes, *, source: PathLike = "<bytes>",
                             relevance: Optional[Callable[[bytes], bool]] = None) -> List[Any]:
    """Parsed records from a blob; *relevance* scopes an interior break to the lines it names."""
    records: List[Any] = []
    for number, raw, record, error, torn in scan_ndjson_lines(data):
        if error is None:
            records.append(record)
        elif torn:
            logger.debug("ndjson_io: skipping torn tail line in %s", source)
        elif relevance is not None and not relevance(raw):
            logger.warning("ndjson_io: skipping malformed NDJSON line %d in %s", number, source)
        else:
            raise NdjsonCorruptionError(source, number, str(error))
    return records


def read_ndjson_strict(path: PathLike, *,
                       relevance: Optional[Callable[[bytes], bool]] = None) -> List[Any]:
    """Only the torn tail may be unparseable; a non-UTF-8 byte refuses, never U+FFFD."""
    try:
        return read_ndjson_strict_bytes(Path(path).read_bytes(), source=path, relevance=relevance)
    except FileNotFoundError:
        return []


__all__ = ["NdjsonCorruptionError", "fsync_fileno", "iter_ndjson", "read_ndjson",
           "read_ndjson_strict", "read_ndjson_strict_bytes", "scan_ndjson_lines"]
