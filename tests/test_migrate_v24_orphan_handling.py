"""tests/test_migrate_v24_orphan_handling.py — orphan row handling in 0024 migration.

Verifies:
- Orphan child rows (track_id not in tracks) are skipped during v24 migration
- Python migration helper emits UserWarning for each orphan batch found
- Non-orphan rows are preserved correctly alongside orphan rows
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
import migrate_future_system


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


def _insert_orphan_history(conn: sqlite3.Connection, orphan_track_id: str) -> None:
    """Insert a phase history row for a track_id that does NOT exist in tracks.

    Must disable FK enforcement temporarily since v22 track_phase_history has
    REFERENCES tracks(track_id).
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor) VALUES (?, ?, ?, ?)",
        (orphan_track_id, "queued", "active", "operator"),
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: orphan rows are skipped by the SQL migration
# ---------------------------------------------------------------------------

def test_orphan_phase_history_row_is_skipped():
    conn = _base_db()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-01', 'T1', 'G1')"
    )
    conn.commit()
    _insert_orphan_history(conn, "track-orphan")
    _apply_v24(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-orphan'"
    ).fetchone()[0]
    assert count == 0, "Orphan phase history row must be excluded after v24 migration"


def test_valid_phase_history_preserved_alongside_orphan():
    conn = _base_db()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-01', 'T1', 'G1')"
    )
    conn.commit()
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor) VALUES ('track-01', 'queued', 'active', 'operator')"
    )
    conn.commit()
    _insert_orphan_history(conn, "track-orphan")
    _apply_v24(conn)

    valid = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-01'"
    ).fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM track_phase_history WHERE track_id = 'track-orphan'"
    ).fetchone()[0]
    assert valid == 1, "Valid phase history row must be preserved"
    assert orphan == 0, "Orphan phase history row must be excluded"


def test_orphan_dependency_row_is_skipped():
    conn = _base_db()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-01', 'T1', 'G1')"
    )
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-02', 'T2', 'G2')"
    )
    conn.commit()
    # Dependency where to_track_id is orphan
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO track_dependencies (from_track_id, to_track_id, kind, derivation_source) VALUES ('track-01', 'track-orphan', 'hard', 'manual')"
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    _apply_v24(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM track_dependencies WHERE to_track_id = 'track-orphan'"
    ).fetchone()[0]
    assert count == 0, "Dependency with orphan to_track_id must be excluded"


def test_orphan_open_item_row_is_skipped():
    conn = _base_db()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO track_open_items (track_id, oi_id, link_type, link_source) VALUES ('track-orphan', 'OI-001', 'blocks', 'manual')"
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    _apply_v24(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM track_open_items WHERE track_id = 'track-orphan'"
    ).fetchone()[0]
    assert count == 0, "Orphan open item row must be excluded after v24 migration"


# ---------------------------------------------------------------------------
# Tests: Python warning helper emits UserWarning for orphan batches
# ---------------------------------------------------------------------------

def test_warn_orphan_emits_warning_for_phase_history():
    conn = _base_db()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-01', 'T1', 'G1')"
    )
    conn.commit()
    _insert_orphan_history(conn, "track-orphan")

    with pytest.warns(UserWarning, match="orphan"):
        migrate_future_system._warn_orphan_child_rows(conn)


def test_warn_orphan_no_warning_when_no_orphans():
    conn = _base_db()
    conn.execute(
        "INSERT INTO tracks (track_id, title, goal_state) VALUES ('track-01', 'T1', 'G1')"
    )
    conn.commit()
    conn.execute(
        "INSERT INTO track_phase_history (track_id, from_phase, to_phase, actor) VALUES ('track-01', 'queued', 'active', 'operator')"
    )
    conn.commit()

    import warnings as _warnings
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        migrate_future_system._warn_orphan_child_rows(conn)
    assert len(caught) == 0, "No warnings expected when all child rows have valid parent"
