#!/usr/bin/env python3
"""migrate_future_system.py — apply track layer migrations (schema only).

Steps:
  1. PRAGMA pre-flight: assert dispatches schema and UNIQUE constraint are intact
  2. Apply schemas/migrations/0022_track_layer.sql (idempotent via user_version)
  3. PRAGMA pre-flight: assert tracks v22 schema intact before composite-key rebuild
  4. Apply schemas/migrations/0023_tracks_tenant_scoping.sql (idempotent via user_version)
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
# Step 1: apply 0022 migration
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
# Step 2: PRAGMA pre-flight for 0023 — assert v22 tracks schema intact
# ---------------------------------------------------------------------------

_EXPECTED_TRACKS_V22_COLS = frozenset({
    'track_id', 'title', 'goal_state', 'phase', 'next_up', 'sort_order',
    'priority', 'requires_operator_promotion', 'instruction_template',
    'context_composer_rules', 'pr_ref', 'trigger_condition', 'project_id',
    'created_at', 'phase_changed_at', 'completed_at', 'metadata_json',
})


def _assert_tracks_v22_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks table is in v22 state: single-column PK, no composite indexes.

    Codex peer-review §3: preflight must check columns AND unique indexes,
    not just column names.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for required in ('tracks', 'track_phase_history', 'track_dependencies', 'track_open_items'):
        if required not in tables:
            raise RuntimeError(
                f"Required table '{required}' not found. "
                "Run migration 0022 before 0023."
            )

    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    missing = _EXPECTED_TRACKS_V22_COLS - cols
    if missing:
        raise RuntimeError(
            f"tracks schema drift before v23 migration: missing columns={missing}. "
            "Expected v22 state."
        )

    # Guard: if composite PK already present (ux_tracks_next_up_per_project from v23),
    # skip — migration was already applied to this tracks table.
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    if 'ux_tracks_next_up_per_project' in indexes:
        raise RuntimeError(
            "tracks already has v23 composite index 'ux_tracks_next_up_per_project'. "
            "Migration 0023 should be skipped (user_version should be >= 23)."
        )


schema_migration.register_preflight(23, _assert_tracks_v22_intact)


# ---------------------------------------------------------------------------
# Step 3: apply 0023 migration
# ---------------------------------------------------------------------------

def apply_migration_v23(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0023_tracks_tenant_scoping.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 23:
        print(f"  [skip] migration 0023 already applied (user_version={current_version})")
        return

    _assert_tracks_v22_intact(conn)
    print("  [apply] migration 0023_tracks_tenant_scoping.sql ...")
    schema_migration.apply_script_if_below(conn, 23, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project_root: Path | None = None) -> None:
    """Apply track layer migrations: 0022 (track tables) + 0023 (tenant-scoping)."""
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
        current_ver = schema_migration.get_user_version(conn)

        if current_ver < 22:
            _assert_dispatches_schema_intact(conn)

        # Apply 0022 — creates track tables; dispatches rebuilt WITHOUT track FK
        apply_migration(conn, project_root)
        conn.commit()

        # Apply 0023 — rebuilds track tables with composite (track_id, project_id) PKs
        apply_migration_v23(conn, project_root)
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
