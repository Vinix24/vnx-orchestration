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


__all__ = ["fsync_fileno", "iter_ndjson", "read_ndjson"]
