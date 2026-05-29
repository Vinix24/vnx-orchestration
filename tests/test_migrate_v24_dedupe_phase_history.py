"""tests/test_migrate_v24_dedupe_phase_history.py — timestamp dedup for v22 phase history.

v22 track_phase_history uses strftime('%Y-%m-%dT%H:%M:%fZ', 'now') as the default
occurred_at, which is millisecond precision. Bulk phase transitions in the same
millisecond produce duplicate occurred_at values. The UNIQUE(track_id, project_id,
occurred_at) constraint added in 0024 rejects those rows during INSERT.

_dedupe_v22_phase_history_timestamps appends a microsecond offset to duplicates
before the SQL migration runs, making all occurred_at values distinct while
preserving chronological order via stable id ordering.
"""
from __future__ import annotations

import sqlite3
import sys
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


def _seed_track(conn: sqlite3.Connection, track_id: str = "track-01") -> None:
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES (?, 'T', 'G')",
        (track_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_shared_timestamp_rows_all_preserved_after_v24():
    """3 rows sharing the same occurred_at must all survive the v24 migration."""
    conn = _base_db_v22()
    _seed_track(conn)

    shared_ts = "2026-05-29T12:00:00.123Z"
    for from_p, to_p in [("queued", "active"), ("active", "parked"), ("parked", "done")]:
        conn.execute(
            "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES ('track-01', ?, ?, 'operator', ?)",
            (from_p, to_p, shared_ts),
        )
    conn.commit()

    migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-01'"
    ).fetchone()[0]
    assert count == 3, f"Expected 3 rows preserved, got {count}"


def test_deduped_timestamps_are_distinct():
    """After v24, all occurred_at values for the same (track_id, project_id) must be unique."""
    conn = _base_db_v22()
    _seed_track(conn)

    shared_ts = "2026-05-29T12:00:00.123Z"
    for from_p, to_p in [("queued", "active"), ("active", "parked"), ("parked", "done")]:
        conn.execute(
            "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES ('track-01', ?, ?, 'operator', ?)",
            (from_p, to_p, shared_ts),
        )
    conn.commit()

    migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    timestamps = [
        r[0] for r in conn.execute(
            "SELECT occurred_at FROM track_phase_history WHERE track_id = 'track-01' ORDER BY id"
        ).fetchall()
    ]
    assert len(timestamps) == len(set(timestamps)), (
        f"Timestamps not distinct after dedup: {timestamps}"
    )


def test_dedup_respects_id_order():
    """The first row (lowest id) keeps the original timestamp; later rows get offsets."""
    conn = _base_db_v22()
    _seed_track(conn)

    shared_ts = "2026-05-29T12:00:00.123Z"
    for from_p, to_p in [("queued", "active"), ("active", "parked")]:
        conn.execute(
            "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES ('track-01', ?, ?, 'operator', ?)",
            (from_p, to_p, shared_ts),
        )
    conn.commit()

    migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    rows = conn.execute(
        "SELECT id, occurred_at FROM track_phase_history WHERE track_id = 'track-01' ORDER BY id"
    ).fetchall()
    # First row: original timestamp unchanged
    assert rows[0][1] == shared_ts, (
        f"First row should keep original ts '{shared_ts}', got '{rows[0][1]}'"
    )
    # Second row: has an appended offset — must differ from original
    assert rows[1][1] != shared_ts, (
        f"Second row should have offset applied, got '{rows[1][1]}'"
    )
    # Chronological order is preserved by stable id ordering, not string sort.
    # The key invariant is that the offset suffix makes them distinct strings.
    assert rows[1][1] != rows[0][1], "Rows must have distinct occurred_at after dedup"


def test_unique_constraint_not_violated_after_migration():
    """Post-migration UNIQUE constraint must not be violated on query."""
    conn = _base_db_v22()
    _seed_track(conn)

    shared_ts = "2026-05-29T12:00:00.000Z"
    for i in range(4):
        conn.execute(
            "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES ('track-01', 'queued', 'active', 'operator', ?)",
            (shared_ts,),
        )
    conn.commit()

    # Must not raise IntegrityError
    migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-01'"
    ).fetchone()[0]
    assert count == 4


def test_different_track_ids_same_timestamp_not_deduped():
    """Rows in different track_ids sharing the same timestamp must NOT be offset.

    They are in separate PARTITION BY track_id groups — dedup only applies within
    the same (track_id, occurred_at) partition.
    """
    conn = _base_db_v22()
    _seed_track(conn, "track-A")
    _seed_track(conn, "track-B")

    shared_ts = "2026-05-29T12:00:00.000Z"
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
        " VALUES ('track-A', 'queued', 'active', 'operator', ?)",
        (shared_ts,),
    )
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor, occurred_at)"
        " VALUES ('track-B', 'queued', 'active', 'operator', ?)",
        (shared_ts,),
    )
    conn.commit()

    migrate_future_system.apply_migration_v24(conn, _PROJECT_ROOT)
    conn.commit()

    ts_a = conn.execute(
        "SELECT occurred_at FROM track_phase_history WHERE track_id = 'track-A'"
    ).fetchone()[0]
    ts_b = conn.execute(
        "SELECT occurred_at FROM track_phase_history WHERE track_id = 'track-B'"
    ).fetchone()[0]

    # Both rows are in different partitions — neither should be modified
    assert ts_a == shared_ts, f"track-A timestamp should be unchanged, got '{ts_a}'"
    assert ts_b == shared_ts, f"track-B timestamp should be unchanged, got '{ts_b}'"
