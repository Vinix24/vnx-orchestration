"""apply_0019.py — Wave 5 PR-5.2 T0 lifecycle tokens migration.

Applies schemas/migrations/0019_t0_lifecycle_tokens.sql to a
runtime_coordination.db. Adds lease_token column + partial UNIQUE index to
terminal_leases for per-incarnation lease identification.

Idempotent: reads MAX(version) from runtime_schema_version. Skips if already
at v13 or higher (the version stamped by the migration).

Atomic: the SQL script uses an explicit BEGIN/COMMIT transaction. If the script
fails mid-way, the uncommitted transaction is rolled back when the connection
is closed (SQLite WAL mode guarantees).

ADR-005: emits NDJSON audit events to .vnx-data/events/schema_migrations.ndjson
for migration_started, migration_completed, and migration_failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_TARGET_VERSION = 13
_MIGRATION_NAME = "0019_t0_lifecycle_tokens"

_DEFAULT_MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "schemas"
    / "migrations"
    / "0019_t0_lifecycle_tokens.sql"
)


def _emit_migration_event(vnx_data_dir: Path, event_type: str, payload: dict) -> None:
    events_path = vnx_data_dir / "events" / "schema_migrations.ndjson"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "source": "schema_migration",
        "migration": _MIGRATION_NAME,
        **payload,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def apply_migration(
    db_path: Path,
    migration_sql_path: Path,
    vnx_data_dir: Path | None = None,
) -> bool:
    """Apply the 0019 migration to db_path.

    Returns True when the migration was applied, False when the DB was
    already at the target version and the migration was skipped.

    Raises sqlite3.Error on failure (the failing transaction is rolled back
    via connection close before the exception propagates).
    """
    if vnx_data_dir is None:
        vnx_data_dir = Path(db_path).parent.parent

    current_version = 0
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(version) FROM runtime_schema_version")
        row = cur.fetchone()
        current_version = int(row[0]) if (row and row[0] is not None) else 0

        if current_version >= _TARGET_VERSION:
            log.info(
                "apply_0019: already at v%s (target v%s), skip",
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
        conn.executescript(sql)
        log.info(
            "apply_0019: migrated from v%s to v%s", current_version, _TARGET_VERSION
        )

        _emit_migration_event(
            vnx_data_dir,
            "migration_completed",
            {"from_version": current_version, "to_version": _TARGET_VERSION},
        )
        return True

    except sqlite3.Error as e:
        conn.rollback()
        log.error("apply_0019: error during migration; transaction rolled back")
        _emit_migration_event(
            vnx_data_dir,
            "migration_failed",
            {"from_version": current_version, "error": str(e)},
        )
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(
        description="Apply 0019 T0 lifecycle tokens migration"
    )
    p.add_argument("--db", required=True, help="Path to runtime_coordination.db")
    p.add_argument(
        "--migration",
        default=str(_DEFAULT_MIGRATION_SQL),
        help="Path to 0019_t0_lifecycle_tokens.sql",
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
