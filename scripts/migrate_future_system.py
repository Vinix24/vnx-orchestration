#!/usr/bin/env python3
"""migrate_future_system.py — apply 0022_track_layer migration (schema only).

Steps:
  1. PRAGMA pre-flight: assert dispatches schema and UNIQUE constraint are intact
  2. Apply schemas/migrations/0022_track_layer.sql (idempotent via user_version)

Seeding tracks and applying 0023_dispatches_fk.sql are deferred to PR-FUT-2
where they will be designed with proper ADR-007 tenant-scoping (composite PK
over project_id for tracks and a composite-FK from dispatches.track).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
_SCHEMAS = _HERE.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root
import schema_migration


# ---------------------------------------------------------------------------
# Step 0: PRAGMA pre-flight — guard against schema drift before rebuild
# ---------------------------------------------------------------------------

def _assert_dispatches_schema_intact(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    expected = {'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
                'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
                'expires_after', 'metadata_json'}
    missing = expected - cols
    extra = cols - expected
    if missing or extra:
        raise RuntimeError(
            f'dispatches schema drift: missing={missing} extra={extra}. '
            'Refusing rebuild — please add migration logic for the new columns first.'
        )
    indexes = list(conn.execute("PRAGMA index_list('dispatches')"))
    composite_unique_exists = False
    for idx in indexes:
        if idx[2]:  # unique flag
            idx_cols = [c[2] for c in conn.execute(f"PRAGMA index_info('{idx[1]}')")]
            if set(idx_cols) == {'dispatch_id', 'project_id'}:
                composite_unique_exists = True
                break
    if not composite_unique_exists:
        raise RuntimeError(
            'dispatches missing UNIQUE(dispatch_id, project_id) — '
            'was added in migration 0017, must be preserved'
        )


# Register PRAGMA pre-flight for 0022: any call to apply_script_if_below(22, ...)
# triggers the column assertion, even when invoked outside of run().
schema_migration.register_preflight(22, _assert_dispatches_schema_intact)


# ---------------------------------------------------------------------------
# Step 1: apply migration SQL
# ---------------------------------------------------------------------------

def apply_migration(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0022_track_layer.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 22:
        print(f"  [skip] migration 0022 already applied (user_version={current_version})")
        return

    _assert_dispatches_schema_intact(conn)
    print("  [apply] migration 0022_track_layer.sql ...")
    schema_migration.apply_script_if_below(conn, 22, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project_root: Path | None = None) -> None:
    """Validate dispatches schema integrity and apply 0022_track_layer migration.

    Seeding tracks, tagging dispatches, and applying 0023_dispatches_fk are
    deferred to PR-FUT-2 (ADR-007 compliance: composite PK over project_id).
    """
    if project_root is None:
        project_root = resolve_project_root(__file__)

    state_dir = project_root / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    if not db_path.exists():
        raise FileNotFoundError(
            f"runtime_coordination.db not found at {db_path}\n"
            "Run `vnx init` or initialize the schema first."
        )

    print(f"\nVNX migrate_future_system — db: {db_path}")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        # PRAGMA pre-flight: assert dispatches schema intact before any rebuild
        current_ver = schema_migration.get_user_version(conn)
        if current_ver < 22:
            _assert_dispatches_schema_intact(conn)

        # Apply 0022 — creates track tables; dispatches rebuilt WITHOUT track FK
        apply_migration(conn, project_root)
        conn.commit()

        print(f"\n  Migration complete. Schema at user_version={schema_migration.get_user_version(conn)}.\n")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\n  [ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
