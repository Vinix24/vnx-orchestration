"""apply_0024.py — tracks tenant-scoping (ADR-007 composite PKs).

Rebuilds tracks, track_phase_history, track_dependencies, track_open_items
with composite PRIMARY KEY (track_id, project_id) for multi-tenant isolation.

ADR-007: all created tables carry composite (track_id, project_id) PKs.
Idempotent: PRAGMA user_version >= 24 → skip entirely.
Atomicity: apply_script_if_below wraps all statements in a SAVEPOINT.
Prerequisite: 0022 must run first (tracks table must exist).
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
        applied = apply_script_if_below(conn, 24, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0024: tracks tenant-scoping applied (user_version → 24)")
    else:
        log.debug("apply_0024: already at user_version >= 24; skipped")
    return applied
