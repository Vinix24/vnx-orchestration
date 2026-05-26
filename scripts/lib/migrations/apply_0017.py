"""apply_0017.py — Wave 5 PR-5.3 multi-tenant lease isolation migration.

Applies schemas/migrations/0017_multi_tenant_lease_isolation.sql to a
runtime_coordination.db. Adds composite UNIQUE constraints on terminal_leases
and dispatches; adds project_id to worker_states; fixes dispatch_attempts FK.

Idempotent: reads MAX(version) from runtime_schema_version. Skips if already
at v12 or higher (the version stamped by the migration).

Atomic: the SQL script uses an explicit BEGIN/COMMIT transaction. If the script
fails mid-way, the uncommitted transaction is rolled back when the connection
is closed (SQLite WAL mode guarantees).

ADR-005: emits NDJSON audit events to .vnx-data/events/schema_migrations.ndjson
for migration_started, migration_completed, and migration_failed.

Tested via tests/test_schema_0017_migration.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
from coordination_db import get_connection_for_db

log = logging.getLogger(__name__)

_TARGET_VERSION = 12

# Matches the bare ``ALTER TABLE worker_states ADD COLUMN project_id ...;``
# statement in 0017. SQLite has no ``ADD COLUMN IF NOT EXISTS``, so a bare
# ALTER raises "duplicate column name" if the column already exists. When the
# idempotent init path (project_id_migration.run_runtime_coordination_migration)
# has already self-healed worker_states.project_id, this statement must become
# a no-op so 0017 can still run its terminal_leases/dispatches composite-UNIQUE
# rebuild without erroring. The companion ``CREATE INDEX IF NOT EXISTS
# idx_worker_states_project`` line is already idempotent and is left intact.
_WORKER_STATES_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+worker_states\s+ADD\s+COLUMN\s+project_id\b[^;]*;",
    re.IGNORECASE,
)

_DEFAULT_MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "schemas"
    / "migrations"
    / "0017_multi_tenant_lease_isolation.sql"
)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *table* exists and has *column* (PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _emit_migration_event(vnx_data_dir: Path, event_type: str, payload: dict) -> None:
    events_path = vnx_data_dir / "events" / "schema_migrations.ndjson"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "source": "schema_migration",
        "migration": "0017_multi_tenant_lease_isolation",
        **payload,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def apply_migration(
    db_path: Path,
    migration_sql_path: Path,
    vnx_data_dir: Path | None = None,
) -> bool:
    """Apply the 0017 migration to db_path.

    Returns True when the migration was applied, False when the DB was
    already at the target version and the migration was skipped.

    Raises sqlite3.Error on failure (the failing transaction is rolled back
    via connection close before the exception propagates).
    """
    if vnx_data_dir is None:
        vnx_data_dir = Path(db_path).parent.parent

    current_version = 0

    with get_connection_for_db(db_path) as conn:
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

            _emit_migration_event(
                vnx_data_dir,
                "migration_started",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )

            sql = migration_sql_path.read_text()

            # Column-guard: if worker_states.project_id was already self-healed
            # by the init path (project_id_migration), neutralise 0017's bare
            # ADD COLUMN so it does not raise "duplicate column name". The
            # composite-UNIQUE rebuild for terminal_leases/dispatches is left
            # untouched. See _WORKER_STATES_ADD_COLUMN_RE.
            if _column_exists(conn, "worker_states", "project_id"):
                sql, n_subs = _WORKER_STATES_ADD_COLUMN_RE.subn(
                    "-- worker_states.project_id already present; "
                    "ADD COLUMN skipped (column-guard, OI-095)",
                    sql,
                )
                if n_subs:
                    log.info(
                        "apply_0017: worker_states.project_id already present; "
                        "skipped %d ADD COLUMN statement(s)",
                        n_subs,
                    )

            conn.executescript(sql)
            log.info(
                "apply_0017: migrated from v%s to v%s", current_version, _TARGET_VERSION
            )

            _emit_migration_event(
                vnx_data_dir,
                "migration_completed",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )
            return True

        except sqlite3.Error as e:
            conn.rollback()
            log.error("apply_0017: error during migration; transaction rolled back")
            _emit_migration_event(
                vnx_data_dir,
                "migration_failed",
                {"from_version": current_version, "error": str(e)},
            )
            raise


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
    p.add_argument(
        "--vnx-data-dir",
        default=None,
        help="Path to .vnx-data directory for audit events (default: db_path/../..)",
    )
    args = p.parse_args()
    applied = apply_migration(
        Path(args.db),
        Path(args.migration),
        Path(args.vnx_data_dir) if args.vnx_data_dir else None,
    )
    print("applied" if applied else "skipped (already at target version)")
