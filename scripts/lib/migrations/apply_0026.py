"""apply_0026.py — N-1 dispatch claim primitive migration.

Applies 0026_dispatch_claim.sql logic to runtime_coordination.db:
  - Adds claimed_by TEXT column to dispatches (if absent)
  - Adds claimed_at TEXT column to dispatches (if absent)
  - Creates composite covering index idx_dispatch_project_state_claim
  - Stamps runtime_schema_version with v15

Idempotent: skips if MAX(version) >= 15. Column existence checked
individually so partial re-runs (e.g. claimed_by added, claimed_at missing)
complete safely. CREATE INDEX IF NOT EXISTS handles re-runs.

ADR-007: claimed_by/claimed_at are project_id-scoped. The composite index
and claim query in claim_next_queued_dispatch ensure cross-project isolation.
See docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md.

ADR-005: emits NDJSON audit events to .vnx-data/events/schema_migrations.ndjson.

Tested by: tests/test_claim_next_queued_dispatch.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
from coordination_db import get_connection_for_db

log = logging.getLogger(__name__)

_TARGET_VERSION = 15
_MIGRATION_NAME = "0026_dispatch_claim"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DEFAULT_MIGRATION_SQL = _REPO_ROOT / "schemas" / "migrations" / "0026_dispatch_claim.sql"


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


def _col_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (checked via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def apply_migration(
    db_path: Path,
    migration_sql_path: Path,
    vnx_data_dir: Path | None = None,
) -> bool:
    """Apply the 0026 up-migration to db_path.

    Returns True when applied, False when the DB was already at or above
    the target version (idempotent skip). Raises sqlite3.Error on failure.

    migration_sql_path is accepted for API compatibility with auto_apply;
    the actual schema changes are applied directly via Python for column
    existence guards.
    """
    if vnx_data_dir is None:
        vnx_data_dir = Path(db_path).parent.parent

    current_version = 0

    with get_connection_for_db(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT MAX(version) FROM runtime_schema_version"
            ).fetchone()
            current_version = int(row[0]) if (row and row[0] is not None) else 0

            if current_version >= _TARGET_VERSION:
                log.info("apply_0026: already at v%s; idempotent skip", _TARGET_VERSION)
                return False

            _emit_migration_event(
                vnx_data_dir,
                "migration_started",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )

            # Self-heal: project_id is required for the composite index (added by migration 0010).
            # On DBs that skipped 0010 (e.g. fresh test installs), add it here.
            if not _col_exists(conn, "dispatches", "project_id"):
                conn.execute(
                    "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
                )

            # Add claimed_by + claimed_at columns (idempotent: check each individually)
            if not _col_exists(conn, "dispatches", "claimed_by"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN claimed_by TEXT")
            if not _col_exists(conn, "dispatches", "claimed_at"):
                conn.execute("ALTER TABLE dispatches ADD COLUMN claimed_at TEXT")

            # Composite covering index for claim query (IF NOT EXISTS = safe on re-run)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dispatch_project_state_claim
                    ON dispatches(project_id, state, priority, created_at)
                """
            )

            # Stamp version
            conn.execute(
                "INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (?, ?)",
                (
                    _TARGET_VERSION,
                    "N-1 PR-N-1: claimed_by/claimed_at columns + project_state index for atomic queue claim",
                ),
            )
            conn.commit()

            log.info(
                "apply_0026: migrated from v%s to v%s", current_version, _TARGET_VERSION
            )
            _emit_migration_event(
                vnx_data_dir,
                "migration_completed",
                {"from_version": current_version, "to_version": _TARGET_VERSION},
            )
            return True

        except sqlite3.Error as e:
            conn.rollback()
            log.error("apply_0026: error during migration; transaction rolled back")
            _emit_migration_event(
                vnx_data_dir,
                "migration_failed",
                {"from_version": current_version, "error": str(e)},
            )
            raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(
        description="Apply 0026 dispatch claim migration (N-1 PR-N-1)"
    )
    p.add_argument("--db", required=True, help="Path to runtime_coordination.db")
    p.add_argument(
        "--migration",
        default=str(_DEFAULT_MIGRATION_SQL),
        help="Path to 0026_dispatch_claim.sql",
    )
    p.add_argument(
        "--vnx-data-dir",
        default=None,
        help="Path to .vnx-data directory for audit events",
    )
    args = p.parse_args()
    applied = apply_migration(
        Path(args.db),
        Path(args.migration),
        Path(args.vnx_data_dir) if args.vnx_data_dir else None,
    )
    print("applied" if applied else "skipped (already at target version)")
