"""atomic_io.py — Shared atomic file write and NDJSON append helpers.

Consolidates the tmp+os.replace pattern (4 inline implementations across
state_writer.py, worker_permission_relay.py, tmux_interactive_dispatch.py,
intelligence_dashboard.py) and the fcntl.flock NDJSON append used in
governance_emit.py.

The scratch file here is a ``tempfile.mkstemp`` name, unique to the writer.
That is the point of it, and the reason a caller must not hand-roll
``path.with_suffix(".tmp")`` instead: that name is a function of the
DESTINATION, so every concurrent writer of one path picks the same scratch
file. See :func:`slot_lock` for what atomicity alone still does not buy.

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
import re
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

logger = logging.getLogger(__name__)

# The name of the scratch file ``atomic_write_text`` leaves behind when a writer dies mid-write:
# ``<TEMP_PREFIX><random><TEMP_SUFFIX>``. Public so a scanner that recognises these files imports the name.
TEMP_PREFIX, TEMP_SUFFIX = tempfile.gettempprefix(), ".tmp"

_EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")


class _SlotLockEntry:
    """One process's hold on one lock file: how deep it is taken, and the description it holds.

    ``reentry`` serialises the threads of the process on this lock; ``depth``
    says how many nested ``slot_lock`` blocks the thread inside it has open.
    """

    __slots__ = ("pid", "reentry", "depth", "handle")

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.reentry = threading.RLock()
        self.depth = 0
        self.handle: Optional[TextIO] = None


# ``flock`` belongs to the open file description, so a second acquisition of the
# same lock inside one process blocks on the first: ``dispatch_serialization``
# documents exactly that and tells its callers not to nest. ``slot_lock`` does
# nest, in one place that must hold a slot's lock ACROSS a call into another
# lock-taking writer: the orphan cleanup holds a request's lock while
# ``gate_outcome.commit_gate_outcome`` ends that request (step 7 takes the same
# lock again), so the identity check and the write are one critical section.
# This table is what makes that inner acquisition a no-op instead of a
# self-deadlock. It is per process (``pid``) and per thread (``reentry``), so
# every other process still queues on the flock exactly as before.
_SLOT_LOCK_ENTRIES: "dict[str, _SlotLockEntry]" = {}
_SLOT_LOCK_ENTRIES_MUTEX = threading.Lock()


def _slot_lock_entry(key: str) -> _SlotLockEntry:
    """This process's entry for one lock file, rebuilt when a ``fork`` inherited the table.

    A child inherits a copy of the table, and with it the parent's depth: its
    own acquisition must queue on the parent's flock like any other process, so
    an entry another process created is dropped and taken fresh.
    """
    pid = os.getpid()
    with _SLOT_LOCK_ENTRIES_MUTEX:
        entry = _SLOT_LOCK_ENTRIES.get(key)
        if entry is None or entry.pid != pid:
            entry = _SlotLockEntry(pid)
            _SLOT_LOCK_ENTRIES[key] = entry
        return entry


@contextmanager
def slot_lock(path: Path) -> Iterator[None]:
    """Hold one path's exclusive lock across a read-check-write sequence.

    :func:`atomic_write_text` makes a write indivisible: a reader never sees
    half a record. It does NOT order two writers, and it cannot, because by
    the time either writes, both have already decided to. A caller that reads
    a file, decides something from what it read, and only then writes needs
    the lock to span all three steps — otherwise two callers read the same
    state, both reach the same decision, and both write, the check having
    ruled out nothing (OI-1486).

    The lock is a sibling ``.<name>.lock`` file rather than the target itself,
    so it survives the target being replaced by rename mid-sequence — locking
    the target would leave each writer holding a lock on a file that is no
    longer at that path.

    ``flock`` is advisory: it holds between callers that take it, and it holds
    equally between processes, because the lock belongs to the open file
    description and each caller opens its own. Within ONE process the lock is
    reentrant (see :data:`_SLOT_LOCK_ENTRIES`): a nested ``slot_lock`` on the
    same path by the thread that already holds it is granted, and every other
    thread waits for the outermost block to close.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    entry = _slot_lock_entry(str(lock_path))
    entry.reentry.acquire()
    try:
        if entry.depth == 0:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            entry.handle = handle
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0:
                handle, entry.handle = entry.handle, None
                if handle is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
    finally:
        entry.reentry.release()


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

    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
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

    Reserved keys event_type, timestamp, pid, actor cannot be overridden by payload.

    ADR-005: raises OSError on write failure — never silently drops events.

    Args:
        events_dir: directory for event NDJSON files (e.g. .vnx-data/events/)
        event_type: determines the filename (<event_type>.ndjson); must match
            [A-Za-z0-9_][A-Za-z0-9_-]{0,63}
        payload: caller-supplied fields merged into the event record
    """
    if not _EVENT_TYPE_RE.match(event_type):
        raise ValueError(
            f"atomic_io: invalid event_type {event_type!r} — must match {_EVENT_TYPE_RE.pattern}"
        )

    events_dir.mkdir(parents=True, exist_ok=True)
    target = events_dir / f"{event_type}.ndjson"

    # Defense-in-depth: assert resolved target stays under events_dir.
    if not target.resolve().is_relative_to(events_dir.resolve()):
        raise ValueError(f"atomic_io: event_type {event_type!r} escapes events_dir")

    # payload merged first so authoritative audit fields always win.
    record: dict = {
        **payload,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
        "actor": os.environ.get("VNX_ACTOR", "system"),
    }

    with open(target, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
