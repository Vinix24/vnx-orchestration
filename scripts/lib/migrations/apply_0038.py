"""apply_0038.py — intelligence_injections carries the arm and the window it was served in (track intelligence-effect-measurable, PR-3).

Adds ``ab_arm TEXT`` and ``ab_window TEXT`` (both nullable: a row without them is
"not assigned") plus ``idx_injection_window_arm ON (project_id, ab_window, ab_arm)``
(ADR-007: project_id first). A column that is already there is left as it is: a store
that took the date-named 2026_05 ab_arm migration by hand reaches 38 without
``duplicate column name``. One ``BEGIN IMMEDIATE`` with a SAVEPOINT, as in apply_0037.
``runtime_schema_version`` is not touched, so up and down mirror each other.

Down: the user_version must be exactly 38. It drops the new index, the older
``idx_injection_ab_arm`` (SQLite refuses to drop an indexed column), then
``ab_window`` and ``ab_arm``, and stamps 37. No other column or row changes. The arm
history is lost on a down; the zero point is a fresh backup right before the up.
A store without an ``intelligence_injections`` table has nothing to add or drop
(the manifest treats the table as conditional); it is only stamped.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Callable, Dict, Tuple

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import schema_migration

_TARGET_VERSION = 38
TABLE = "intelligence_injections"
NEW_COLUMNS: Tuple[str, ...] = ("ab_arm", "ab_window")
WINDOW_INDEX = "idx_injection_window_arm"
LEGACY_ARM_INDEX = "idx_injection_ab_arm"
_MIN_SQLITE_DROP_COLUMN = (3, 35)


class Migration0038Error(RuntimeError):
    """The store did not reach (or leave) the 0038 shape; the migration rolled back."""


def _in_transaction(db_path: Path, step: Callable[[sqlite3.Connection], bool]) -> bool:
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit: required for SAVEPOINT semantics
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


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection) -> Dict[str, str]:
    return {row[1]: (row[2] or "").upper() for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone() is not None


def _migrate(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn):
        return
    present = _columns(conn)
    for column in NEW_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} TEXT")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {WINDOW_INDEX} ON {TABLE} (project_id, ab_window, ab_arm)"
    )
    after = _columns(conn)
    missing = [c for c in NEW_COLUMNS if after.get(c) != "TEXT"]
    if missing or not _index_exists(conn, WINDOW_INDEX):
        raise Migration0038Error(
            f"apply_0038: the store did not reach the 0038 shape (columns {missing}, "
            f"index {WINDOW_INDEX} present: {_index_exists(conn, WINDOW_INDEX)})"
        )


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """True if applied, False if skipped (user_version >= 38); a failure raises, store unchanged.

    *migration_sql_path* is the auto_apply runner contract: the .sql file documents, this builds.
    """
    return _in_transaction(db_path, lambda c: schema_migration.apply_if_below(c, _TARGET_VERSION, _migrate))


def _revert(conn: sqlite3.Connection) -> bool:
    """Drop 38 back to 37; False when the store already sits at 37."""
    version = schema_migration.get_user_version(conn)
    if version == _TARGET_VERSION - 1:
        return False
    if version != _TARGET_VERSION:
        raise Migration0038Error(f"apply_0038 down: needs user_version 38, found {version}")
    if _table_exists(conn):
        conn.execute(f"DROP INDEX IF EXISTS {WINDOW_INDEX}")
        conn.execute(f"DROP INDEX IF EXISTS {LEGACY_ARM_INDEX}")
        present = _columns(conn)
        for column in reversed(NEW_COLUMNS):
            if column in present:
                conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")
    conn.execute(f"PRAGMA user_version = {_TARGET_VERSION - 1}")
    return True


def revert_migration(db_path: Path) -> bool:
    """v38 -> v37 in one transaction: both columns and their indexes go, every other column and row stays.

    False at 37, Migration0038Error at any other version or on a SQLite without DROP COLUMN
    (checked before the store is opened).
    """
    if sqlite3.sqlite_version_info < _MIN_SQLITE_DROP_COLUMN:
        raise Migration0038Error(
            f"apply_0038 down: SQLite {sqlite3.sqlite_version} cannot DROP COLUMN (needs >= 3.35)"
        )
    return _in_transaction(db_path, _revert)
