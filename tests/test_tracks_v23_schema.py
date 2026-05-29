"""tests/test_tracks_v23_schema.py — migration 0024 schema validation.

Verifies:
- Migration 0024 runs cleanly on a v22 DB (fresh + seeded)
- All 4 tables have composite PKs over (track_id, project_id)
- sqlite_sequence preserved for track_phase_history (AUTOINCREMENT)
- FK enforced: child rows reject non-existent (track_id, project_id) pairs
- Composite PK prevents duplicate (track-01, vnx-dev); allows (track-01, seocrawler-v2)
- ux_tracks_next_up_per_project UNIQUE index present
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a v21-equivalent DB (dispatches + coordination_events) in memory."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
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
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT, event_type TEXT, entity_type TEXT,
            entity_id   TEXT, from_state TEXT, to_state TEXT,
            actor       TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()
    return conn


def _apply_v22(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()


def _apply_v24(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 24, sql)
    conn.commit()


def _v22_then_v24(tmp_path: Path) -> sqlite3.Connection:
    conn = _base_db(tmp_path)
    _apply_v22(conn)
    _apply_v24(conn)
    return conn


# ---------------------------------------------------------------------------
# Tests: schema version
# ---------------------------------------------------------------------------

def test_user_version_is_24(tmp_path):
    conn = _v22_then_v24(tmp_path)
    assert schema_migration.get_user_version(conn) == 24


def test_migration_idempotent(tmp_path):
    conn = _v22_then_v24(tmp_path)
    # Re-applying 0024 must be a no-op
    result = schema_migration.apply_script_if_below(
        conn, 24, (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text()
    )
    assert result is False
    assert schema_migration.get_user_version(conn) == 24


# ---------------------------------------------------------------------------
# Tests: tracks composite PK
# ---------------------------------------------------------------------------

def test_tracks_composite_pk_prevents_duplicate_same_project(tmp_path):
    conn = _v22_then_v24(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        ("track-01", "vnx-dev", "Title A", "Goal A"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
            ("track-01", "vnx-dev", "Title B", "Goal B"),
        )


def test_tracks_composite_pk_allows_same_id_different_project(tmp_path):
    conn = _v22_then_v24(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        ("track-01", "vnx-dev", "Title A", "Goal A"),
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        ("track-01", "seocrawler-v2", "Title B", "Goal B"),
    )
    conn.commit()
    rows = conn.execute("SELECT project_id FROM tracks WHERE track_id = 'track-01'").fetchall()
    assert {r[0] for r in rows} == {"vnx-dev", "seocrawler-v2"}


def test_tracks_columns_present(tmp_path):
    conn = _v22_then_v24(tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info('tracks')")}
    assert "project_id" in cols
    assert "track_id" in cols
    assert "phase" in cols
    assert "next_up" in cols


def test_tracks_v23_index_present(tmp_path):
    conn = _v22_then_v24(tmp_path)
    indexes = [row[1] for row in conn.execute("PRAGMA index_list('tracks')")]
    assert "ux_tracks_next_up_per_project" in indexes


# ---------------------------------------------------------------------------
# Tests: track_phase_history composite FK
# ---------------------------------------------------------------------------

def test_track_phase_history_fk_enforced(tmp_path):
    conn = _v22_then_v24(tmp_path)
    # Insert without a matching track → FK violation
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO track_phase_history
                (track_id, project_id, from_phase, to_phase, actor)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("ghost-track", "vnx-dev", "queued", "active", "operator"),
        )


def test_track_phase_history_fk_satisfied(tmp_path):
    conn = _v22_then_v24(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        ("track-01", "vnx-dev", "Title", "Goal"),
    )
    conn.commit()
    conn.execute(
        """
        INSERT INTO track_phase_history
            (track_id, project_id, from_phase, to_phase, actor)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("track-01", "vnx-dev", "queued", "active", "operator"),
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Tests: sqlite_sequence preserved for track_phase_history
# ---------------------------------------------------------------------------

def test_track_phase_history_sqlite_sequence_preserved(tmp_path):
    conn = _base_db(tmp_path)
    _apply_v22(conn)

    # Seed track and phase history in v22
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES (?, ?, ?)",
        ("track-01", "Title", "Goal"),
    )
    conn.commit()
    conn.execute(
        """
        INSERT INTO track_phase_history
            (track_id, from_phase, to_phase, actor)
        VALUES (?, ?, ?, ?)
        """,
        ("track-01", "queued", "active", "operator"),
    )
    conn.commit()

    max_id_before = conn.execute("SELECT MAX(id) FROM track_phase_history").fetchone()[0]
    assert max_id_before == 1

    # Apply v24 migration
    _apply_v24(conn)

    # sqlite_sequence for track_phase_history must reflect the max id
    seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'track_phase_history'"
    ).fetchone()
    assert seq is not None
    assert seq[0] >= max_id_before

    # Next insert must not reuse id=1
    conn.execute(
        "INSERT INTO track_phase_history (track_id, project_id, from_phase, to_phase, actor) VALUES (?, ?, ?, ?, ?)",
        ("track-01", "vnx-dev", "active", "parked", "operator"),
    )
    conn.commit()
    new_id = conn.execute("SELECT MAX(id) FROM track_phase_history").fetchone()[0]
    assert new_id > max_id_before


