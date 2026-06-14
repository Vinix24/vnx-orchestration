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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists (PRAGMA table_info returns [] for absent tables)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _col_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (checked via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _self_heal_dispatches_project_id(conn: sqlite3.Connection) -> None:
    """Add dispatches.project_id before the v22 rebuild if it is missing.

    The 0022 SQL rebuilds dispatches via ``INSERT INTO dispatches(... project_id
    ...) SELECT ... project_id ... FROM dispatches_pre_v22``. That SELECT assumes
    project_id was already added by migrations 0010/0015. On a legacy DB whose
    ``PRAGMA user_version`` diverged ahead of its actual schema (those project_id
    migrations never ran against ``dispatches``), the rebuild raises
    ``no such column: project_id`` and the whole script rolls back. Stamping the
    column here (ADR-007 tenant key, DEFAULT 'vnx-dev') lets the rebuild resolve.

    Idempotent + non-destructive: only fires when ``dispatches`` exists AND lacks
    project_id. DBs that already have project_id (fresh installs, re-runs) are
    untouched, and a missing ``dispatches`` table is a no-op — the 0022 SQL's own
    ``ALTER TABLE dispatches RENAME`` surfaces that case unchanged.
    """
    if not _table_exists(conn, "dispatches"):
        return
    if _col_exists(conn, "dispatches", "project_id"):
        return
    conn.execute(
        "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
    )
    log.info("apply_0022: self-healed legacy dispatches with project_id (ADR-007)")


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version)."""
    sql = migration_sql_path.read_text()

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        _self_heal_dispatches_project_id(conn)
        applied = apply_script_if_below(conn, 22, sql)
    finally:
        conn.close()

    if applied:
        log.info("apply_0022: track layer applied (user_version → 22)")
    else:
        log.debug("apply_0022: already at user_version >= 22; skipped")
    return applied
