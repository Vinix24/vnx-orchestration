#!/usr/bin/env python3
"""migrate_future_system.py — apply track layer migrations (schema only).

Steps:
  1. PRAGMA pre-flight: assert dispatches schema and UNIQUE constraint are intact
  2. Apply schemas/migrations/0022_track_layer.sql (idempotent via user_version)
  3. PRAGMA pre-flight: assert tracks v22 schema intact before composite-key rebuild
  4. Apply schemas/migrations/0024_tracks_tenant_scoping.sql (idempotent via user_version)
  5. PRAGMA pre-flight: assert tracks composite-key schema intact before adding horizon
  6. Apply schemas/migrations/0027_planning_horizon_and_deliverable_view.sql (idempotent)
  7. PRAGMA pre-flight: assert tracks has horizon (v27) before adding derived_status
  8. Apply schemas/migrations/0028_tracks_derived_status.sql (idempotent)
  9. PRAGMA pre-flight: assert tracks has derived_status (v28) before adding track_type
  10. Apply schemas/migrations/0029_track_type_discriminator.sql (idempotent)
  11. PRAGMA pre-flight: assert track_type present (v29) before adding resolved_at
  12. Apply schemas/migrations/0030_track_oi_resolved_at.sql (idempotent)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import warnings
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
# Test isolation guard (R8.6 / PR-0) — active only under pytest
# ---------------------------------------------------------------------------

def _pytest_db_isolation_guard() -> None:
    """Refuse to open any DB when running under pytest without explicit isolation.

    Active only when PYTEST_CURRENT_TEST is set (i.e. inside a pytest process).
    Callers must set VNX_DATA_DIR_EXPLICIT=1 (and VNX_DATA_DIR=<tmp path>)
    to signal that the _fsr_migration_module_isolation fixture is active.

    Production code is never affected: PYTEST_CURRENT_TEST is only set by pytest.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is None:
        return
    if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
        return
    raise RuntimeError(
        "[TEST ISOLATION GUARD] migrate_future_system.run() called under pytest "
        "without VNX_DATA_DIR_EXPLICIT=1. This would open the live database. "
        "Ensure the _fsr_migration_module_isolation fixture is active (tests/conftest.py), "
        "or set VNX_DATA_DIR_EXPLICIT=1 and VNX_DATA_DIR=<tmp_path> in your test."
    )


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
# Step 2: PRAGMA pre-flight for 0024 — assert v22 tracks schema intact
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
                "Run migration 0022 before 0024."
            )

    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    missing = _EXPECTED_TRACKS_V22_COLS - cols
    if missing:
        raise RuntimeError(
            f"tracks schema drift before v24 migration: missing columns={missing}. "
            "Expected v22 state."
        )

    # Guard: if composite PK already present (ux_tracks_next_up_per_project from v24),
    # skip — migration was already applied to this tracks table.
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    if 'ux_tracks_next_up_per_project' in indexes:
        raise RuntimeError(
            "tracks already has v24 composite index 'ux_tracks_next_up_per_project'. "
            "Migration 0024 should be skipped (user_version should be >= 24)."
        )


schema_migration.register_preflight(24, _assert_tracks_v22_intact)


# ---------------------------------------------------------------------------
# Step 3: orphan warning check before v24 migration
# ---------------------------------------------------------------------------

def _warn_orphan_child_rows(conn: sqlite3.Connection) -> None:
    """Check for orphan child rows before v24 migration and warn. Does not block."""
    checks = [
        ("track_phase_history", "track_phase_history", "track_id"),
        ("track_dependencies (from_track_id)", "track_dependencies", "from_track_id"),
        ("track_dependencies (to_track_id)", "track_dependencies", "to_track_id"),
        ("track_open_items", "track_open_items", "track_id"),
    ]
    for label, table, col in checks:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} NOT IN (SELECT track_id FROM tracks)"
        ).fetchone()[0]
        if count:
            warnings.warn(
                f"v24 migration: {count} orphan row(s) in {label} "
                f"({col} not in tracks) will be skipped",
                UserWarning,
                stacklevel=3,
            )


