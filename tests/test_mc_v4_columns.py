"""tests/test_mc_v4_columns.py — MC v4-column preservation through the 0022 walk.

Four scenarios per the problem spec:

  (a) Guard allows extra columns + still fails on a missing required column.
  (b) v22-shape dispatches with 5 extra empty columns → 0022 step SKIPS its
      rebuild + preserves the extra columns + bumps to v22.
  (c) Genuine pre-v22 dispatches WITH an extra column → rebuild preserves it
      (dynamic copy).
  (d) Full walk on a synthetic MC-shaped store (user_version=20, dispatches
      composite + 5 v4 extra columns, headless_runs ghost FK) reaches v31 with
      the 5 columns intact.

All tests use tmp_path fixtures only — ~/.vnx-data is NEVER opened.

ADR-007: composite UNIQUE/PK over project_id.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import migrate_future_system as mfs  # noqa: E402
import schema_migration  # noqa: E402


# ---------------------------------------------------------------------------
# Autouse isolation — pin VNX_DATA_DIR to a tmp dir, never ~/.vnx-data
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR for every test."""
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_mc_v4_data"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)


# ---------------------------------------------------------------------------
# DB helper: canonical path anchored at .vnx-data/<pid>/state/
# ---------------------------------------------------------------------------

def _make_db_path(tmp_path: Path, pid: str) -> Path:
    """Canonical DB path shape so _project_id_from_db_path resolves pid."""
    state_dir = tmp_path / ".vnx-data" / pid / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "runtime_coordination.db"


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


# ---------------------------------------------------------------------------
# Shared: 5 VNX-canonical v4 column names
# ---------------------------------------------------------------------------

_V4_COLS = ("task_class", "target_type", "target_id", "channel_origin", "intelligence_payload")


# ---------------------------------------------------------------------------
# Schema builders
# ---------------------------------------------------------------------------

def _create_dispatches_v22_shape_with_v4(conn: sqlite3.Connection) -> None:
    """Create dispatches in post-v22 shape (composite UNIQUE + operator_approved_at)
    PLUS the 5 VNX-canonical v4 forward-scaffolding columns.  Mirrors MC's real DB.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id          TEXT    NOT NULL,
            project_id           TEXT    NOT NULL DEFAULT 'vnx-dev',
            state                TEXT    NOT NULL DEFAULT 'proposed'
                                         CHECK (state IN (
                                             'proposed', 'ready', 'active', 'completed', 'failed',
                                             'queued', 'claimed', 'delivering', 'accepted', 'running',
                                             'timed_out', 'failed_delivery', 'expired', 'recovered',
                                             'dead_letter'
                                         )),
            terminal_id          TEXT,
            track                TEXT,
            priority             TEXT    DEFAULT 'P2',
            pr_ref               TEXT,
            gate                 TEXT,
            attempt_count        INTEGER NOT NULL DEFAULT 0,
            bundle_path          TEXT,
            created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after        TEXT,
            metadata_json        TEXT    DEFAULT '{}',
            operator_approved_at TEXT,
            task_class           TEXT,
            target_type          TEXT,
            target_id            TEXT,
            channel_origin       TEXT,
            intelligence_payload TEXT,
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()


def _create_dispatches_pre_v22_with_extra(conn: sqlite3.Connection) -> None:
    """Create a genuine pre-v22 dispatches table (NO operator_approved_at, NO v22 state CHECK)
    WITH one extra column beyond the 15-column required baseline.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'proposed',
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
            legacy_extra    TEXT,
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()


def _build_mc_shaped_store(db_path: Path, pid: str) -> None:
    """Build a synthetic MC-shaped store: user_version=20, dispatches in v22-shape
    with 5 v4 columns, and a headless_runs ghost-FK table (the real MC shape).
    """
    conn = _open(db_path)
    try:
        _create_dispatches_v22_shape_with_v4(conn)

        # headless_runs with GHOST FK to dispatches_pre_v22 (real MC artifact)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches_pre_v22 (
                id INTEGER PRIMARY KEY,
                dispatch_id TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS headless_runs (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id                  TEXT    NOT NULL,
                dispatch_id             TEXT    NOT NULL,
                attempt_id              TEXT    NOT NULL,
                target_id               TEXT    NOT NULL,
                target_type             TEXT    NOT NULL,
                task_class              TEXT    NOT NULL,
                terminal_id             TEXT,
                pid                     INTEGER,
                pgid                    INTEGER,
                state                   TEXT    NOT NULL DEFAULT 'init',
                failure_class           TEXT,
                exit_code               INTEGER,
                started_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                subprocess_started_at   TEXT,
                heartbeat_at            TEXT,
                last_output_at          TEXT,
                completed_at            TEXT,
                duration_seconds        REAL,
                log_artifact_path       TEXT,
                output_artifact_path    TEXT,
                receipt_id              TEXT,
                metadata_json           TEXT    DEFAULT '{}'
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_headless_run_state "
            "ON headless_runs(state, started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_headless_run_dispatch "
            "ON headless_runs(dispatch_id)"
        )

        conn.execute("PRAGMA user_version = 20")
        conn.commit()
    finally:
        conn.close()


