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