# ---------------------------------------------------------------------------
# Tests: track_dependencies composite PK
# ---------------------------------------------------------------------------

def test_track_dependencies_composite_pk(tmp_path):
    conn = _v22_then_v24(tmp_path)
    for pid in ("vnx-dev", "seocrawler-v2"):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
            ("track-01", pid, "T1", "G1"),
        )
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
            ("track-02", pid, "T2", "G2"),
        )
    conn.commit()

    conn.execute(
        """
        INSERT INTO track_dependencies
            (from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("track-01", "vnx-dev", "track-02", "vnx-dev", "hard", "manual"),
    )
    conn.execute(
        """
        INSERT INTO track_dependencies
            (from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("track-01", "seocrawler-v2", "track-02", "seocrawler-v2", "soft", "manual"),
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM track_dependencies").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Tests: track_open_items composite PK
# ---------------------------------------------------------------------------

def test_track_open_items_composite_pk(tmp_path):
    conn = _v22_then_v24(tmp_path)
    for pid in ("vnx-dev", "seocrawler-v2"):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
            ("track-01", pid, "T", "G"),
        )
    conn.commit()

    # Same oi_id + link_type, different project_id → allowed
    conn.execute(
        "INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source) VALUES (?, ?, ?, ?, ?)",
        ("track-01", "vnx-dev", "OI-001", "blocks", "manual"),
    )
    conn.execute(
        "INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source) VALUES (?, ?, ?, ?, ?)",
        ("track-01", "seocrawler-v2", "OI-001", "blocks", "manual"),
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM track_open_items").fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# Tests: ADR-007 composite UNIQUE on track_phase_history (Priority 2)
# ---------------------------------------------------------------------------

def test_track_phase_history_composite_unique_over_project_id(tmp_path):
    """ADR-007: track_phase_history must have UNIQUE index involving project_id."""
    conn = _v22_then_v24(tmp_path)
    unique_indexes = [
        idx[1] for idx in conn.execute("PRAGMA index_list('track_phase_history')")
        if idx[2] == 1  # unique flag
    ]
    assert any(
        "project_id" in [col[2] for col in conn.execute(f"PRAGMA index_info('{name}')")]
        for name in unique_indexes
    ), "ADR-007: track_phase_history must have at least one UNIQUE index over project_id"
    # Also verify the UNIQUE index has length >= 2 (composite, not single-column)
    for name in unique_indexes:
        idx_cols = [col[2] for col in conn.execute(f"PRAGMA index_info('{name}')")]
        if "project_id" in idx_cols:
            assert len(idx_cols) >= 2, (
                f"ADR-007: UNIQUE index {name!r} includes project_id but length < 2"
            )


def test_track_phase_history_unique_constraint_rejects_duplicate_occurred_at(tmp_path):
    """Behavioral: inserting two rows with same (track_id, project_id, occurred_at) raises."""
    conn = _v22_then_v24(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        ("track-01", "vnx-dev", "T", "G"),
    )
    ts = "2026-01-01T00:00:00.000Z"
    conn.execute(
        "INSERT INTO track_phase_history (track_id, project_id, from_phase, to_phase, actor, occurred_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("track-01", "vnx-dev", "queued", "active", "operator", ts),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO track_phase_history (track_id, project_id, from_phase, to_phase, actor, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("track-01", "vnx-dev", "active", "parked", "operator", ts),
        )


# ---------------------------------------------------------------------------
# Tests: sqlite_sequence high-water-mark preservation (Priority 3)
# ---------------------------------------------------------------------------

def test_sqlite_sequence_preserves_high_water_after_delete(tmp_path):
    """Regression: seq must survive to pre-v24 high-water even when rows were deleted."""
    conn = _base_db(tmp_path)
    _apply_v22(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES (?, ?, ?)",
        ("t1", "T", "G"),
    )
    conn.execute(
        "INSERT INTO track_phase_history (id, track_id, from_phase, to_phase, actor)"
        " VALUES (1, 't1', 'queued', 'active', 'operator')"
    )
    conn.execute(
        "INSERT INTO track_phase_history (id, track_id, from_phase, to_phase, actor)"
        " VALUES (100, 't1', 'active', 'parked', 'operator')"
    )
    conn.execute("DELETE FROM track_phase_history WHERE id = 100")
    conn.commit()

    seq_before = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='track_phase_history'"
    ).fetchone()[0]
    assert seq_before == 100  # high-water before migration

    _apply_v24(conn)

    seq_after = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='track_phase_history'"
    ).fetchone()[0]
    assert seq_after >= seq_before, (
        f"sqlite_sequence regressed: was {seq_before}, now {seq_after}. "
        "Fix1 lesson: preserve from pre_v24 snapshot, not current MAX(id)."
    )
