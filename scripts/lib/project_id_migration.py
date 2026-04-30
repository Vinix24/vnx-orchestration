#!/usr/bin/env python3
"""VNX Migration 0010 — project_id column runner (Phase 0 single-VNX).

Idempotently adds ``project_id TEXT NOT NULL DEFAULT 'vnx-dev'`` plus a
single-column index to hot tables in both ``quality_intelligence.db`` and
``runtime_coordination.db``. Reapplying is a no-op:

  - Tables not present in a given DB are skipped (cross-DB single SQL file).
  - Tables that already have ``project_id`` are skipped; index is still ensured.
  - Version stamp is inserted via ``INSERT OR IGNORE`` so reruns are quiet.

Source of truth for the SQL: ``schemas/migrations/0010_add_project_id.sql``.
This module mirrors that file via a Python runner because SQLite's
``ALTER TABLE ... ADD COLUMN`` does not support ``IF NOT EXISTS``.

Companion plan: ``claudedocs/2026-04-30-single-vnx-migration-plan.md`` §6 Phase 0.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable

DEFAULT_PROJECT_ID = "vnx-dev"

# Quality Intelligence: hot tables that need project_id at Phase 0.
# (Migration plan §4.1 P0 set + dispatch's confidence_events addition.)
QUALITY_INTELLIGENCE_TABLES: tuple[str, ...] = (
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "pattern_usage",
    "confidence_events",
    "dispatch_metadata",
    "dispatch_pattern_offered",
    "session_analytics",
)

# Runtime Coordination: hot tables that need project_id at Phase 0.
RUNTIME_COORDINATION_TABLES: tuple[str, ...] = (
    "dispatches",
    "dispatch_attempts",
    "terminal_leases",
    "coordination_events",
    "incident_log",
    "intelligence_injections",
)

RUNTIME_SCHEMA_VERSION = 10
RUNTIME_VERSION_DESCRIPTION = (
    "Phase 0 single-VNX migration: add project_id column + indexes to hot tables"
)
QI_SCHEMA_VERSION = "8.3.0-project-id"
QI_VERSION_DESCRIPTION = (
    "Phase 0 single-VNX migration: add project_id columns + indexes to hot tables"
)

# SQLite identifier sanity check — protects f-string interpolation below.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"Refusing to use unsafe SQL identifier: {name!r}")
    return name


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    _validate_identifier(table)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def apply_project_id_migration(
    conn: sqlite3.Connection,
    tables: Iterable[str],
    *,
    default_project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, str]:
    """Apply project_id column + index to each table that exists. Idempotent.

    Returns a mapping ``{table: status}`` where status is one of:
      - ``"added"`` — column was just added (and index created)
      - ``"already_present"`` — column already existed; index ensured
      - ``"skipped_missing"`` — table not present in this DB
    """
    if not _IDENT_RE.match(default_project_id) and "-" not in default_project_id:
        # default_project_id is interpolated literally into the DEFAULT clause;
        # accept kebab-case and lowercase project ids only.
        raise ValueError(f"Unsafe default_project_id: {default_project_id!r}")
    if "'" in default_project_id or "\\" in default_project_id:
        raise ValueError(f"Unsafe default_project_id: {default_project_id!r}")

    results: Dict[str, str] = {}
    for table in tables:
        _validate_identifier(table)
        if not _table_exists(conn, table):
            results[table] = "skipped_missing"
            continue

        index_name = f"idx_{table}_project"
        _validate_identifier(index_name)

        if _column_exists(conn, table, "project_id"):
            results[table] = "already_present"
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(project_id)"
            )
            continue

        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN project_id TEXT NOT NULL "
            f"DEFAULT '{default_project_id}'"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(project_id)"
        )
        results[table] = "added"
    return results


def run_runtime_coordination_migration(db_path: str | Path) -> Dict[str, object]:
    """Apply migration 0010 to runtime_coordination.db. Idempotent.

    Stamps ``runtime_schema_version`` to 10 if not already present.
    """
    path = Path(db_path)
    if not path.exists():
        return {"status": "skipped_no_db", "db_path": str(path), "results": {}}

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        results = apply_project_id_migration(conn, RUNTIME_COORDINATION_TABLES)
        conn.execute(
            "INSERT OR IGNORE INTO runtime_schema_version (version, description) "
            "VALUES (?, ?)",
            (RUNTIME_SCHEMA_VERSION, RUNTIME_VERSION_DESCRIPTION),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "db_path": str(path),
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "results": results,
    }


def run_quality_intelligence_migration(db_path: str | Path) -> Dict[str, object]:
    """Apply migration 0010 to quality_intelligence.db. Idempotent.

    Stamps ``schema_version`` (TEXT-keyed) with ``8.3.0-project-id`` if absent.
    """
    path = Path(db_path)
    if not path.exists():
        return {"status": "skipped_no_db", "db_path": str(path), "results": {}}

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        results = apply_project_id_migration(conn, QUALITY_INTELLIGENCE_TABLES)
        # schema_version table may not exist on a fresh DB; create idempotently.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "    version TEXT PRIMARY KEY,"
            "    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "    description TEXT"
            ")"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, description) VALUES (?, ?)",
            (QI_SCHEMA_VERSION, QI_VERSION_DESCRIPTION),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "db_path": str(path),
        "schema_version": QI_SCHEMA_VERSION,
        "results": results,
    }
