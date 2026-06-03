"""tests/test_migration_track_type.py — migration 0029 validation.

Verifies:
- 0029 applies cleanly on a v28-equivalent DB
- tracks.track_type added with CHECK IN ('coding','content','deal','relationship')
- track_type defaults to 'coding' for all pre-existing rows (no data impact)
- tracks.next_action_owner added (nullable, CHECK on valid values or NULL)
- migration is idempotent (second apply is a no-op, version unchanged)
- row count preserved before and after migration
- PRAGMA integrity_check passes after migration
- invalid track_type value rejected (CHECK constraint or DAL enforcement)
- invalid next_action_owner value rejected
- preflight rejects double-apply (column already present)

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

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
import migrate_future_system  # noqa: F401 — registers preflight hooks for v29


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _base_v28_db(tmp_path: Path) -> sqlite3.Connection:
    """Build a DB in the v28-equivalent state (0022+0024+0027+0028 applied).

    Mirrors the _base_v26_db pattern from test_migrate_0027_planning_horizon:
    - dispatches starts WITHOUT output_ref/output_kind (preflight v22 rejects extras)
    - 0022+0024 applied normally
    - output_ref/output_kind added manually (mirrors structural-doctor) at v26
    - 0027+0028 applied via apply_script_if_below
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
    # Mirror structural-doctor additions present in the live v26 DB.
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    return conn


def _apply_0029(conn: sqlite3.Connection) -> bool:
    sql = (_MIGRATIONS / "0029_track_type_discriminator.sql").read_text(encoding="utf-8")
    applied = schema_migration.apply_script_if_below(conn, 29, sql)
    conn.commit()
    return applied


# ---------------------------------------------------------------------------
# Tests: basic application
# ---------------------------------------------------------------------------

def test_0029_applies_and_bumps_version(tmp_path):
    conn = _base_v28_db(tmp_path)
    assert schema_migration.get_user_version(conn) == 28
    assert _apply_0029(conn) is True
    assert schema_migration.get_user_version(conn) == 29


def test_track_type_column_added(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    assert "track_type" in _cols(conn, "tracks")


def test_next_action_owner_column_added(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    assert "next_action_owner" in _cols(conn, "tracks")


# ---------------------------------------------------------------------------
# Tests: default value and existing row preservation
# ---------------------------------------------------------------------------

def test_track_type_defaults_to_coding_on_existing_rows(tmp_path):
    """Existing rows get track_type='coding' from the DEFAULT — no data impact."""
    conn = _base_v28_db(tmp_path)
    # Seed three rows BEFORE migration.
    conn.executemany(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) VALUES (?, ?, ?, ?)",
        [
            ("t1", "vnx-dev", "Alpha", "goal-a"),
            ("t2", "vnx-dev", "Beta", "goal-b"),
            ("t3", "proj-x", "Gamma", "goal-c"),
        ],
    )
    conn.commit()
    count_before = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_before == 3

    _apply_0029(conn)

    count_after = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_after == count_before, "Row count must be unchanged after migration"

    # All pre-existing rows have track_type='coding'.
    coding_count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE track_type = 'coding'"
    ).fetchone()[0]
    assert coding_count == 3

    # next_action_owner is NULL for all pre-existing rows (no DEFAULT).
    null_count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE next_action_owner IS NULL"
    ).fetchone()[0]
    assert null_count == 3


def test_rowcount_preserved_with_empty_tracks(tmp_path):
    """Edge case: migration on a DB with zero tracks rows."""
    conn = _base_v28_db(tmp_path)
    count_before = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    _apply_0029(conn)
    count_after = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_after == count_before == 0


# ---------------------------------------------------------------------------
# Tests: CHECK constraints
# ---------------------------------------------------------------------------

def test_track_type_accepts_valid_values(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    for i, tt in enumerate(("coding", "content", "deal", "relationship")):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, track_type) "
            "VALUES (?, 'vnx-dev', ?, 'g', ?)",
            (f"t-{i}", f"T{i}", tt),
        )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE track_type IN ('coding','content','deal','relationship')"
    ).fetchone()[0]
    assert count == 4


