"""tests/test_migrate_v23_preserve.py — data preservation through 0022 → 0023.

Verifies:
- All track rows from v22 are preserved after 0023 migration
- project_id stamped as 'vnx-dev' for all migrated rows
- sqlite_sequence preserved (no id reuse after migration)
- Phase history, dependencies, open-item links preserved
- Idempotent: re-running 0023 on v23 DB is a no-op
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration


def _base_db() -> sqlite3.Connection:
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


def _seed_v22_data(conn: sqlite3.Connection) -> None:
    """Insert 6 tracks + phase history + dependencies + open items."""
    tracks_data = [
        ("track-01", "T1 — Central infra"),
        ("track-02", "T2 — Multi-tenant tracks"),
        ("track-03", "T3 — Cost tracking"),
        ("track-04", "T4 — Provider failover"),
        ("track-05", "T5 — Roadmap autopilot"),
        ("track-06", "T6 — Kanban UI"),
    ]
    for i, (tid, title) in enumerate(tracks_data, start=1):
        conn.execute(
            """
            INSERT INTO tracks (track_id, title, goal_state, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (tid, title, f"Goal for {tid}", i),
        )
    conn.commit()

    # Phase history: track-01 activated
    conn.execute(
        """
        INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor)
        VALUES ('track-01', 'queued', 'active', 'operator')
        """
    )
    conn.execute(
        """
        INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor)
        VALUES ('track-01', 'active', 'parked', 'operator')
        """
    )
    conn.commit()

    # Dependency: track-02 depends on track-01
    conn.execute(
        """
        INSERT INTO track_dependencies (from_track_id, to_track_id, kind, derivation_source)
        VALUES ('track-02', 'track-01', 'hard', 'manual')
        """
    )
    conn.commit()

    # Open item linked to track-03
    conn.execute(
        """
        INSERT INTO track_open_items (track_id, oi_id, link_type, link_source)
        VALUES ('track-03', 'OI-007', 'blocks', 'manual')
        """
    )
    conn.commit()


def _apply_v23(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0023_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 23, sql)
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_track_rows_preserved(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    rows = conn.execute("SELECT track_id FROM tracks ORDER BY sort_order").fetchall()
    assert len(rows) == 6
    tids = [r[0] for r in rows]
    assert tids == ["track-01", "track-02", "track-03", "track-04", "track-05", "track-06"]


def test_project_id_stamped_vnx_dev(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    pids = {r[0] for r in conn.execute("SELECT DISTINCT project_id FROM tracks").fetchall()}
    assert pids == {"vnx-dev"}, f"Expected all rows stamped 'vnx-dev', got: {pids}"


def test_phase_history_preserved(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)

    max_id_before = conn.execute("SELECT MAX(id) FROM track_phase_history").fetchone()[0]
    assert max_id_before == 2

    _apply_v23(conn)

    count = conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0]
    assert count == 2

    rows = conn.execute(
        "SELECT track_id, project_id FROM track_phase_history"
    ).fetchall()
    for track_id, project_id in rows:
        assert track_id == "track-01"
        assert project_id == "vnx-dev"


def test_sqlite_sequence_preserved_for_phase_history(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'track_phase_history'"
    ).fetchone()
    assert seq is not None
    assert seq[0] >= 2, f"sqlite_sequence seq must be >= 2, got {seq[0]}"

    # Next insert must get id > 2
    conn.execute(
        """
        INSERT INTO track_phase_history
            (track_id, project_id, from_phase, to_phase, actor)
        VALUES ('track-02', 'vnx-dev', 'queued', 'active', 'operator')
        """
    )
    conn.commit()
    new_max = conn.execute("SELECT MAX(id) FROM track_phase_history").fetchone()[0]
    assert new_max > 2, f"New id must be > 2 (no id reuse), got {new_max}"


def test_dependencies_preserved(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    conn.row_factory = sqlite3.Row
    deps = conn.execute("SELECT * FROM track_dependencies").fetchall()
    assert len(deps) == 1
    dep = dict(deps[0])
    assert dep["from_track_id"] == "track-02"
    assert dep["to_track_id"] == "track-01"
    assert dep["from_project_id"] == "vnx-dev"
    assert dep["to_project_id"] == "vnx-dev"


def test_open_items_preserved(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    ois = conn.execute("SELECT track_id, project_id, oi_id FROM track_open_items").fetchall()
    assert len(ois) == 1
    assert ois[0][0] == "track-03"
    assert ois[0][1] == "vnx-dev"
    assert ois[0][2] == "OI-007"


def test_user_version_is_23(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)
    assert schema_migration.get_user_version(conn) == 23


def test_idempotent_reapply(tmp_path):
    conn = _base_db()
    _seed_v22_data(conn)
    _apply_v23(conn)

    # Re-apply must be no-op
    sql = (_MIGRATIONS / "0023_tracks_tenant_scoping.sql").read_text()
    result = schema_migration.apply_script_if_below(conn, 23, sql)
    assert result is False

    # Row count unchanged
    count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count == 6
