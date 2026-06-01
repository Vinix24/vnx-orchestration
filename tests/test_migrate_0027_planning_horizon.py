"""tests/test_migrate_0027_planning_horizon.py — migration 0027 up/down validation.

Verifies:
- 0027 applies cleanly on a v24-equivalent DB (tracks composite key + dispatches)
- Preflight adds output_ref + output_kind to dispatches when absent (self-contained
  on fresh DBs that never ran the structural-doctor)
- deliverables VIEW created and queryable on a fresh DB (no structural-doctor pass)
- tracks.horizon column added with the now|next|later CHECK
- deliverables VIEW rolls dispatches up by output_ref
- derived_status computed correctly across mixed dispatch states
- migration is idempotent (second apply is a no-op, version unchanged)
- the _down migration removes horizon + the view and preserves track data
- down migration succeeds when track_dependencies rows reference tracks (FK-safety)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
import migrate_future_system  # noqa: F401 — registers preflight hooks for v27


def _base_v26_db(tmp_path: Path) -> sqlite3.Connection:
    """Build a DB at the live-equivalent state: 0022 + 0024 applied, plus the
    output_ref/output_kind columns the structural-doctor adds to dispatches.
    """
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
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
    conn.commit()
    # Apply 0022 then 0024 (track layer + tenant scoping).
    schema_migration.apply_script_if_below(
        conn, 22, (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 24, (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    # Mirror the structural-doctor additions present in the live v26 DB.
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    return conn


def _apply_0027(conn: sqlite3.Connection) -> bool:
    sql = (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    applied = schema_migration.apply_script_if_below(conn, 27, sql)
    conn.commit()
    return applied


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _fresh_v24_db(tmp_path: Path) -> sqlite3.Connection:
    """Build a DB at v24 WITHOUT structural-doctor output_ref/output_kind additions.

    Simulates a fresh DB that only went through 0022+0024, never ran the
    structural-doctor. The 0027 preflight must add those columns itself.
    """
    db_path = tmp_path / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
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
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 22, (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 24, (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    return conn


def _apply_0027_down(conn: sqlite3.Connection) -> None:
    sql = (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view_down.sql").read_text(encoding="utf-8")
    for stmt in schema_migration._split_sql_statements(sql):
        conn.execute(stmt)
    conn.commit()


def test_deliverables_view_queryable_on_fresh_db(tmp_path):
    """BLOCKER-1: deliverables VIEW must be queryable on a fresh DB where
    output_ref/output_kind do NOT exist before 0027 runs. The preflight
    in migrate_future_system.py adds them idempotently.
    """
    conn = _fresh_v24_db(tmp_path)
    assert "output_ref" not in _cols(conn, "dispatches")
    assert "output_kind" not in _cols(conn, "dispatches")

    applied = _apply_0027(conn)
    assert applied is True

    # Columns added by preflight before the view was created.
    assert "output_ref" in _cols(conn, "dispatches")
    assert "output_kind" in _cols(conn, "dispatches")

    # Insert a dispatch and verify the view is queryable (no column-not-found).
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, output_kind) "
        "VALUES ('d1', 'vnx-dev', 'completed', 'feat-x', 'pr:#100', 'pr')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT deliverable_ref, output_kind, derived_status FROM deliverables WHERE project_id='vnx-dev'"
    ).fetchone()
    assert row[0] == "pr:#100"
    assert row[1] == "pr"
    assert row[2] == "done"


def test_0027_down_succeeds_with_track_dependencies(tmp_path):
    """BLOCKER-2: down migration must succeed when track_dependencies has rows
    referencing tracks via FK. PRAGMA foreign_keys=OFF guards the rebuild.
    """
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)

    # Seed two tracks and a dependency edge (FK to tracks).
    conn.executemany(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, horizon) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("dep-from", "vnx-dev", "From", "goal", "active", "now"),
            ("dep-to", "vnx-dev", "To", "goal", "active", "now"),
        ],
    )
    conn.commit()
    conn.execute(
        "INSERT INTO track_dependencies "
        "(from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source) "
        "VALUES ('dep-from', 'vnx-dev', 'dep-to', 'vnx-dev', 'hard', 'manual')"
    )
    conn.commit()

    # Verify FK is active before the down migration.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # Apply down — must NOT raise an FK constraint error. The PRAGMA
    # foreign_keys=OFF guard in the down SQL prevents the DROP TABLE from
    # failing when child FKs are retargeted by the ALTER TABLE RENAME.
    _apply_0027_down(conn)
    assert schema_migration.get_user_version(conn) == 26

    # Tracks data preserved (horizon column gone).
    assert "horizon" not in _cols(conn, "tracks")
    row = conn.execute(
        "SELECT track_id, title FROM tracks WHERE track_id='dep-from' AND project_id='vnx-dev'"
    ).fetchone()
    assert row == ("dep-from", "From")

    # track_dependencies rows survived the rebuild.
    deps = conn.execute("SELECT * FROM track_dependencies").fetchone()
    assert deps is not None


def test_0027_applies_and_bumps_version(tmp_path):
    conn = _base_v26_db(tmp_path)
    assert schema_migration.get_user_version(conn) == 26
    assert _apply_0027(conn) is True
    assert schema_migration.get_user_version(conn) == 27


def test_horizon_column_added(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    assert "horizon" in _cols(conn, "tracks")


def test_horizon_check_rejects_invalid(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, horizon) "
        "VALUES ('t-ok', 'vnx-dev', 'T', 'g', 'now')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, horizon) "
            "VALUES ('t-bad', 'vnx-dev', 'T', 'g', 'someday')"
        )


def test_horizon_nullable(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('t-null', 'vnx-dev', 'T', 'g')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT horizon FROM tracks WHERE track_id='t-null'"
    ).fetchone()
    assert row[0] is None


def test_deliverables_view_exists(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert "deliverables" in views


def test_deliverables_view_groups_by_output_ref(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    # Two dispatches share one output_ref (a "deliverable"); one is separate.
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, output_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("d1", "vnx-dev", "completed", "feat-x", "pr:#100", "pr"),
            ("d2", "vnx-dev", "active", "feat-x", "pr:#100", "pr"),
            ("d3", "vnx-dev", "completed", "feat-y", "post:q3-1", "post"),
            ("d4", "vnx-dev", "queued", "feat-z", None, None),  # excluded (null output_ref)
        ],
    )
    conn.commit()
    rows: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT deliverable_ref, output_kind, track, dispatch_count, derived_status "
        "FROM deliverables WHERE project_id='vnx-dev'"
    ):
        rows[r[0]] = {"output_kind": r[1], "track": r[2], "count": r[3], "derived": r[4]}

    assert set(rows.keys()) == {"pr:#100", "post:q3-1"}
    assert rows["pr:#100"]["count"] == 2
    assert rows["pr:#100"]["derived"] == "in_progress"  # one active
    assert rows["pr:#100"]["output_kind"] == "pr"
    assert rows["post:q3-1"]["count"] == 1
    assert rows["post:q3-1"]["derived"] == "done"  # all completed


def test_deliverables_view_failed_status(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    conn.executemany(
        "INSERT INTO dispatches (dispatch_id, project_id, state, output_ref, output_kind) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("f1", "vnx-dev", "failed", "pr:#200", "pr"),
            ("f2", "vnx-dev", "dead_letter", "pr:#200", "pr"),
        ],
    )
    conn.commit()
    row = conn.execute(
        "SELECT derived_status FROM deliverables WHERE deliverable_ref='pr:#200'"
    ).fetchone()
    assert row[0] == "failed"


def test_0027_idempotent(tmp_path):
    conn = _base_v26_db(tmp_path)
    assert _apply_0027(conn) is True
    # Second apply is skipped (version already >= 27).
    assert _apply_0027(conn) is False
    assert schema_migration.get_user_version(conn) == 27


def test_0027_down_removes_horizon_and_view(tmp_path):
    conn = _base_v26_db(tmp_path)
    _apply_0027(conn)
    # Seed a track to confirm data survives the down rebuild.
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, phase, horizon) "
        "VALUES ('keep-me', 'vnx-dev', 'Keep', 'goal', 'active', 'now')"
    )
    conn.commit()

    down_sql = (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view_down.sql").read_text(encoding="utf-8")
    for stmt in schema_migration._split_sql_statements(down_sql):
        conn.execute(stmt)
    conn.commit()

    assert schema_migration.get_user_version(conn) == 26
    assert "horizon" not in _cols(conn, "tracks")
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert "deliverables" not in views
    # Data preserved.
    row = conn.execute(
        "SELECT title, phase FROM tracks WHERE track_id='keep-me' AND project_id='vnx-dev'"
    ).fetchone()
    assert row == ("Keep", "active")
