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
    the listed columns, ``(project_id, id)`` for all eleven;
  - recommendation_outcomes: ``ux_recommendation_outcomes_pid_rec_metric`` UNIQUE
    over ``(project_id, recommendation_id, metric_name)``, the conflict target
    of recommendation_tracker's two upserts (UPSERT_KEYS). Tenant stamping
    rebuilds the table with that key in it; a store at 34 has it before that,
    so the target is the same on every shape with the column. On sales-copilot
    the table's own composite UNIQUE already covers the same columns; the index
    is created there too, so every v34 store carries it under one name.
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

Post-condition, checked before the version stamp (shape_violations):
composite_index_audit reports nothing missing, and the v34 schema manifest's own
invariants hold for everything v34 adds to v33. ``CREATE [UNIQUE] INDEX IF NOT
EXISTS`` is a silent no-op when an index of that name already exists, on another
table, over other columns, partial or with the wrong uniqueness; the
post-condition refuses each of those, so 0034 never stamps 34 on a shape the
manifest would send back to 33. Any failure raises inside apply_if_below's
SAVEPOINT, which sits in an outer ``BEGIN IMMEDIATE``; that transaction is rolled
back, so user_version stays below 34 and the file stays byte-equal.

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
import schema_manifest
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


#: The per-project upsert key: (table, index, columns). recommendation_tracker's
#: two upserts name it as their conflict target wherever the store carries it.
#: Over a superset of the natural key (recommendation_id, metric_name), which is
#: already UNIQUE on every shape, so creating it never meets a duplicate.
UPSERT_KEYS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("recommendation_outcomes", "ux_recommendation_outcomes_pid_rec_metric",
     ("project_id", "recommendation_id", "metric_name")),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


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


def create_upsert_keys(conn: sqlite3.Connection) -> List[str]:
    """``CREATE UNIQUE INDEX IF NOT EXISTS`` for each :data:`UPSERT_KEYS` entry
    whose table exists. Returns the indexes it ran the statement for."""
    done: List[str] = []
    for table, index_name, cols in UPSERT_KEYS:
        if not _table_exists(conn, table):
            continue
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} ({', '.join(cols)})"
        )
        done.append(index_name)
    return done


def shape_violations(conn: sqlite3.Connection) -> List[str]:
    """What keeps *conn*'s store from the 0034 shape; empty when it holds.

    Two checks, both on the tables the store has (0034 creates no table):
      - spec step 2d: ``composite_index_audit`` reports no ux_<table>_pid
        missing from its own table;
      - the v34 manifest's own invariants for what v34 adds to v33: a table v33
        does not declare is validated whole, worker_pool_membership on what v34
        adds to it. So every v34 rejection 0034 is answerable for is a refusal
        here: a missing column, a partial or non-UNIQUE ux_<table>_pid, an
        idx_<table>_project on another table, over other columns, partial or
        UNIQUE, and the upsert key with any of those defects. A violation that
        v33 reports as well is left to the reconciler; 0034 did not cause it.
    """
    out: List[str] = [
        f"{m['table']}: index {m['index']} is not attached to the table"
        for m in composite_index_audit(conn)["missing"]
    ]
    for tbl in schema_manifest.SCHEMA_MANIFEST[_TARGET_VERSION].tables:
        prev = schema_manifest.table_at(_TARGET_VERSION - 1, tbl.name)
        if tbl == prev or not _table_exists(conn, tbl.name):
            continue
        found = schema_manifest.validate_table(conn, tbl)
        known = set(schema_manifest.validate_table(conn, prev)) if prev else set()
        out.extend(v for v in found if v not in known)
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
            upsert_keys = create_upsert_keys(c)
            violations = shape_violations(c)
            if violations:
                raise Migration0034Error(
                    f"apply_0034: {Path(db_path).name} did not reach the 0034 shape, "
                    f"rolled back: {violations}"
                )
            added = sorted(t for t, status in results.items() if status == "added")
            log.info(
                "apply_0034: project_id=%r added to %s; ux_<table>_pid ensured on %s; "
                "upsert keys ensured: %s",
                pid, added or "no table", indexed, upsert_keys,
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
