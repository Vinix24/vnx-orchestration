"""tests/test_migrate_v24_stale_0023_compat.py — backward-compat with stale 0023_dispatches_fk.

Verifies that apply_migration_v24 strips a stale dispatches.track -> tracks(track_id) FK
that the superseded 0023_dispatches_fk.sql may have added, before executing the 0024
tracks RENAME. Without the strip, the RENAME fails with FK constraint violations.

Operator path C: applied 0022 → stale 0023_dispatches_fk → now applying 0024.
"""
from __future__ import annotations

import sqlite3
import sys
import warnings as _warnings
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
import migrate_future_system


def _base_db_v22() -> sqlite3.Connection:
    """Create a v22 DB (dispatches + coordination_events + track tables)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT,
            entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()
    sql_v22 = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 22, sql_v22)
    conn.commit()
    return conn


def _apply_stale_0023_dispatches_fk(conn: sqlite3.Connection) -> None:
    """Inline the body of the superseded 0023_dispatches_fk.sql.

    That migration added a FK on dispatches.track -> tracks(track_id).
    It was removed in FUT-1 Option B scope-shrink; we simulate an operator
    who applied it before the removal.

    FK enforcement must be OFF since existing dispatch rows have track=NULL.
    """
    col_names = [row[1] for row in conn.execute("PRAGMA table_info('dispatches')")]
    col_list = ", ".join(col_names)

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE dispatches RENAME TO dispatches_pre_stale_fk")
    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            operator_approved_at TEXT,
            UNIQUE(dispatch_id, project_id),
            FOREIGN KEY (track) REFERENCES tracks(track_id)
        )
    """)
    conn.execute(
        f"INSERT INTO dispatches ({col_list}) SELECT {col_list} FROM dispatches_pre_stale_fk"
    )
    conn.execute("DROP TABLE dispatches_pre_stale_fk")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_v24_succeeds_with_stale_0023_fk_present():
    """apply_migration_v24 must succeed even when the stale dispatches->tracks FK exists."""
    conn = _base_db_v22()
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-001', 'queued')"
    )
    conn.commit()

    _apply_stale_0023_dispatches_fk(conn)

    # Confirm stale FK is present before v24
    fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    assert fks, "Stale FK must exist before migration for this test to be meaningful"

    # apply_migration_v24 should detect and strip the FK, then proceed
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    # Exactly one warning about the stale FK
    stale_warnings = [w for w in caught if "stale" in str(w.message).lower()]
    assert stale_warnings, "Expected UserWarning about stale FK stripping"


def test_no_tracks_pre_v24_table_after_migration():
    """tracks_pre_v24 must be dropped by STEP 5 of 0024 migration."""
    conn = _base_db_v22()
    _apply_stale_0023_dispatches_fk(conn)

    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "tracks_pre_v24" not in tables, "tracks_pre_v24 must be dropped after migration"


def test_dispatches_fk_to_tracks_removed_after_migration():
    """After v24, dispatches must not carry the stale tracks(track_id) FK."""
    conn = _base_db_v22()
    _apply_stale_0023_dispatches_fk(conn)

    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    remaining_fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    assert not remaining_fks, (
        "dispatches must not have FK to tracks(track_id) after v24 migration"
    )


def test_dispatch_rows_preserved_after_stale_fk_strip():
    """All dispatch rows must survive the stale-FK strip + v24 migration."""
    conn = _base_db_v22()
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-001', 'queued')"
    )
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-002', 'active')"
    )
    conn.commit()

    _apply_stale_0023_dispatches_fk(conn)

    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    dispatch_ids = {
        r[0] for r in conn.execute("SELECT dispatch_id FROM dispatches").fetchall()
    }
    assert "d-001" in dispatch_ids, "d-001 must be preserved"
    assert "d-002" in dispatch_ids, "d-002 must be preserved"


def test_strip_stale_fk_preserves_dispatches_seq_high_water():
    """_strip_stale_dispatches_track_fk must preserve the sqlite_sequence high-water mark.

    Pattern: apply stale-FK (rebuilds dispatches, seq resets), then insert id=1 and id=100,
    delete id=100 → seq=100, max(id)=1. Apply v24 (triggers stale-FK strip). Assert seq
    remains >= 100 so the next insert lands at id=101, not id=2.

    Note: high-water seeding happens AFTER _apply_stale_0023_dispatches_fk, because that
    helper rebuilds dispatches itself and would regress an earlier seed.
    """
    conn = _base_db_v22()

    # Apply stale FK first (dispatches rebuilt from empty, seq resets)
    _apply_stale_0023_dispatches_fk(conn)

    # Seed high-water AFTER the stale FK rebuild (track=NULL is FK-permitted)
    conn.execute("INSERT INTO dispatches (id, dispatch_id, state) VALUES (1, 'd-001', 'queued')")
    conn.execute("INSERT INTO dispatches (id, dispatch_id, state) VALUES (100, 'd-100', 'queued')")
    conn.commit()
    conn.execute("DELETE FROM dispatches WHERE id = 100")
    conn.commit()

    # Pre-condition: seq must be 100 at this point
    seq_before = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'dispatches'"
    ).fetchone()
    assert seq_before is not None and seq_before[0] >= 100, (
        f"Precondition: seq should be >= 100 after inserting id=100, got {seq_before}"
    )

    # Confirm stale FK present before v24
    fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    assert fks, "Stale FK must exist before migration for this test to be meaningful"

    import warnings as _warnings
    with _warnings.catch_warnings(record=True):
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    # Assert seq not regressed
    seq_after = conn.execute(
        "SELECT MAX(seq) FROM sqlite_sequence WHERE name = 'dispatches'"
    ).fetchone()[0]
    assert seq_after is not None and seq_after >= 100, (
        f"sqlite_sequence.seq for 'dispatches' regressed to {seq_after} after stale-FK strip + v24"
    )

    # Assert next insert lands at id=101, not id=2
    conn.execute("INSERT INTO dispatches (dispatch_id, state) VALUES ('d-new', 'queued')")
    conn.commit()
    new_id = conn.execute(
        "SELECT id FROM dispatches WHERE dispatch_id = 'd-new'"
    ).fetchone()[0]
    assert new_id >= 101, (
        f"New dispatch id={new_id} — seq was not preserved through stale-FK strip"
    )


def test_v24_clean_path_unaffected_by_stale_fk_logic():
    """Without stale FK, apply_migration_v24 proceeds normally with no extra warning."""
    conn = _base_db_v22()
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('d-001', 'queued')"
    )
    conn.commit()

    # NO stale FK applied
    fks = [
        row for row in conn.execute("PRAGMA foreign_key_list('dispatches')")
        if row[2] == "tracks" and row[4] == "track_id"
    ]
    assert not fks, "Baseline: no stale FK present"

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    stale_warnings = [w for w in caught if "stale" in str(w.message).lower()]
    assert not stale_warnings, "No stale FK warning expected on clean path"

    assert schema_migration.get_user_version(conn) == 24
