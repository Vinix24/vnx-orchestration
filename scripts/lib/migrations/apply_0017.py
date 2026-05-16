"""apply_0017.py — Wave 5 PR-5.3 multi-tenant lease isolation migration.

Applies schemas/migrations/0017_multi_tenant_lease_isolation.sql to a
runtime_coordination.db. Adds composite UNIQUE constraints on terminal_leases
and dispatches; adds project_id to worker_states.

Idempotent: reads MAX(version) from runtime_schema_version. Skips if already
at v12 or higher (the version stamped by the migration).

Atomic: the SQL script uses an explicit BEGIN/COMMIT transaction. If the script
fails mid-way, the uncommitted transaction is rolled back when the connection
is closed (SQLite WAL mode guarantees).

Tested via tests/test_schema_0017_migration.py.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_TARGET_VERSION = 12

_DEFAULT_MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "schemas"
    / "migrations"
    / "0017_multi_tenant_lease_isolation.sql"
)


def apply_migration(db_path: Path, migration_sql_path: Path) -> bool:
    """Apply the 0017 migration to db_path.

    Returns True when the migration was applied, False when the DB was
    already at the target version and the migration was skipped.

    Raises sqlite3.Error on failure (the failing transaction is rolled back
    via connection close before the exception propagates).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(version) FROM runtime_schema_version")
        row = cur.fetchone()
        current_version = int(row[0]) if (row and row[0] is not None) else 0

        if current_version >= _TARGET_VERSION:
            log.info(
                "apply_0017: already at v%s (target v%s), skip",
                current_version,
                _TARGET_VERSION,
            )
            return False

        sql = migration_sql_path.read_text()
        conn.executescript(sql)
        log.info(
            "apply_0017: migrated from v%s to v%s", current_version, _TARGET_VERSION
        )
        return True

    except sqlite3.Error:
        conn.rollback()
        log.error("apply_0017: error during migration; transaction rolled back")
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(
        description="Apply 0017 multi-tenant lease isolation migration"
    )
    p.add_argument("--db", required=True, help="Path to runtime_coordination.db")
    p.add_argument(
        "--migration",
        default=str(_DEFAULT_MIGRATION_SQL),
        help="Path to 0017_multi_tenant_lease_isolation.sql",
    )
    args = p.parse_args()
    applied = apply_migration(Path(args.db), Path(args.migration))
    print("applied" if applied else "skipped (already at target version)")