def test_track_type_rejects_invalid_value(tmp_path):
    """CHECK constraint must reject any value outside the allowed set."""
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, track_type) "
            "VALUES ('bad', 'vnx-dev', 'Bad', 'g', 'infrastructure')"
        )


def test_next_action_owner_accepts_valid_values(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    for i, owner in enumerate(("me", "client", "waiting")):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, next_action_owner) "
            "VALUES (?, 'vnx-dev', ?, 'g', ?)",
            (f"o-{i}", f"O{i}", owner),
        )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE next_action_owner IN ('me','client','waiting')"
    ).fetchone()[0]
    assert count == 3


def test_next_action_owner_accepts_null(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('null-owner', 'vnx-dev', 'T', 'g')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT next_action_owner FROM tracks WHERE track_id='null-owner'"
    ).fetchone()
    assert row[0] is None


def test_next_action_owner_rejects_invalid_value(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracks (track_id, project_id, title, goal_state, next_action_owner) "
            "VALUES ('bad-owner', 'vnx-dev', 'T', 'g', 'unknown')"
        )


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------

def test_0029_idempotent(tmp_path):
    conn = _base_v28_db(tmp_path)
    assert _apply_0029(conn) is True
    # Second apply returns False (skipped — version already >= 29).
    assert _apply_0029(conn) is False
    assert schema_migration.get_user_version(conn) == 29


def test_0029_idempotent_rowcount_stable(tmp_path):
    """Row count must not change on a second (no-op) apply."""
    conn = _base_v28_db(tmp_path)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state) "
        "VALUES ('stable', 'vnx-dev', 'S', 'g')"
    )
    conn.commit()
    _apply_0029(conn)
    count_after_first = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    _apply_0029(conn)
    count_after_second = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert count_after_first == count_after_second == 1


# ---------------------------------------------------------------------------
# Tests: integrity and ADR-007 composite key
# ---------------------------------------------------------------------------

def test_integrity_check_passes(tmp_path):
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, goal_state, track_type) "
        "VALUES ('ic-1', 'vnx-dev', 'IC', 'g', 'deal')"
    )
    conn.commit()
    result = conn.execute("PRAGMA integrity_check").fetchall()
    assert result == [("ok",)]


def test_composite_pk_preserved(tmp_path):
    """ADR-007: PRIMARY KEY (track_id, project_id) must survive the additive migration."""
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
    # Duplicate (track_id, project_id) must be rejected.
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
    conn = _base_v28_db(tmp_path)
    _apply_0029(conn)
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


# ---------------------------------------------------------------------------
# Tests: preflight guard
# ---------------------------------------------------------------------------

def test_preflight_rejects_double_apply(tmp_path):
    """Preflight must raise if track_type already exists but version < 29 (safety belt)."""
    conn = _base_v28_db(tmp_path)
    # Manually add the column WITHOUT bumping user_version — simulates a
    # partial apply or operator error.
    conn.execute("ALTER TABLE tracks ADD COLUMN track_type TEXT DEFAULT 'coding'")
    conn.commit()
    # The preflight hook must now reject the migration.
    from migrate_future_system import _assert_tracks_v28_intact
    with pytest.raises(RuntimeError, match="already has 'track_type'"):
        _assert_tracks_v28_intact(conn)


def test_preflight_rejects_missing_derived_status(tmp_path):
    """Preflight must reject if v28 prerequisite (derived_status) is missing."""
    conn = _base_v28_db(tmp_path)
    # We can't easily drop a column in SQLite, so we simulate v27 by checking
    # the preflight logic directly with a minimal connection.
    db_path = tmp_path / "v27_sim.db"
    c = sqlite3.connect(str(db_path))
    c.execute("""
        CREATE TABLE tracks (
            track_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            title TEXT NOT NULL,
            goal_state TEXT,
            horizon TEXT,
            PRIMARY KEY (track_id, project_id)
        )
    """)
    c.commit()
    from migrate_future_system import _assert_tracks_v28_intact
    with pytest.raises(RuntimeError, match="missing 'derived_status'"):
        _assert_tracks_v28_intact(c)
    c.close()
