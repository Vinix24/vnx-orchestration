#!/usr/bin/env python3
"""VNX Migration 0010 — project_id column runner (Phase 0 single-VNX).

Idempotently adds ``project_id TEXT NOT NULL DEFAULT '<resolved-pid>'`` plus a
single-column index to hot tables in both ``quality_intelligence.db`` and
``runtime_coordination.db``. Reapplying is a no-op:

  - Tables not present in a given DB are skipped (cross-DB single SQL file).
  - Tables that already have ``project_id`` are skipped; index is still ensured.
  - Version stamp is inserted via ``INSERT OR IGNORE`` so reruns are quiet.

Source of truth for the SQL: ``schemas/migrations/0010_add_project_id.sql``.
This module mirrors that file via a Python runner because SQLite's
``ALTER TABLE ... ADD COLUMN`` does not support ``IF NOT EXISTS``.

Companion plan: ``claudedocs/2026-04-30-single-vnx-migration-plan.md`` §6 Phase 0.

W-init (1.0-blocker): run_runtime_coordination_migration and
run_quality_intelligence_migration now accept an explicit ``default_project_id``
and expose a ``resolve_init_project_id()`` helper that resolves the owning
project_id fail-closed (marker file → VNX_PROJECT_ID env; no silent 'vnx-dev'
fallback). Callers that cannot supply an explicit pid call
``resolve_init_project_id(db_path)`` before invoking the runners.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, Optional

DEFAULT_PROJECT_ID = "vnx-dev"

# ---------------------------------------------------------------------------
# W-init: fail-closed project_id resolution for init / ADD COLUMN backfill.
#
# Resolution order (anchored on the DB file path, not cwd — same anchor as
# migrate_future_system._resolve_validated_project_id):
#   1. .vnx-project-id marker file walking UP from the DB directory.
#   2. VNX_PROJECT_ID environment variable.
#
# All present sources MUST agree.  Any conflict → RuntimeError (fail-closed).
# No sources at all → RuntimeError (no silent 'vnx-dev' default).
#
# This is intentionally a stripped-down resolver — it avoids importing the
# full vnx_identity chain (which requires the vnx_paths bootstrap) so that
# project_id_migration.py can be imported from lightweight init scripts.
# ---------------------------------------------------------------------------

_PROJECT_FILE_NAME = ".vnx-project-id"
_INIT_PID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _read_marker_from_path(db_path: Path) -> Optional[str]:
    """Walk UP from db_path's directory, return the first-line pid from .vnx-project-id."""
    start = db_path.resolve().parent
    for ancestor in [start, *start.parents]:
        marker = ancestor / _PROJECT_FILE_NAME
        if marker.is_file():
            try:
                first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            except (OSError, IndexError):
                return None
            return first or None
    return None


