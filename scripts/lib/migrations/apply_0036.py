"""apply_0036.py — the deliverables view knows the terminal state withdrawn (ADR-049).

DROP VIEW plus CREATE VIEW from the v36 manifest (the one copy of the view's SQL),
checked against it before the stamp, in one SAVEPOINT inside ``BEGIN IMMEDIATE``
as in apply_0035: a failure rolls back with user_version at 35. Down:
``revert_migration`` (v36 -> v35). Idempotent both ways.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import schema_manifest
import schema_migration

_TARGET_VERSION = 36
REVERTED_EVENT = "deliverable_withdrawn_reverted"


class Migration0036Error(RuntimeError):
    """The store did not reach (or leave) the 0036 shape; the migration rolled back."""


def _create_view(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DROP VIEW IF EXISTS deliverables")
    conn.execute(schema_manifest.view_at(version, "deliverables").sql)


def _in_transaction(db_path: Path, step: Callable[[sqlite3.Connection], bool]) -> bool:
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit — required for SAVEPOINT semantics
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = step(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return changed
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    _create_view(conn, _TARGET_VERSION)
    violations = schema_manifest.validate_view(conn, schema_manifest.view_at(_TARGET_VERSION, "deliverables"))
    if violations:
        raise Migration0036Error(f"apply_0036: the store did not reach the 0036 shape: {violations}")


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """True if applied, False if skipped (user_version >= 36); a failure raises with the store unchanged.

    *migration_sql_path* is the auto_apply runner contract; the .sql file documents, this runner builds.
    """
    return _in_transaction(db_path, lambda c: schema_migration.apply_if_below(c, _TARGET_VERSION, _migrate))


def _revert(conn: sqlite3.Connection) -> bool:
    version = schema_migration.get_user_version(conn)
    if version == _TARGET_VERSION - 1:
        return False
    if version != _TARGET_VERSION:
        raise Migration0036Error(f"apply_0036 down: needs user_version 36, found {version}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for dispatch_id, project_id in conn.execute(
            "SELECT dispatch_id, project_id FROM dispatches WHERE state = 'withdrawn'").fetchall():
        withdraw = conn.execute(
            "SELECT from_state, reason FROM coordination_events WHERE event_type = 'deliverable_withdrawn' "
            "AND entity_id = ? AND project_id IS ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (dispatch_id, project_id)).fetchone() or (None, None)
        conn.execute("UPDATE dispatches SET state = 'expired', updated_at = ? "
                     "WHERE dispatch_id = ? AND project_id = ? AND state = 'withdrawn'", (now, dispatch_id, project_id))
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, from_state, to_state, "
            "actor, reason, metadata_json, occurred_at, project_id) "
            "VALUES (?, ?, 'dispatch', ?, 'withdrawn', 'expired', 'migration', ?, ?, ?, ?)",
            (uuid.uuid4().hex, REVERTED_EVENT, dispatch_id, "migration 0036 down: v35 has no withdrawn state",
             json.dumps({"withdrawn_from": withdraw[0], "withdraw_reason": withdraw[1]}), now, project_id))
    _create_view(conn, _TARGET_VERSION - 1)
    conn.execute(f"PRAGMA user_version = {_TARGET_VERSION - 1}")
    return True


def revert_migration(db_path: Path) -> bool:
    """v36 -> v35 in one transaction: each withdrawn row becomes expired with one revert event, the v35 view returns.

    The event carries the state before the withdraw and its reason, so an old reader sees a terminal
    state it knows and the ledger keeps why. False at user_version 35; Migration0036Error at any
    version other than 35 or 36.
    """
    return _in_transaction(db_path, _revert)
