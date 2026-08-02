"""coordination_retry.py — Shared bounded-retry pattern for coordination DB writes.

Extracted from the proven ``_rearm_busy_timeout`` pattern in
``dispatch_metadata_db.py`` so every coordination-event writer gets the
same deadline-aware lock retry.  Without this, a single ``database is locked``
under concurrency silently loses the audit event — the writer logs at
WARNING and returns, the dispatch continues, and the governance trail is
incomplete with no signal to the operator (OI-868).
"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)

# How long a coordination writer waits for the DB lock before failing loud.
# Matching the existing dispatch_metadata_db default so no caller that
# switches to this helper sees a different wait behaviour than before.
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0


class CoordinationLockError(Exception):
    """Raised when a coordination DB write cannot acquire the lock in time.

    Callers that previously swallowed ``sqlite3.OperationalError`` at
    WARNING level must either catch this explicitly or let it propagate
    so the lost audit event is visible.
    """


def rearm_busy_timeout(conn: sqlite3.Connection, deadline: float) -> None:
    """Re-arm ``busy_timeout`` to the seconds remaining on *deadline*.

    The sqlite3 driver's ``busy_timeout`` is per-statement, not per-connection
    lifetime — a 3-statement transaction (INSERT + UPDATE + commit) would each
    independently wait up to the full timeout, multiplying the effective stall.
    Re-arming before each statement bounds the TOTAL wait to one deadline window.

    Raises :exc:`CoordinationLockError` when the deadline has already passed.
    The caller must NOT proceed with the write — the audit event will be lost
    and the failure must be visible.
    """
    remaining = deadline - time.monotonic()
    conn.execute(f"PRAGMA busy_timeout = {max(0, int(remaining * 1000))}")
    if remaining <= 0:
        raise CoordinationLockError(
            "Coordination DB write lock deadline exhausted — "
            "lock still held after full timeout; audit event NOT written"
        )


def deadline_for_timeout(timeout: float) -> float:
    """Return a monotonic deadline *timeout* seconds from now."""
    return time.monotonic() + timeout


def is_lock_timeout_error(exc: BaseException) -> bool:
    """Classify *exc* as a coordination lock-timeout failure (OI-880).

    Two distinct failures surface when a coordination write loses the lock
    race, and both must reach the caller as :exc:`CoordinationLockError`:

    - the deadline sentinel that :func:`rearm_busy_timeout` raises once the
      deadline has already passed, and
    - the ``sqlite3.OperationalError`` SQLite itself raises when a statement
      exhausts its ``busy_timeout`` while another connection still holds the
      write lock (``database is locked``).  That error is what the call sites'
      generic ``except Exception`` / ``except sqlite3.Error`` used to swallow at
      WARNING — a silently lost audit event.

    The classification is content-based, not type-only: ``OperationalError``
    covers real bugs too (``no such table``, ``no such column``, ...) and those
    must NOT be treated as lock contention.  Prefer the structured
    ``sqlite_errorcode`` / ``sqlite_errorname`` attributes (Python 3.11+)
    against SQLITE_BUSY / SQLITE_LOCKED — including extended codes via the
    low-byte primary mask, mirroring ``migrate_future_system._is_busy_or_locked``
    — and fall back to the message substring only when no error code is
    attached (e.g. an exception constructed in a test).
    """
    if isinstance(exc, CoordinationLockError):
        return True
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        if (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return True
        # A structured code that is not BUSY/LOCKED is authoritative — the
        # exception is definitively not a lock conflict, whatever its message
        # says.  Falling through to the message check here would let a
        # non-standard message flip a real bug into a lock timeout.
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message
