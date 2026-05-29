"""tests/test_migrate_v24_multi_project_preservation.py — multi-project project_id derivation.

Verifies that 0024 migration derives project_id from parent tracks via JOIN,
not from a hardcoded 'vnx-dev' default.

Key scenario: two tracks in different projects (vnx-dev / seocrawler-v2).
After migration, each child row must carry the project_id of its parent track.
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


def _apply_v24(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 24, sql)
    conn.commit()


def _seed_multi_project(conn: sqlite3.Connection) -> None:
    """Seed v22 data with tracks in two different projects.

    track-A belongs to 'vnx-dev', track-B belongs to 'seocrawler-v2'.
    v22 tracks table has project_id column but no composite PK yet.
    """
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state, project_id) VALUES ('track-A', 'Alpha', 'Goal A', 'vnx-dev')"
    )
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state, project_id) VALUES ('track-B', 'Beta', 'Goal B', 'seocrawler-v2')"
    )
    conn.commit()

    # Phase history: one row per track
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor) VALUES ('track-A', 'queued', 'active', 'operator')"
    )
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor) VALUES ('track-B', 'queued', 'active', 'operator')"
    )
    conn.commit()

    # Cross-project dependency: track-A (vnx-dev) → track-B (seocrawler-v2)
    conn.execute(
        "INSERT INTO track_dependencies (from_track_id, to_track_id, kind, derivation_source) VALUES ('track-A', 'track-B', 'hard', 'manual')"
    )
    conn.commit()

    # Open items: one per track
    conn.execute(
        "INSERT INTO track_open_items (track_id, oi_id, link_type, link_source) VALUES ('track-A', 'OI-001', 'blocks', 'manual')"
    )
    conn.execute(
        "INSERT INTO track_open_items (track_id, oi_id, link_type, link_source) VALUES ('track-B', 'OI-002', 'warns', 'manual')"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_phase_history_inherits_correct_project_id():
    conn = _base_db()
    _seed_multi_project(conn)
    _apply_v24(conn)

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT track_id, project_id FROM track_phase_history ORDER BY track_id"
        ).fetchall()
    }
    assert rows["track-A"] == "vnx-dev", (
        f"track-A history must have project_id='vnx-dev', got {rows['track-A']!r}"
    )
    assert rows["track-B"] == "seocrawler-v2", (
        f"track-B history must have project_id='seocrawler-v2', not 'vnx-dev' default; got {rows['track-B']!r}"
    )


def test_cross_project_dependency_inherits_both_project_ids():
    conn = _base_db()
    _seed_multi_project(conn)
    _apply_v24(conn)

    conn.row_factory = sqlite3.Row
    dep = conn.execute("SELECT * FROM track_dependencies").fetchone()
    assert dep is not None
    dep = dict(dep)
    assert dep["from_project_id"] == "vnx-dev", (
        f"from_project_id must be 'vnx-dev', got {dep['from_project_id']!r}"
    )
    assert dep["to_project_id"] == "seocrawler-v2", (
        f"to_project_id must be 'seocrawler-v2', not 'vnx-dev' default; got {dep['to_project_id']!r}"
    )


def test_open_items_inherit_correct_project_id():
    conn = _base_db()
    _seed_multi_project(conn)
    _apply_v24(conn)

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT track_id, project_id FROM track_open_items ORDER BY track_id"
        ).fetchall()
    }
    assert rows["track-A"] == "vnx-dev", (
        f"track-A OI must have project_id='vnx-dev', got {rows['track-A']!r}"
    )
    assert rows["track-B"] == "seocrawler-v2", (
        f"track-B OI must have project_id='seocrawler-v2', not 'vnx-dev' default; got {rows['track-B']!r}"
    )


def test_all_rows_preserved_count():
    conn = _base_db()
    _seed_multi_project(conn)
    _apply_v24(conn)

    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM track_dependencies").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM track_open_items").fetchone()[0] == 2


def test_no_vnx_dev_bleed_into_seocrawler_tracks():
    """Regression: child rows for seocrawler-v2 tracks must NOT get project_id='vnx-dev'."""
    conn = _base_db()
    _seed_multi_project(conn)
    _apply_v24(conn)

    seo_history = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-B' AND project_id = 'vnx-dev'"
    ).fetchone()[0]
    assert seo_history == 0, (
        "track-B (seocrawler-v2) history must NOT have project_id='vnx-dev' after JOIN derivation"
    )

    seo_oi = conn.execute(
        "SELECT COUNT(*) FROM track_open_items WHERE track_id = 'track-B' AND project_id = 'vnx-dev'"
    ).fetchone()[0]
    assert seo_oi == 0, (
        "track-B (seocrawler-v2) OI must NOT have project_id='vnx-dev' after JOIN derivation"
    )