def resolve_init_project_id(db_path: Path) -> str:
    """Resolve the owning project_id for an init-time ADD COLUMN backfill.

    Anchor: the resolved DB file path (not cwd).  Resolution order:
      1. .vnx-project-id marker file, walking UP from the DB directory.
      2. VNX_PROJECT_ID environment variable.

    All present sources must agree — any conflict raises RuntimeError (fail-closed).
    No sources → RuntimeError.  No silent 'vnx-dev' fallback.

    ADR-007 compliance: callers must invoke this and pass the returned value
    as ``default_project_id`` to ``run_runtime_coordination_migration`` /
    ``run_quality_intelligence_migration``.  Passing ``DEFAULT_PROJECT_ID``
    directly (hardcoding 'vnx-dev') is now a linter violation.
    """
    marker_pid = _read_marker_from_path(Path(db_path))
    env_pid = (os.environ.get("VNX_PROJECT_ID") or "").strip() or None

    sources: Dict[str, str] = {}
    if marker_pid:
        sources["marker"] = marker_pid
    if env_pid:
        sources["env:VNX_PROJECT_ID"] = env_pid

    distinct = set(sources.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v!r}" for k, v in sources.items())
        raise RuntimeError(
            f"ADR-007 fail-closed: project_id conflict for init backfill — {detail}. "
            "All sources must agree. Resolve the conflict before running init."
        )
    if not distinct:
        raise RuntimeError(
            "ADR-007 fail-closed: cannot resolve project_id for init ADD COLUMN "
            "backfill. No .vnx-project-id marker found walking up from "
            f"'{Path(db_path).resolve().parent}', and VNX_PROJECT_ID is unset. "
            "Set VNX_PROJECT_ID or place a .vnx-project-id marker at the project root."
        )

    pid = distinct.pop()
    if not _INIT_PID_RE.match(pid):
        raise RuntimeError(
            f"ADR-007 fail-closed: resolved project_id {pid!r} is not a valid VNX id "
            f"(must match {_INIT_PID_RE.pattern}). Fix the marker or VNX_PROJECT_ID."
        )
    return pid

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
#
# worker_states is included here even though its project_id column was
# originally introduced by migration 0017. The v9 schema creates
# worker_states WITHOUT project_id, and the v10 schema's
# ``CREATE TABLE IF NOT EXISTS worker_states`` is a no-op on a DB that
# already has the v9 table — so freshly-initialised and desynced DBs end up
# missing worker_states.project_id unless 0017 happens to run. 0017 is
# version-gated and also performs an invasive composite-UNIQUE rebuild, so it
# will not re-run on a DB whose runtime_schema_version is already >= 12.
# Listing worker_states here lets the idempotent init path self-heal the
# column (and the ``idx_worker_states_project`` index) on every init,
# independent of schema version. Closes the worker_states half of OI-095.
RUNTIME_COORDINATION_TABLES: tuple[str, ...] = (
    "dispatches",
    "dispatch_attempts",
    "terminal_leases",
    "worker_states",
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


# ---------------------------------------------------------------------------
# worker_pid self-heal — 2nd schema-code drift (after worker_states.project_id)
# ---------------------------------------------------------------------------
#
# rc6 code WRITES and READS ``terminal_leases.worker_pid``
# (pool_state_repo.store_worker_pid / list_members), but no schema file or
# migration ever defined the column. Every dispatch logged
# "PID persistence failed: no such column: worker_pid" — non-fatal (the write
# is in a rolled-back try/except) but it blinds the supervisor's worker-PID
# tracking, degrading the lease-reaper / runtime_supervise path.
#
# The column is now declared in schemas/runtime_coordination{,_v10}.sql, but a
# fresh DB builds terminal_leases from the v1 base (CREATE TABLE IF NOT EXISTS
# in v10 is a no-op on the existing table) and pre-existing DBs predate the
# declaration entirely — so, exactly like worker_states.project_id (OI-095), a
# version-independent idempotent self-heal on every init is required. SQLite
# has no ``ADD COLUMN IF NOT EXISTS``; guard with PRAGMA table_info first.
WORKER_PID_TABLE = "terminal_leases"
WORKER_PID_COLUMN = "worker_pid"


def ensure_worker_pid_column(conn: sqlite3.Connection) -> str:
    """Idempotently ensure ``terminal_leases.worker_pid`` (INTEGER, nullable).

    Returns one of:
      - ``"added"``           — column was just added
      - ``"already_present"`` — column already existed
      - ``"skipped_missing"`` — terminal_leases table not present in this DB

    Reapplying is a clean no-op regardless of ``user_version``. Nullable
    INTEGER: a worker PID when one is attached, NULL otherwise.
    """
    _validate_identifier(WORKER_PID_TABLE)
    if not _table_exists(conn, WORKER_PID_TABLE):
        return "skipped_missing"
    if _column_exists(conn, WORKER_PID_TABLE, WORKER_PID_COLUMN):
        return "already_present"
    conn.execute(
        f"ALTER TABLE {WORKER_PID_TABLE} ADD COLUMN {WORKER_PID_COLUMN} INTEGER"
    )
    return "added"


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


def run_runtime_coordination_migration(
    db_path: str | Path,
    *,
    default_project_id: Optional[str] = None,
) -> Dict[str, object]:
    """Apply migration 0010 to runtime_coordination.db. Idempotent.

    Stamps ``runtime_schema_version`` to 10 if not already present.
    Creates the table first when absent — on some pip-wheel paths init_schema
    may resolve a different schema directory and skip the base schema that would
    normally create it, leaving the INSERT below to hit "no such table".

    W-init (ADR-007): ``default_project_id`` controls the DEFAULT clause of
    the ADD COLUMN backfill.  When ``None``, it is resolved fail-closed via
    ``resolve_init_project_id(db_path)``.  Callers should pass the owning
    project_id explicitly; the auto-resolve path is a convenience for
    library-level use.  Passing ``'vnx-dev'`` explicitly is intentional for
    the vnx-dev project store only.
    """
    path = Path(db_path)
    if not path.exists():
        return {"status": "skipped_no_db", "db_path": str(path), "results": {}}

    if default_project_id is None:
        default_project_id = resolve_init_project_id(path)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        results = apply_project_id_migration(
            conn, RUNTIME_COORDINATION_TABLES, default_project_id=default_project_id
        )
        # Self-heal the 2nd schema-code drift: terminal_leases.worker_pid.
        # Independent of the project_id columns above; nullable INTEGER.
        worker_pid_status = ensure_worker_pid_column(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runtime_schema_version ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "description TEXT NOT NULL)"
        )
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
        "worker_pid_status": worker_pid_status,
    }


def run_quality_intelligence_migration(
    db_path: str | Path,
    *,
    default_project_id: Optional[str] = None,
) -> Dict[str, object]:
    """Apply migration 0010 to quality_intelligence.db. Idempotent.

    Stamps ``schema_version`` (TEXT-keyed) with ``8.3.0-project-id`` if absent.

    W-init (ADR-007): ``default_project_id`` controls the DEFAULT clause of
    the ADD COLUMN backfill.  When ``None``, it is resolved fail-closed via
    ``resolve_init_project_id(db_path)``.
    """
    path = Path(db_path)
    if not path.exists():
        return {"status": "skipped_no_db", "db_path": str(path), "results": {}}

    if default_project_id is None:
        default_project_id = resolve_init_project_id(path)

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        results = apply_project_id_migration(
            conn, QUALITY_INTELLIGENCE_TABLES, default_project_id=default_project_id
        )
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