def _cols(db_path: Path, table: str) -> set[str]:
    """Return column names for table in db_path."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
    finally:
        conn.close()


# ===========================================================================
# (a) Guard: allows extra columns; fails on missing required column
# ===========================================================================

class TestGuardBehavior:
    """_assert_dispatches_schema_intact relaxed guard."""

    def test_guard_allows_extra_columns(self, tmp_path: Path) -> None:
        """Guard must NOT raise when dispatches has extra columns beyond the 15 required."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_v22_shape_with_v4(conn)

        # Should not raise — extra v4 columns are allowed.
        mfs._assert_dispatches_schema_intact(conn)
        conn.close()

    def test_guard_fails_on_missing_required_column(self, tmp_path: Path) -> None:
        """Guard raises RuntimeError when a required column is absent."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # Create dispatches with metadata_json missing.
        conn.execute("""
            CREATE TABLE dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL,
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
                state           TEXT    NOT NULL DEFAULT 'proposed',
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
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.commit()

        with pytest.raises(RuntimeError, match="missing required columns"):
            mfs._assert_dispatches_schema_intact(conn)
        conn.close()

    def test_guard_fails_on_missing_composite_unique(self, tmp_path: Path) -> None:
        """Guard raises when composite UNIQUE(dispatch_id, project_id) is absent."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # All required columns present but NO composite UNIQUE.
        conn.execute("""
            CREATE TABLE dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL UNIQUE,
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
                state           TEXT    NOT NULL DEFAULT 'proposed',
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
                metadata_json   TEXT    DEFAULT '{}'
            )
        """)
        conn.commit()

        with pytest.raises(RuntimeError, match="UNIQUE"):
            mfs._assert_dispatches_schema_intact(conn)
        conn.close()

    def test_guard_extra_cols_no_missing_no_raise(self, tmp_path: Path) -> None:
        """Guard with extra v4 cols AND correct composite UNIQUE → no raise."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_v22_shape_with_v4(conn)

        # All five v4 extra columns present; must not raise.
        try:
            mfs._assert_dispatches_schema_intact(conn)
        except RuntimeError as exc:
            pytest.fail(f"Guard raised unexpectedly: {exc}")
        finally:
            conn.close()


# ===========================================================================
# (b) v22-shape dispatches + 5 extra cols → 0022 skips rebuild, preserves cols
# ===========================================================================

class TestV22ShapeSkip:
    """MC scenario: dispatches already in v22-shape before user_version=22."""

    def test_skip_rebuild_preserves_v4_columns(self, tmp_path: Path) -> None:
        """0022 step skips rebuild and preserves all 5 v4 extra columns."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_v22_shape_with_v4(conn)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        # Insert some rows to verify data preserved.
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES ('d-001', 'mc-project')"
        )
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()

        # user_version bumped to 22.
        assert schema_migration.get_user_version(conn) == 22

        # All 5 v4 columns still present.
        present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        for col in _V4_COLS:
            assert col in present, f"v4 column '{col}' was dropped by 0022 skip-rebuild"

        # Existing row still present.
        count = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        assert count == 1

        # Track tables created.
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for t in ("tracks", "track_phase_history", "track_dependencies", "track_open_items"):
            assert t in tables, f"Track table '{t}' missing after 0022 skip-rebuild"

        conn.close()

    def test_skip_rebuild_detects_v22_shape_correctly(self, tmp_path: Path) -> None:
        """_dispatches_already_v22_shape returns True for MC-shaped dispatches."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_v22_shape_with_v4(conn)

        assert mfs._dispatches_already_v22_shape(conn) is True
        conn.close()

    def test_genuine_pre_v22_not_detected_as_v22_shape(self, tmp_path: Path) -> None:
        """_dispatches_already_v22_shape returns False for genuine pre-v22 dispatches."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_pre_v22_with_extra(conn)

        assert mfs._dispatches_already_v22_shape(conn) is False
        conn.close()

    def test_idempotent_second_apply(self, tmp_path: Path) -> None:
        """Calling apply_migration when user_version is already 22 is a no-op."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_v22_shape_with_v4(conn)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()
        assert schema_migration.get_user_version(conn) == 22

        # Call again — should not raise or regress.
        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()
        assert schema_migration.get_user_version(conn) == 22

        # v4 cols still intact.
        present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        for col in _V4_COLS:
            assert col in present
        conn.close()


