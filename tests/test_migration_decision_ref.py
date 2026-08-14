"""tests/test_migration_decision_ref.py — migration 0033 validation.

Verifies:
- 0033 applies cleanly on a v29-equivalent tracks table
- tracks.decision_ref added (nullable TEXT, no DEFAULT — NULL on existing rows)
- migration is idempotent (second apply is a no-op, version unchanged)
- row count preserved before and after migration
- PRAGMA integrity_check passes after migration
- ADR-007 composite PRIMARY KEY (track_id, project_id) survives the additive change
- the apply_0033.py runner (the auto_apply path) applies end-to-end

All tests use temporary DBs only — the live .vnx-data DB is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schema_migration  # noqa: E402
from migrations.apply_0033 import apply_migration  # noqa: E402
from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _base_v29_db(tmp_path: Path) -> sqlite3.Connection:
    """Build a DB in the v29-equivalent state (0022+0024+0027+0028+0029 applied).

    Mirrors the `_base_v28_db` pattern from test_migration_track_type.py, then
    applies 0029 (track_type / next_action_owner) so the tracks table is in the
    exact pre-0033 shape.
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
    for version, filename in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8")
        )
        conn.commit()
    # Mirror structural-doctor additions present in the live v26 DB.
    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for version, filename in (
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8")
        )
        conn.commit()
    return conn


def _apply_0033(conn: sqlite3.Connection) -> bool:
    sql = (_MIGRATIONS / "0033_track_decision_ref.sql").read_text(encoding="utf-8")
    applied = schema_migration.apply_script_if_below(conn, 33, sql)
    conn.commit()
    return applied


# ---------------------------------------------------------------------------
# Tests: basic application
# ---------------------------------------------------------------------------

def test_0033_applies_and_bumps_version(tmp_path):
    conn = _base_v29_db(tmp_path)
    assert schema_migration.get_user_version(conn) == 29
    assert _apply_0033(conn) is True
    assert schema_migration.get_user_version(conn) == 33


def test_decision_ref_column_added(tmp_path):
    conn = _base_v29_db(tmp_path)
    _apply_0033(conn)
    assert "decision_ref" in _cols(conn, "tracks")


def test_decision_ref_is_nullable_and_defaults_to_null(tmp_path):
    """Existing rows get decision_ref=NULL from the additive ALTER — no data impact."""
    conn = _base_v29_db(tmp_path)
    conn.executemany(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        [
            ("t1", "vnx-dev", "Alpha", "goal-a"),
            ("t2", "proj-x", "Beta", "goal-b"),
        ],
    )
    conn.commit()
    _apply_0033(conn)
    null_count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE decision_ref IS NULL"
    ).fetchone()[0]
    assert null_count == 2


def test_rowcount_preserved(tmp_path):
    conn = _base_v29_db(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('stable', 'vnx-dev', 'S', 'g')"
    )
    conn.commit()
    count_before = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    _apply_0033(conn)
    count_after = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_after == count_before == 1


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------

def test_0033_idempotent(tmp_path):
    conn = _base_v29_db(tmp_path)
    assert _apply_0033(conn) is True
    # Second apply returns False (skipped — version already >= 33).
    assert _apply_0033(conn) is False
    assert schema_migration.get_user_version(conn) == 33


def test_0033_idempotent_rowcount_stable(tmp_path):
    conn = _base_v29_db(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('stable', 'vnx-dev', 'S', 'g')"
    )
    conn.commit()
    _apply_0033(conn)
    count_after_first = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    _apply_0033(conn)
    count_after_second = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_after_first == count_after_second == 1


# ---------------------------------------------------------------------------
# Tests: integrity and ADR-007 composite key
# ---------------------------------------------------------------------------

def test_integrity_check_passes(tmp_path):
    conn = _base_v29_db(tmp_path)
    _apply_0033(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, decision_ref) "
        "VALUES ('ic-1', 'vnx-dev', 'IC', 'g', '{}')"
    )
    conn.commit()
    result = conn.execute("PRAGMA integrity_check").fetchall()
    assert result == [("ok",)]


def test_composite_pk_preserved(tmp_path):
    """ADR-007: PRIMARY KEY (track_id, project_id) must survive the additive migration."""
    conn = _base_v29_db(tmp_path)
    _apply_0033(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('dup', 'vnx-dev', 'A', 'g')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state) "
            "VALUES ('dup', 'vnx-dev', 'B', 'g')"
        )


def test_composite_pk_allows_same_track_id_different_project(tmp_path):
    """Same track_id in different projects is allowed (multi-tenant scope)."""
    conn = _base_v29_db(tmp_path)
    _apply_0033(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('shared-id', 'proj-a', 'A', 'g')"
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('shared-id', 'proj-b', 'B', 'g')"
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE track_id='shared-id'"
    ).fetchone()[0]
    assert count == 2


def test_decision_ref_stores_arbitrary_json_text(tmp_path):
    """decision_ref is a TEXT payload — a JSON string round-trips verbatim."""
    conn = _base_v29_db(tmp_path)
    _apply_0033(conn)
    payload = '{"decision": "PASS", "reports": ["unified_reports/plan-gate-x-opus-abcd1234.md"]}'
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, decision_ref) "
        "VALUES ('dr', 'vnx-dev', 'DR', 'g', ?)",
        (payload,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT decision_ref FROM tracks WHERE track_id='dr' AND project_id='vnx-dev'"
    ).fetchone()
    assert row[0] == payload


# ---------------------------------------------------------------------------
# Tests: the apply_0033.py runner (auto_apply's discovery path)
# ---------------------------------------------------------------------------

def test_apply_0033_runner_end_to_end(tmp_path):
    """The paired runner (what auto_apply invokes) applies on a fresh connection."""
    db_path = tmp_path / "runtime_coordination.db"
    conn = _base_v29_db(tmp_path)
    assert schema_migration.get_user_version(conn) == 29
    conn.close()

    assert apply_migration(db_path, _MIGRATIONS / "0033_track_decision_ref.sql") is True

    conn2 = sqlite3.connect(str(db_path))
    assert schema_migration.get_user_version(conn2) == 33
    assert "decision_ref" in _cols(conn2, "tracks")
    conn2.close()


def test_apply_0033_runner_idempotent(tmp_path):
    db_path = tmp_path / "runtime_coordination.db"
    conn = _base_v29_db(tmp_path)
    conn.close()

    assert apply_migration(db_path, _MIGRATIONS / "0033_track_decision_ref.sql") is True
    # Already at user_version >= 33 → skipped, not an error.
    assert apply_migration(db_path, _MIGRATIONS / "0033_track_decision_ref.sql") is False