# ---------------------------------------------------------------------------
# Stale-FK repair: strip dispatches.track -> tracks(track_id) FK if present
# ---------------------------------------------------------------------------

def _strip_stale_dispatches_track_fk(conn: sqlite3.Connection) -> None:
    """Remove the stale dispatches.track -> tracks(track_id) FK via table rebuild.

    The superseded 0023_dispatches_fk.sql added this FK before it was removed
    in FUT-1 Option B scope-shrink. If an operator applied that migration before
    upgrading, the tracks RENAME in 0024 breaks unless the FK is stripped first.
    This repair is safe: the FK existed only in the operator-side superseded
    0023 application and carries no semantic constraint we need to preserve.
    """
    col_names = [row[1] for row in conn.execute("PRAGMA table_info('dispatches')")]
    col_list = ", ".join(col_names)

    conn.execute("ALTER TABLE dispatches RENAME TO dispatches_pre_v24_strip")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'proposed'
                                    CHECK (state IN (
                                        'proposed', 'ready', 'active', 'completed', 'failed',
                                        'queued', 'claimed', 'delivering', 'accepted', 'running',
                                        'timed_out', 'failed_delivery', 'expired', 'recovered',
                                        'dead_letter'
                                    )),
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            operator_approved_at TEXT,
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute(
        f"INSERT INTO dispatches ({col_list}) SELECT {col_list} FROM dispatches_pre_v24_strip"
    )
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'dispatches'")
    conn.execute("""
        INSERT INTO sqlite_sequence(name, seq)
        SELECT 'dispatches',
               COALESCE(
                   (SELECT seq FROM sqlite_sequence WHERE name = 'dispatches_pre_v24_strip'),
                   (SELECT MAX(id) FROM dispatches),
                   0
               )
    """)
    conn.execute("DROP TABLE dispatches_pre_v24_strip")


# ---------------------------------------------------------------------------
# v22 timestamp dedup: prevent UNIQUE(track_id, project_id, occurred_at) rejection
# ---------------------------------------------------------------------------

def _dedupe_v22_phase_history_timestamps(conn: sqlite3.Connection) -> None:
    """v22 occurred_at default is millisecond precision; bulk transitions
    can share timestamps. Composite UNIQUE in v24 would reject those.
    Append microsecond offset (.0001Z, .0002Z, ...) to make timestamps
    distinct while preserving chronological order via stable id ordering.

    KNOWN LIMITATIONS (tracked in OI-008 + GitHub roadmap):
    - Dedupe-suffix collision possible if pre-existing v22 data has
      timestamps matching the post-dedupe format (.NNN0001Z). Real-world
      probability near-zero for default v22 strftime '%f' timestamps.
    - Suffix '.0001Z' does not sort lex with '.NNNZ'. Chronological
      ordering preserved via id sequence, not timestamp string sort.
    """
    rows = conn.execute("""
        SELECT id, occurred_at,
               ROW_NUMBER() OVER (PARTITION BY track_id, occurred_at ORDER BY id) - 1 AS offset
        FROM track_phase_history
        ORDER BY id
    """).fetchall()
    for row_id, occurred_at, offset in rows:
        if offset > 0:
            base = occurred_at.rstrip("Z")
            new_ts = (
                f"{base}{offset:04d}Z"
                if "." in base.rsplit("T", 1)[-1]
                else f"{base}.{offset:04d}Z"
            )
            conn.execute(
                "UPDATE track_phase_history SET occurred_at = ? WHERE id = ?",
                (new_ts, row_id),
            )


# ---------------------------------------------------------------------------
# Step 4: apply 0024 migration
# ---------------------------------------------------------------------------