# ===========================================================================
# (c) Genuine pre-v22 dispatches WITH extra column → dynamic rebuild preserves it
# ===========================================================================

class TestDynamicRebuildPreservesExtra:
    """Genuine pre-v22 store with one extra column → dynamic copy preserves it."""

    def test_extra_column_preserved_through_rebuild(self, tmp_path: Path) -> None:
        """Pre-v22 dispatches with legacy_extra column → column present after 0022."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_pre_v22_with_extra(conn)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        # Seed one row with a value in the extra column.
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, legacy_extra) "
            "VALUES ('d-001', 'test-pid', 'sentinel-value')"
        )
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()

        assert schema_migration.get_user_version(conn) == 22

        # legacy_extra column still present.
        present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert "legacy_extra" in present, "legacy_extra was dropped by dynamic rebuild"

        # operator_approved_at added (it's part of v22 shape).
        assert "operator_approved_at" in present

        # Row data preserved including extra column value.
        row = conn.execute(
            "SELECT legacy_extra FROM dispatches WHERE dispatch_id='d-001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "sentinel-value", f"legacy_extra value lost: {row[0]!r}"

        conn.close()

    def test_dynamic_rebuild_produces_composite_unique(self, tmp_path: Path) -> None:
        """Dynamic rebuild result has UNIQUE(dispatch_id, project_id)."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_pre_v22_with_extra(conn)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()

        assert mfs._has_composite_unique(conn), "composite UNIQUE missing after dynamic rebuild"
        conn.close()

    def test_dynamic_rebuild_creates_track_tables(self, tmp_path: Path) -> None:
        """Track tables exist after the dynamic rebuild path."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        _create_dispatches_pre_v22_with_extra(conn)
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for t in ("tracks", "track_phase_history", "track_dependencies", "track_open_items"):
            assert t in tables, f"Track table '{t}' missing after dynamic rebuild"
        conn.close()

    def test_no_dynamic_path_when_no_extra_cols(self, tmp_path: Path) -> None:
        """Standard path used (SQL file) when pre-v22 dispatches has no extra columns."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        # Exactly 15 required columns, no extras.
        conn.execute("""
            CREATE TABLE dispatches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id     TEXT    NOT NULL,
                project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
                state           TEXT    NOT NULL DEFAULT 'proposed',
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
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

        mfs.apply_migration(conn, _PROJECT_ROOT)
        conn.commit()

        assert schema_migration.get_user_version(conn) == 22
        # operator_approved_at added by standard path.
        present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
        assert "operator_approved_at" in present
        conn.close()


# ===========================================================================
# (d) Full walk on synthetic MC-shaped store → reaches v31, 5 columns intact
# ===========================================================================

class TestFullWalkMCShape:
    """Full 0022→0031 walk on a MC-shaped store preserves 5 v4 forward-scaffolding cols."""

    def _run_walk(self, db_path: Path) -> None:
        """Open a connection and run the numbered walk 0022→0031."""
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            project_root = db_path.parent.parent.parent.parent
            mfs._run_numbered_walk(conn, project_root)
            conn.commit()
        finally:
            conn.close()

    def test_full_walk_reaches_v31(self, tmp_path: Path) -> None:
        """MC-shaped store at user_version=20 reaches user_version=31 after walk."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        self._run_walk(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            final_version = schema_migration.get_user_version(conn)
        finally:
            conn.close()

        assert final_version == 31, f"Expected user_version=31 after walk, got {final_version}"

    def test_full_walk_preserves_all_v4_columns(self, tmp_path: Path) -> None:
        """All 5 v4 columns survive the full 0022→0031 walk."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        self._run_walk(db_path)

        present = _cols(db_path, "dispatches")
        for col in _V4_COLS:
            assert col in present, (
                f"v4 column '{col}' was dropped during the full 0022→0031 walk"
            )

    def test_full_walk_preserves_existing_rows(self, tmp_path: Path) -> None:
        """Rows seeded before the walk survive with their data intact."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        # Seed a row before the walk.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, task_class) "
            "VALUES ('d-mc-001', 'mission-control', 'coding_interactive')"
        )
        conn.commit()
        conn.close()

        self._run_walk(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT dispatch_id, project_id, task_class FROM dispatches "
                "WHERE dispatch_id='d-mc-001'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "Seeded row missing after walk"
        assert row[0] == "d-mc-001"
        assert row[1] == "mission-control"
        assert row[2] == "coding_interactive", f"task_class value lost: {row[2]!r}"

    def test_full_walk_composite_unique_intact(self, tmp_path: Path) -> None:
        """dispatches retains UNIQUE(dispatch_id, project_id) after the full walk."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        self._run_walk(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            has_composite = mfs._has_composite_unique(conn)
        finally:
            conn.close()

        assert has_composite, "dispatches lost UNIQUE(dispatch_id, project_id) after walk"

    def test_full_walk_track_tables_created(self, tmp_path: Path) -> None:
        """Track-layer tables exist after the full walk."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        self._run_walk(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            conn.close()

        for t in ("tracks", "track_phase_history", "track_dependencies", "track_open_items"):
            assert t in tables, f"Track table '{t}' missing after full walk"

    def test_full_walk_v4_cols_nullable_and_empty(self, tmp_path: Path) -> None:
        """v4 columns survive as nullable TEXT; new rows can be inserted without them."""
        pid = "mission-control"
        db_path = _make_db_path(tmp_path, pid)
        _build_mc_shaped_store(db_path, pid)

        self._run_walk(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            # Insert a minimal row without specifying any v4 column.
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id) "
                "VALUES ('d-minimal', 'mission-control')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT task_class, target_type, target_id, channel_origin, intelligence_payload "
                "FROM dispatches WHERE dispatch_id='d-minimal'"
            ).fetchone()
            # All v4 cols should be NULL (no default value set).
            assert all(v is None for v in row), f"Expected NULL v4 cols, got {row!r}"
        finally:
            conn.close()


