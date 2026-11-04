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

  read_ndjson_strict    Same parsing, but only the torn tail (the final fragment
                        of a file that does not end in a newline) may be
                        unparseable; an optional predicate scopes the refusal.

Posix-only paths (the audit trail is written with ``fcntl.flock`` on posix);
``os.fsync`` is available on every platform this repo targets.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Union

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


def read_ndjson_strict(
    path: PathLike, predicate: Optional[Callable[[str], bool]] = None
) -> List[Any]:
    """Parsed records; only an unterminated final fragment (torn) may be unparseable.

    A broken final line that ends in a newline was written completely, so it is
    interior corruption. A line that is not UTF-8 is unparseable too — never U+FFFD.
    ``predicate`` scopes the refusal: an interior break raises only when it holds for
    that line's raw text, and is otherwise skipped with a WARNING.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return []
    lines = data.split(b"\n")
    # Only the final physical fragment — the bytes after the last "\n" — can be torn.
    # A whitespace-only fragment is no line at all, so a break before it is interior.
    final_idx = len(lines) - 1
    torn_idx = final_idx if not data.endswith(b"\n") and lines[final_idx].strip() else -1
    records: List[Any] = []
    for idx, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw.decode("utf-8").strip()))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if idx == torn_idx:
                logger.debug("ndjson_io: skipping torn tail line %d in %s", idx + 1, p)
                continue
            if predicate is None or predicate(raw.decode("utf-8", errors="replace")):
                raise NdjsonCorruptionError(p, idx + 1, str(exc)) from exc
            logger.warning("ndjson_io: skipping malformed NDJSON line %d in %s", idx + 1, p)
    return records


__all__ = ["NdjsonCorruptionError", "fsync_fileno", "iter_ndjson", "read_ndjson",
           "read_ndjson_strict"]
