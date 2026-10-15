"""apply_0034.py — project_id on the seven runtime tables 0015 missed, and the
ADR-007 composite UNIQUE index on every table of _RC_COMPOSITE_KEYS (OI-2026).

The drift. 0015's runtime partition gives project_id to retry_budgets,
retry_state, escalation_log, execution_targets, inbound_inbox, recommendations
and recommendation_outcomes. Stores that passed user_version 15 on another path
never got that column on those seven (measured 2026-10-08/09 and 2026-10-15 on
vnx-dev, mission-control, seocrawler-v2 and pacompany-engine, all at v33). The
sales-copilot store has it, in a rebuilt shape: NOT NULL without a default and
composite natural keys. The ``ux_<table>_pid`` indexes of _RC_COMPOSITE_KEYS are
missing on most stores, because 0015 creates them before 0020 adds
worker_pool_membership and because _migrate_v11_composite_keys skips a table on
any error without raising.

Target shape, per table:
  - the seven: ``project_id TEXT NOT NULL DEFAULT '<store id>'`` and
    ``idx_<table>_project``, exactly as
    project_id_migration.apply_project_id_migration makes them. A table that
    already has the column (sales-copilot, a fresh store) keeps its column, its
    natural keys and its table SQL;
  - every table of _RC_COMPOSITE_KEYS that exists: ``ux_<table>_pid`` UNIQUE over
    the listed columns, ``(project_id, id)`` for all eleven.
No table is rebuilt and no row is touched: an added column takes its value from
the default, which is the store's own id.

Why Python and not the generic pure-SQL runner:
  - the default is the store's id, resolved fail-closed from the DB path
    (``resolve_init_project_id(strict=True)``). An unresolvable store or one
    whose sources disagree raises before anything is written; no id is guessed;
  - ``ALTER TABLE ... ADD COLUMN`` has no IF NOT EXISTS, so the column is
    guarded on existence (apply_project_id_migration);
  - the indexes are created here with plain statements, not through
    _migrate_v11_composite_keys, so a duplicate row or a missing column raises.

Post-condition, checked before the version stamp: every one of the seven that
exists carries project_id, and every ``ux_<table>_pid`` is attached to its own
table as a UNIQUE index over the listed columns (composite_index_audit plus the
key check). ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a silent no-op when an index
of that name belongs to another table; the post-condition catches that. Any
failure raises inside apply_if_below's SAVEPOINT, which sits in an outer
``BEGIN IMMEDIATE``; that transaction is rolled back, so user_version stays
below 34 and the file stays byte-equal.

A table that does not exist is not created: 0034 adds columns and indexes, not
tables. A store without one of these tables (a partial fixture) passes over it.

No down script. The forward shape is additive and the code before it (v1.7.1)
writes into it unchanged, its INSERTs taking the store id from the default.
That holds until tenant stamping (``vnx migrate``, W1 phases 1-3) runs on the
store: it then rebuilds the seven to composite natural keys with project_id
NOT NULL and no default, and v1.7.1's INSERTs fail on NOT NULL (measured on tmp
copies, 2026-10-15). A generic down cannot be correct for every shape: on the
sales-copilot shape project_id is part of the composite natural keys, so DROP
COLUMN refuses, and on fresh and pacompany-engine stores some
``ux_<table>_pid`` indexes predate 0034. The rollback path is the pre-0034
backup of the store.

Idempotent: skipped entirely when user_version >= 34; every step is itself a
no-op on a store that already carries its result.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_id_migration
import schema_migration
from coordination_db import _RC_COMPOSITE_KEYS, composite_index_audit

log = logging.getLogger(__name__)

_TARGET_VERSION = 34

#: The runtime partition of 0015_complete_project_id.sql, the tables that
#: partition meant to give project_id.
COLUMN_TABLES: Tuple[str, ...] = (
    "retry_budgets",
    "retry_state",
    "escalation_log",
    "execution_targets",
    "inbound_inbox",
    "recommendations",
    "recommendation_outcomes",
)


class Migration0034Error(RuntimeError):
    """The store did not reach the 0034 shape; the migration rolled back."""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _index_key(conn: sqlite3.Connection, index_name: str) -> Tuple[str, ...]:
    return tuple(r[2] for r in conn.execute(f"PRAGMA index_info({index_name})").fetchall())


def _index_is_unique(conn: sqlite3.Connection, table: str, index_name: str) -> bool:
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if row[1] == index_name:
            return bool(row[2])
    return False


def create_composite_indexes(conn: sqlite3.Connection) -> List[str]:
    """``CREATE UNIQUE INDEX IF NOT EXISTS ux_<table>_pid`` for each existing table.

    Returns the tables it ran the statement for. Does not catch: a duplicate
    ``(project_id, id)`` or a missing column raises to the caller.
    """
    done: List[str] = []
    for table, cols in _RC_COMPOSITE_KEYS:
        project_id_migration._validate_identifier(table)
        if not _table_exists(conn, table):
            continue
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_pid ON {table} ({', '.join(cols)})"
        )
        done.append(table)
    return done


def shape_violations(conn: sqlite3.Connection) -> List[str]:
    """What keeps *conn*'s store from the 0034 shape; empty when it holds."""
    out: List[str] = []
    for table in COLUMN_TABLES:
        if _table_exists(conn, table) and not project_id_migration._column_exists(
            conn, table, "project_id"
        ):
            out.append(f"{table}: no project_id column")
    audit = composite_index_audit(conn)
    for missing in audit["missing"]:
        out.append(
            f"{missing['table']}: index {missing['index']} is not attached to the table"
        )
    missing_tables = {m["table"] for m in audit["missing"]}
    for table, cols in _RC_COMPOSITE_KEYS:
        if table in missing_tables or not _table_exists(conn, table):
            continue
        index_name = f"ux_{table}_pid"
        key = _index_key(conn, index_name)
        if key != cols:
            out.append(f"{table}: {index_name} keys {key}, expected {cols}")
        if not _index_is_unique(conn, table, index_name):
            out.append(f"{table}: {index_name} is not UNIQUE")
    return out


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Returns True if applied, False if skipped (already at user_version >= 34).

    *migration_sql_path* is accepted for the auto_apply runner contract; the
    .sql file documents the shape, this runner produces it.

    Raises project_scope.TenantUnresolved (no id) or RuntimeError (sources
    disagree, invalid id), sqlite3.Error (an index cannot be created) and
    Migration0034Error (post-condition); in every case the store is unchanged.
    """
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        if schema_migration.get_user_version(conn) >= _TARGET_VERSION:
            log.debug("apply_0034: already at user_version >= %d; skipped", _TARGET_VERSION)
            return False

        def _migrate(c: sqlite3.Connection) -> None:
            pid = project_id_migration.resolve_init_project_id(Path(db_path), strict=True)
            results = project_id_migration.apply_project_id_migration(
                c, COLUMN_TABLES, default_project_id=pid
            )
            indexed = create_composite_indexes(c)
            violations = shape_violations(c)
            if violations:
                raise Migration0034Error(
                    f"apply_0034: {Path(db_path).name} did not reach the 0034 shape, "
                    f"rolled back: {violations}"
                )
            added = sorted(t for t, status in results.items() if status == "added")
            log.info(
                "apply_0034: project_id=%r added to %s; ux_<table>_pid ensured on %s",
                pid, added or "no table", indexed,
            )

        # apply_if_below's own rollback (ROLLBACK TO + RELEASE) commits an empty
        # transaction and so bumps the file's change counter. The outer
        # transaction is rolled back instead: a refused store stays byte-equal.
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = schema_migration.apply_if_below(conn, _TARGET_VERSION, _migrate)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()

    if applied:
        log.info("apply_0034: runtime project_id completion applied (user_version → 34)")
    return applied