def apply_migration_v24(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0024_tracks_tenant_scoping.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 24:
        print(f"  [skip] migration 0024 already applied (user_version={current_version})")
        return

    _assert_tracks_v22_intact(conn)

    # Detect and strip stale FK from superseded 0023_dispatches_fk.sql.
    # If operator applied that migration, dispatches has a FK to tracks(track_id)
    # that would break the tracks RENAME in 0024.
    stale_fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    if stale_fks:
        warnings.warn(
            "Detected stale dispatches.track -> tracks(track_id) FK from superseded "
            "0023_dispatches_fk.sql. Stripping FK before applying 0024.",
            UserWarning,
            stacklevel=2,
        )
        _strip_stale_dispatches_track_fk(conn)

    _dedupe_v22_phase_history_timestamps(conn)
    _warn_orphan_child_rows(conn)
    print("  [apply] migration 0024_tracks_tenant_scoping.sql ...")
    schema_migration.apply_script_if_below(conn, 24, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 5: PRAGMA pre-flight for 0027 — assert composite-key tracks intact
# ---------------------------------------------------------------------------

def _ensure_dispatches_output_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ensure dispatches carries output_ref + output_kind columns.

    Migration 0027 creates the deliverables VIEW which reads dispatches.output_ref
    and dispatches.output_kind. On the live DB these columns were added by the
    structural-doctor repair step, but a fresh DB that arrives at v24 without the
    structural-doctor pass (or via tests) will not have them. The VIEW creation
    does not fail at DDL time (SQLite resolves view columns at query time), but
    any SELECT from deliverables would fail.

    This preflight adds the columns additively when they are absent, then back-
    fills output_ref=pr_ref, output_kind='pr' for rows where pr_ref is set.
    It is idempotent: column-existence checks guard the ALTER TABLE calls so
    they are never attempted twice, and the UPDATE is a no-op after the first run.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}

    if "output_ref" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    if "output_kind" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    if "operator_approved_at" not in cols:
        conn.execute("ALTER TABLE dispatches ADD COLUMN operator_approved_at TEXT")

    conn.execute(
        "UPDATE dispatches SET output_ref = pr_ref, output_kind = 'pr' "
        "WHERE pr_ref IS NOT NULL AND output_ref IS NULL"
    )


def _assert_tracks_v24_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks is in the composite-key (v24+) state before adding horizon.

    0027 is purely additive (ALTER TABLE ADD COLUMN + a VIEW), so it only needs
    the tracks table to exist with its composite-key index. It must NOT run on a
    pre-v24 single-column-PK tracks table.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if 'tracks' not in tables:
        raise RuntimeError(
            "Required table 'tracks' not found. Run migrations 0022 + 0024 before 0027."
        )
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    if 'ux_tracks_next_up_per_project' not in indexes:
        raise RuntimeError(
            "tracks missing composite-key index 'ux_tracks_next_up_per_project' "
            "(from 0024). Run migration 0024 before 0027."
        )
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if 'horizon' in cols:
        raise RuntimeError(
            "tracks already has 'horizon' column. Migration 0027 should be "
            "skipped (user_version should be >= 27)."
        )


schema_migration.register_preflight(27, _ensure_dispatches_output_columns)
schema_migration.register_preflight(27, _assert_tracks_v24_intact)


# ---------------------------------------------------------------------------
# Step 6: apply 0027 migration
# ---------------------------------------------------------------------------

def apply_migration_v27(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 27:
        print(f"  [skip] migration 0027 already applied (user_version={current_version})")
        return

    _assert_tracks_v24_intact(conn)
    print("  [apply] migration 0027_planning_horizon_and_deliverable_view.sql ...")
    schema_migration.apply_script_if_below(conn, 27, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 7: preflight + apply 0028 migration (tracks.derived_status)
# ---------------------------------------------------------------------------

def _assert_tracks_v27_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks has horizon column (v27 state) before adding derived_status."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "horizon" not in cols:
        raise RuntimeError(
            "tracks missing 'horizon' column (from 0027). "
            "Run migration 0027 before 0028."
        )
    if "derived_status" in cols:
        raise RuntimeError(
            "tracks already has 'derived_status' column. Migration 0028 should be "
            "skipped (user_version should be >= 28)."
        )


schema_migration.register_preflight(28, _assert_tracks_v27_intact)


def apply_migration_v28(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0028_tracks_derived_status.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 28:
        print(f"  [skip] migration 0028 already applied (user_version={current_version})")
        return

    _assert_tracks_v27_intact(conn)
    print("  [apply] migration 0028_tracks_derived_status.sql ...")
    schema_migration.apply_script_if_below(conn, 28, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 8: preflight + apply 0029 migration (tracks.track_type + next_action_owner)
# ---------------------------------------------------------------------------

def _assert_tracks_v28_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks has derived_status (v28 state) before adding track_type.

    Also guards against double-apply by rejecting if track_type already exists.
    Column-presence check via PRAGMA table_info provides a secondary idempotency
    guard beyond the user_version check in apply_script_if_below.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "derived_status" not in cols:
        raise RuntimeError(
            "tracks missing 'derived_status' column (from 0028). "
            "Run migration 0028 before 0029."
        )
    if "track_type" in cols:
        raise RuntimeError(
            "tracks already has 'track_type' column. Migration 0029 should be "
            "skipped (user_version should be >= 29)."
        )


schema_migration.register_preflight(29, _assert_tracks_v28_intact)


def apply_migration_v29(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0029_track_type_discriminator.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 29:
        print(f"  [skip] migration 0029 already applied (user_version={current_version})")
        return

    _assert_tracks_v28_intact(conn)
    print("  [apply] migration 0029_track_type_discriminator.sql ...")
    schema_migration.apply_script_if_below(conn, 29, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 9: preflight + apply 0030 migration (track_open_items.resolved_at)
# ---------------------------------------------------------------------------

def _assert_tracks_v29_intact(conn: sqlite3.Connection) -> None:
    """Assert tracks has track_type (v29 state) before adding resolved_at to track_open_items.

    Also guards against double-apply by rejecting if resolved_at already exists.
    """
    track_cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    if "track_type" not in track_cols:
        raise RuntimeError(
            "tracks missing 'track_type' column (from 0029). "
            "Run migration 0029 before 0030."
        )
    oi_cols = {row[1] for row in conn.execute("PRAGMA table_info('track_open_items')")}
    if "resolved_at" in oi_cols:
        raise RuntimeError(
            "track_open_items already has 'resolved_at' column. Migration 0030 should be "
            "skipped (user_version should be >= 30)."
        )


schema_migration.register_preflight(30, _assert_tracks_v29_intact)


def apply_migration_v30(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0030_track_oi_resolved_at.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 30:
        print(f"  [skip] migration 0030 already applied (user_version={current_version})")
        return

    _assert_tracks_v29_intact(conn)
    print("  [apply] migration 0030_track_oi_resolved_at.sql ...")
    schema_migration.apply_script_if_below(conn, 30, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project_root: Path | None = None) -> None:
    """Apply track layer migrations: 0022, 0024, 0027, 0028, 0029, 0030."""
    _pytest_db_isolation_guard()

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

        # Apply 0024 — rebuilds track tables with composite (track_id, project_id) PKs
        apply_migration_v24(conn, project_root)
        conn.commit()

        # Apply 0027 — additive: tracks.horizon column + deliverables derived view
        apply_migration_v27(conn, project_root)
        conn.commit()

        # Apply 0028 — additive: tracks.derived_status advisory column
        apply_migration_v28(conn, project_root)
        conn.commit()

        # Apply 0029 — additive: tracks.track_type + tracks.next_action_owner
        apply_migration_v29(conn, project_root)
        conn.commit()

        # Apply 0030 — additive: track_open_items.resolved_at + resolution_reason
        apply_migration_v30(conn, project_root)
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
