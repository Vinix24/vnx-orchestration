"""apply_0015.py — project_id on every runtime_coordination.db tenant table.

Why Python and not the generic pure-SQL runner:
  - 0015_complete_project_id.sql is cross-database. Its first half ALTERs
    quality_intelligence.db tables that do not exist in runtime_coordination.db.
    Only the ``-- @db: runtime_coordination`` partition applies here; the table
    list is read from that partition, so the .sql file stays the source.
  - ``ALTER TABLE ... ADD COLUMN`` has no IF NOT EXISTS. A store that already
    has the column (sales-copilot got the 0015 columns through
    migrate_to_central_vnx.py) must not raise "duplicate column". Each ALTER is
    guarded on column existence by project_id_migration.apply_project_id_migration.
  - The DEFAULT is the owning tenant resolved fail-closed from the DB path
    (project_id_migration.resolve_init_project_id), not the file's 'vnx-dev'
    literal (ADR-007 W-init).

Why this runner also ensures the 0010 hot-table columns:
  A fresh store reaches PRAGMA user_version 10 through init_schema's
  runtime_coordination_v10.sql. auto_apply therefore never visits 0010, and the
  hot tables (dispatches, terminal_leases, ...) arrive here without project_id.
  0022 then rebuilds dispatches with ``INSERT ... SELECT project_id`` and fails.
  Ensuring 0010's columns here closes that gap on the one path that has it
  (build_t0_state's init_schema + auto_apply); on a store that ran
  run_runtime_coordination_migration first it is a no-op.

After the columns: the ADR-007 composite UNIQUE indexes (coordination_db's V11)
and the terminal_leases.worker_pid self-heal, the same tail
run_runtime_coordination_migration applies.

Idempotent: skipped entirely when user_version >= 15; every step below is
itself a no-op on a store that already carries its result.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Tuple

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_id_migration
import schema_migration
from coordination_db import _migrate_v11_composite_keys

log = logging.getLogger(__name__)

_TARGET_VERSION = 15
_RUNTIME_PARTITION = "-- @db: runtime_coordination"
_ALTER_RE = re.compile(r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+project_id\b",
                       re.IGNORECASE | re.MULTILINE)


def runtime_tables_from_sql(sql: str) -> Tuple[str, ...]:
    """Tables the runtime_coordination partition of 0015 gives a project_id column."""
    _qi_block, marker, rc_block = sql.partition(_RUNTIME_PARTITION)
    if not marker:
        raise ValueError(
            f"0015 migration has no '{_RUNTIME_PARTITION}' partition; cannot tell "
            "which tables belong to runtime_coordination.db"
        )
    tables = tuple(_ALTER_RE.findall(rc_block))
    if not tables:
        raise ValueError("0015 runtime_coordination partition names no project_id ALTER")
    return tables


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at target version)."""
    runtime_tables = runtime_tables_from_sql(migration_sql_path.read_text(encoding="utf-8"))

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        if schema_migration.get_user_version(conn) >= _TARGET_VERSION:
            log.debug("apply_0015: already at user_version >= %d; skipped", _TARGET_VERSION)
            return False
        pid = project_id_migration.resolve_init_project_id(Path(db_path))
        tables = tuple(dict.fromkeys(
            (*project_id_migration.RUNTIME_COORDINATION_TABLES, *runtime_tables)
        ))

        def _migrate(c: sqlite3.Connection) -> None:
            results = project_id_migration.apply_project_id_migration(
                c, tables, default_project_id=pid
            )
            project_id_migration.ensure_worker_pid_column(c)
            _migrate_v11_composite_keys(c)
            added = sorted(t for t, status in results.items() if status == "added")
            log.info("apply_0015: project_id=%r added to %s", pid, added or "no table")

        applied = schema_migration.apply_if_below(conn, _TARGET_VERSION, _migrate)
    finally:
        conn.close()

    if applied:
        log.info("apply_0015: runtime project_id columns applied (user_version → 15)")
    return applied
