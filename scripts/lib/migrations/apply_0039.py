"""apply_0039.py — deliverable_dependencies, order between deliverables (ADR-096, B1).

Runs ``schemas/migrations/0039_deliverable_dependencies.sql`` inside one SAVEPOINT
(``apply_if_below``) in an outer ``BEGIN IMMEDIATE``, then checks the v39 manifest
shape of the table before the stamp: a table of that name in another shape is
refused and the store stays at 38, byte-equal. Skipped at user_version >= 39.
Applied by: scripts/lib/migrations/auto_apply.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import schema_manifest
import schema_migration

_TARGET_VERSION = 39
TABLE = "deliverable_dependencies"


class Migration0039Error(RuntimeError):
    """The store did not reach the 0039 shape; the migration rolled back."""


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """True if applied, False if skipped (user_version >= 39); a failure raises, store unchanged."""
    sql = Path(migration_sql_path).read_text(encoding="utf-8")

    def _migrate(conn: sqlite3.Connection) -> None:
        for statement in _statements(sql):
            conn.execute(statement)
        violations = schema_manifest.validate_table(conn, schema_manifest.table_at(_TARGET_VERSION, TABLE))
        if violations:
            raise Migration0039Error(f"apply_0039: {TABLE} did not reach the 0039 shape: {violations}")

    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # autocommit: required for SAVEPOINT semantics
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = schema_migration.apply_if_below(conn, _TARGET_VERSION, _migrate)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return applied
    finally:
        conn.close()


def _statements(sql: str) -> list:
    """The SQL file's statements, comment lines dropped (executescript would COMMIT the SAVEPOINT)."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]