# ===========================================================================
# (e) FK-guard: ghost FK on child table does NOT block the dynamic rebuild DROP
# ===========================================================================

class TestDynamicRebuildGhostFKGuard:
    """Regression test for the MC ghost-FK bug.

    Scenario: a genuine pre-v22 dispatches table WITH an extra column, plus a child
    table carrying a FOREIGN KEY referencing the soon-to-be-renamed/dropped shadow
    table (dispatches_pre_v22).  With foreign_keys ON and the PRAGMA set INSIDE the
    savepoint (the old, broken behaviour), the DROP TABLE dispatches_pre_v22 raises
    sqlite3.IntegrityError.  The fix sets foreign_keys=OFF BEFORE the SAVEPOINT, so
    the DROP succeeds.  The dangling child FK is tolerated here (it is repaired by the
    adaptive 0031-branch in later migration steps).
    """

    def _build_store_with_ghost_fk(self, db_path: Path) -> None:
        """Create a pre-v22 dispatches with one extra column AND a headless_runs-like
        child table that has a GHOST FK pointing at dispatches_pre_v22.

        The real MC artifact: headless_runs carries a FK referencing dispatches_pre_v22,
        but dispatches_pre_v22 does NOT exist yet.  The migration RENAMES dispatches →
        dispatches_pre_v22, which instantiates the shadow; then tries to DROP it, which
        fails (FOREIGN KEY constraint) with foreign_keys=ON unless the PRAGMA is set OFF
        before the savepoint.  We must create the child table with foreign_keys=OFF so
        SQLite accepts the dangling FK without error at schema-creation time.
        """
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # Pre-v22 dispatches with one extra column (triggers the dynamic path).
            conn.execute("""
                CREATE TABLE dispatches (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id     TEXT    NOT NULL,
                    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
                    state           TEXT    NOT NULL DEFAULT 'proposed',
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
                    mc_extra        TEXT,
                    UNIQUE(dispatch_id, project_id)
                )
            """)

            # Child table with a GHOST FK referencing dispatches_pre_v22, which does NOT
            # exist at this point.  This mirrors the real MC schema oddity.  Created with
            # foreign_keys=OFF so SQLite accepts the dangling reference.
            # During migration, RENAME dispatches → dispatches_pre_v22 makes this FK
            # "real"; the subsequent DROP TABLE dispatches_pre_v22 then fails with
            # IntegrityError when foreign_keys=ON — exactly the bug being fixed.
            conn.execute("""
                CREATE TABLE child_with_ghost_fk (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id TEXT NOT NULL,
                    FOREIGN KEY (dispatch_id) REFERENCES dispatches_pre_v22(dispatch_id)
                )
            """)

            # Seed one dispatches row to verify data survives.
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, mc_extra) "
                "VALUES ('d-ghost-001', 'mc-ghost', 'ghost-sentinel')"
            )

            conn.execute("PRAGMA user_version = 20")
            conn.commit()
        finally:
            conn.close()

    def test_dynamic_rebuild_succeeds_with_ghost_fk(self, tmp_path: Path) -> None:
        """_apply_0022_dynamic_rebuild must not raise IntegrityError when a child table
        carries a FK referencing dispatches_pre_v22 (the dropped shadow table).
        """
        db_path = tmp_path / "ghost_fk.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")  # matches the MC production setting
        try:
            # Must not raise sqlite3.IntegrityError: FOREIGN KEY constraint failed.
            mfs._apply_0022_dynamic_rebuild(conn)
            conn.commit()
        finally:
            conn.close()

    def test_extra_column_preserved_despite_ghost_fk(self, tmp_path: Path) -> None:
        """The mc_extra column survives the dynamic rebuild even with the ghost FK present."""
        db_path = tmp_path / "ghost_fk_col.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            mfs._apply_0022_dynamic_rebuild(conn)
            conn.commit()
            present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
            assert "mc_extra" in present, "mc_extra column was dropped by dynamic rebuild"
        finally:
            conn.close()

    def test_user_version_reaches_22_with_ghost_fk(self, tmp_path: Path) -> None:
        """user_version is 22 after the dynamic rebuild completes with ghost FK present."""
        db_path = tmp_path / "ghost_fk_ver.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            mfs._apply_0022_dynamic_rebuild(conn)
            conn.commit()
            assert schema_migration.get_user_version(conn) == 22
        finally:
            conn.close()

    def test_row_data_preserved_despite_ghost_fk(self, tmp_path: Path) -> None:
        """Seeded row data (including the extra column value) survives the rebuild."""
        db_path = tmp_path / "ghost_fk_data.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            mfs._apply_0022_dynamic_rebuild(conn)
            conn.commit()
            row = conn.execute(
                "SELECT dispatch_id, project_id, mc_extra FROM dispatches "
                "WHERE dispatch_id='d-ghost-001'"
            ).fetchone()
            assert row is not None, "Seeded row missing after dynamic rebuild"
            assert row[0] == "d-ghost-001"
            assert row[1] == "mc-ghost"
            assert row[2] == "ghost-sentinel", f"mc_extra value lost: {row[2]!r}"
        finally:
            conn.close()

    def test_dangling_child_fk_tolerated_not_strict_check(self, tmp_path: Path) -> None:
        """The rebuild does NOT run a strict foreign_key_check after DROP; the dangling
        child FK on child_with_ghost_fk is left for the 0031-branch to repair.
        """
        db_path = tmp_path / "ghost_fk_tolerant.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            # Must complete without raising — the dangling ref is not our problem here.
            mfs._apply_0022_dynamic_rebuild(conn)
            conn.commit()

            # Verify child_with_ghost_fk still exists (not nuked by the rebuild).
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            assert "child_with_ghost_fk" in tables, (
                "child_with_ghost_fk was unexpectedly dropped by the 0022 rebuild"
            )
        finally:
            conn.close()

    def test_via_apply_migration_entry_point(self, tmp_path: Path) -> None:
        """The ghost-FK scenario succeeds via the public apply_migration entry point."""
        db_path = tmp_path / "ghost_fk_entry.db"
        self._build_store_with_ghost_fk(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            mfs.apply_migration(conn, _PROJECT_ROOT)
            conn.commit()
            assert schema_migration.get_user_version(conn) == 22
            present = {r[1] for r in conn.execute("PRAGMA table_info('dispatches')")}
            assert "mc_extra" in present, "mc_extra dropped via apply_migration entry point"
            assert "operator_approved_at" in present
        finally:
            conn.close()
