"""apply_0033.py — tracks.decision_ref migration (OI-1190 plan decision findability).

Adds: tracks.decision_ref (nullable TEXT) holding a JSON payload that points at
the plan-gate report(s) plus the rejected alternatives with their reasons.

ADR-007: additive column on tracks, whose composite PRIMARY KEY
(track_id, project_id) already satisfies ADR-007 (migration 0024). No new index
(decision_ref is read via the existing PK lookup, never queried by its own value).

Idempotent: PRAGMA user_version >= 33 → skip entirely, AND the column-existence
state guard below — a store can carry tracks.decision_ref while user_version < 33
(reconcile_user_version downgrades a v33 store that fails a manifest invariant,
then the re-walk leaves the additive column in place). Re-running ALTER TABLE ADD
COLUMN would raise "duplicate column name: decision_ref", so the guard tests the
schema STATE, not just the version label.

Atomicity: apply_script_if_below wraps all statements in a SAVEPOINT.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from schema_migration import apply_script_if_below, get_user_version

log = logging.getLogger(__name__)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return any(r[1] == column for r in rows)


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version).

    Idempotent against a version downgrade: if tracks.decision_ref is already
    present but user_version < 33, the ALTER is skipped and only the version
    stamp is advanced (the schema is already at v33 shape for this column).
    """
    sql = migration_sql_path.read_text()

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        if _column_exists(conn, "tracks", "decision_ref"):
            # State guard (OI-1197): the column is already there. Advance the
            # version stamp WITHOUT re-running the non-idempotent ALTER TABLE
            # ADD COLUMN, which would raise "duplicate column name: decision_ref".
            if get_user_version(conn) < 33:
                conn.execute("PRAGMA user_version = 33")
                log.info(
                    "apply_0033: tracks.decision_ref already present; "
                    "advanced user_version → 33 (skipped ALTER)"
                )
            else:
                log.debug("apply_0033: already at user_version >= 33; skipped")
            return False
        applied = apply_script_if_below(conn, 33, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0033: tracks.decision_ref applied (user_version → 33)")
    else:
        log.debug("apply_0033: already at user_version >= 33; skipped")
    return applied
