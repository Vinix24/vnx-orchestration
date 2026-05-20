"""Schema versioning utilities for VNX central databases.

Provides a unified schema_meta key/value table for migration version tracking.
Orthogonal to runtime_schema_version (integer row table in runtime_coordination.db)
and schema_version (text PK table in quality_intelligence.db).
"""

from __future__ import annotations

import sqlite3

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""


def ensure_schema_meta(conn: sqlite3.Connection, initial_version: int = 0) -> None:
    """Create schema_meta if absent; seed schema_version = initial_version."""
    conn.execute(_SCHEMA_META_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(initial_version),),
    )


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return current schema_version from schema_meta, 0 if table is absent."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set schema_version in schema_meta, creating the table if absent."""
    ensure_schema_meta(conn)
    conn.execute(
        """INSERT INTO schema_meta(key, value, updated_at)
           VALUES ('schema_version', ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
               value      = excluded.value,
               updated_at = excluded.updated_at""",
        (str(version),),
    )


def check_schema_version(
    conn: sqlite3.Connection,
    expected_version: int,
    migration_name: str,
) -> bool:
    """Verify schema_version matches expected_version.

    Returns True when the migration should proceed (version == expected).
    Returns False when the DB is already past this migration (version > expected)
    -- the caller should skip the migration.
    Raises RuntimeError when version < expected (missing prerequisites).
    """
    ensure_schema_meta(conn)
    current = get_schema_version(conn)

    if current == expected_version:
        return True

    if current > expected_version:
        return False

    # current < expected_version -- prerequisites not met
    raise RuntimeError(
        f"Migration '{migration_name}' requires schema_version={expected_version} "
        f"but DB is at schema_version={current}. "
        f"Run migrations sequentially to reach v{expected_version} before applying "
        f"'{migration_name}'. See docs/operations/migration-rollback-runbook.md."
    )
