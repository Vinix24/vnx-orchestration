"""apply_0022.py — track layer migration (ADR-019 zero-to-tracks).

Creates: tracks, track_phase_history, track_dependencies, track_open_items.
Rebuilds dispatches with state CHECK + operator_approved_at column.

Idempotent: PRAGMA user_version >= 22 → skip entirely.
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
        applied = apply_script_if_below(conn, 22, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0022: track layer applied (user_version → 22)")
    else:
        log.debug("apply_0022: already at user_version >= 22; skipped")
    return applied
