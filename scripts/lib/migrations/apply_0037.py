"""apply_0037.py — a deliverable closed on its own round derives ``done`` (ADR-049, OI-2262).

DROP VIEW plus CREATE VIEW from the v37 manifest (the one copy of the SQL), validated
before the stamp, in one SAVEPOINT inside ``BEGIN IMMEDIATE`` as in apply_0036. No row
moves: a base that ended ``expired`` names its delivering round under
``metadata_json.superseded_by``, and the view joins that ``<id>-rK`` row's ``completed``
state as positive evidence — no round, a foreign round or an unreadable blob keeps v36's
reading. Down restores the v36 view and stamps 36, changing no row, so ADR-005 asks for
no ledger line (0036's down path books one because it moves rows). Idempotent both ways.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Callable

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import schema_manifest
import schema_migration

_TARGET_VERSION = 37
VIEW = "deliverables"


class Migration0037Error(RuntimeError):
    """The store did not reach (or leave) the 0037 shape; the migration rolled back."""


def _create_view(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"DROP VIEW IF EXISTS {VIEW}")
    conn.execute(schema_manifest.view_at(version, VIEW).sql)


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
    violations = schema_manifest.validate_view(conn, schema_manifest.view_at(_TARGET_VERSION, VIEW))
    if violations:
        raise Migration0037Error(f"apply_0037: the store did not reach the 0037 shape: {violations}")


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """True if applied, False if skipped (user_version >= 37); a failure raises, store unchanged.

    *migration_sql_path* is the auto_apply runner contract: the .sql file documents, this builds.
    """
    return _in_transaction(db_path, lambda c: schema_migration.apply_if_below(c, _TARGET_VERSION, _migrate))


def _revert(conn: sqlite3.Connection) -> bool:
    """Drop 37 back to 36: the v36 view and the stamp; False when the store already sits at 36."""
    version = schema_migration.get_user_version(conn)
    if version == _TARGET_VERSION - 1:
        return False
    if version != _TARGET_VERSION:
        raise Migration0037Error(f"apply_0037 down: needs user_version 37, found {version}")
    _create_view(conn, _TARGET_VERSION - 1)
    conn.execute(f"PRAGMA user_version = {_TARGET_VERSION - 1}")
    return True


def revert_migration(db_path: Path) -> bool:
    """v37 -> v36 in one transaction: the v36 view returns, no row changes, nothing is booked.

    False at 36, Migration0037Error at any other version. A superseded base reads
    ``failed`` again (fail-closed); its evidence is kept, so re-applying 0037 restores it.
    """
    return _in_transaction(db_path, _revert)
