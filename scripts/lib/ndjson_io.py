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

  read_ndjson_strict_bytes
                        The same rule over an in-memory blob (a ledger window read
                        from an offset), for readers that never hold the whole file.

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

# The append closer's seal on a crash fragment: the only line ending in this is debris.
TORN_TAIL_CLOSED = b"\t#vnx-torn-tail-closed"


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

    # Only the final element after the last newline may be unparseable (K-9c rule 2).
    last_content_idx = -1
    if not text.endswith("\n") and text.rsplit("\n", 1)[-1].strip():
        last_content_idx = len(lines) - 1

    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if line.endswith(TORN_TAIL_CLOSED.decode()):
                logger.warning("ndjson_io: closed torn fragment on line %d in %s", idx + 1, p)
            elif idx == last_content_idx:
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

    ``torn`` is only the final fragment of a blob that does not end in a newline:
    such a fragment was never written completely by a crash mid-append. A broken
    final line that DOES end in a newline was committed whole, so it is interior
    corruption, exactly like any broken line before it. Decided on the blob's last
    split element, never on the last non-blank line (K-9c rule 2).
    """
    parts = data.split(b"\n")
    torn_idx = len(parts) - 1 if not data.endswith(b"\n") else -1
    for index, raw in enumerate(parts):
        if not raw.strip():
            continue
        try:
            yield index + 1, raw, json.loads(raw.decode("utf-8").strip()), None, False
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            yield index + 1, raw, None, exc, index == torn_idx


def read_ndjson_strict_bytes(
    data: bytes,
    *,
    source: PathLike = "<bytes>",
    predicate: Optional[Callable[[str], bool]] = None,
) -> List[Any]:
    """Parsed records from an in-memory blob; the torn-tail decision is ``scan_ndjson_lines``.

    ``predicate`` scopes the refusal: an interior break raises only when it holds for
    that line's raw text (decoded with ``errors="replace"``, never U+FFFD silently lost),
    and is otherwise skipped with a WARNING. A broken line ending in ``TORN_TAIL_CLOSED`` is
    debris: skipped with a WARNING whatever the predicate says.
    """
    records: List[Any] = []
    for number, raw, record, error, torn in scan_ndjson_lines(data):
        if error is None:
            records.append(record)
        elif raw.endswith(TORN_TAIL_CLOSED):
            logger.warning("ndjson_io: closed torn fragment on line %d of %s", number, source)
        elif torn:
            logger.debug("ndjson_io: skipping torn tail line %d in %s", number, source)
        elif predicate is None or predicate(raw.decode("utf-8", errors="replace")):
            raise NdjsonCorruptionError(source, number, str(error))
        else:
            logger.warning("ndjson_io: skipping malformed NDJSON line %d in %s", number, source)
    return records


def read_ndjson_strict(
    path: PathLike, predicate: Optional[Callable[[str], bool]] = None
) -> List[Any]:
    """Parsed records; only an unterminated final fragment (torn) may be unparseable.

    A broken final line that ends in a newline was written completely, so it is
    interior corruption; so is one that is not UTF-8. ``TORN_TAIL_CLOSED`` debris is skipped.
    ``predicate`` scopes the refusal: an interior break raises only when it holds for
    that line's raw text, and is otherwise skipped with a WARNING.
    """
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    return read_ndjson_strict_bytes(data, source=path, predicate=predicate)


__all__ = ["NdjsonCorruptionError", "TORN_TAIL_CLOSED", "fsync_fileno", "iter_ndjson",
           "read_ndjson", "read_ndjson_strict", "read_ndjson_strict_bytes", "scan_ndjson_lines"]
