"""apply_0033.py — tracks.decision_ref migration (OI-1190 plan decision findability).

Adds: tracks.decision_ref (nullable TEXT) holding a JSON payload that points at
the plan-gate report(s) plus the rejected alternatives with their reasons.

ADR-007: additive column on tracks, whose composite PRIMARY KEY
(track_id, project_id) already satisfies ADR-007 (migration 0024). No new index
(decision_ref is read via the existing PK lookup, never queried by its own value).

Idempotent: PRAGMA user_version >= 33 → skip entirely.
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

from schema_migration import apply_script_if_below

log = logging.getLogger(__name__)


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version)."""
    sql = migration_sql_path.read_text()

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        applied = apply_script_if_below(conn, 33, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0033: tracks.decision_ref applied (user_version → 33)")
    else:
        log.debug("apply_0033: already at user_version >= 33; skipped")
    return applied
