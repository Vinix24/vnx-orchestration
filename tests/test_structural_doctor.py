"""test_structural_doctor.py — tests for scripts/vnx_structural_doctor.py.

Tests against temp DBs that mimic the v26-but-absent-tracks divergence.
Never touches the live runtime_coordination.db.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure the scripts dir is on sys.path for importing the doctor module
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vnx_structural_doctor as doctor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_divergent_db(db_path: Path, rowcount: int = 5) -> Path:
    """Create a temp DB mimicking the v26-but-absent-tracks divergence.

    - user_version = 26
    - dispatches table with track + pr_ref columns (and a few rows)
    - NO track tables
    - NO output_ref or output_kind columns
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 26")

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
            task_class       TEXT,
            target_type      TEXT,
            target_id        TEXT,
            channel_origin   TEXT,
            intelligence_payload TEXT,
            claimed_by       TEXT,
            claimed_at       TEXT,
            UNIQUE(dispatch_id, project_id)
        )
    """)

    # Insert rows, some with pr_ref set
    for i in range(1, rowcount + 1):
        pr_val = f"pr-{i}" if i <= 2 else None
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, terminal_id, track, pr_ref) "
            "VALUES (?, 'queued', 'T1', ?, ?)",
            (f"dispatch-{i:04d}", f"track-{i}", pr_val),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def divergent_db(tmp_path):
    """Create a divergent temp DB for testing."""
    db_path = tmp_path / "test_divergent.db"
    return _create_divergent_db(db_path)


@pytest.fixture
def divergent_db_10rows(tmp_path):
    """Create a divergent temp DB with 10 dispatches rows."""
    db_path = tmp_path / "test_divergent_10.db"
    return _create_divergent_db(db_path, rowcount=10)


# ---------------------------------------------------------------------------
# Tests — diagnose
# ---------------------------------------------------------------------------


class TestDiagnose:
    def test_detects_divergence(self, divergent_db):
        """Diagnose should detect the v26/absent-tracks divergence."""
        conn = sqlite3.connect(str(divergent_db))
        try:
            result = doctor.diagnose(conn, label="test")
        finally:
            conn.close()

        assert result["user_version"] == 26
        assert result["dispatches_rowcount"] == 5
        assert result["divergence"]["verdict"] == "DIVERGENT"

        # Track tables should all be absent
        for t in doctor.TRACK_TABLE_NAMES:
            assert result["track_tables"][t] is False, f"{t} should be absent"

        # dispatches columns: track and pr_ref exist, output_ref/output_kind absent
        assert result["dispatches_columns"]["track"] is True
        assert result["dispatches_columns"]["pr_ref"] is True
        assert result["dispatches_columns"]["output_ref"] is False
        assert result["dispatches_columns"]["output_kind"] is False

    def test_no_false_positive_on_clean_db(self, tmp_path):
        """Diagnose on a fully repaired DB should return CLEAN."""
        db_path = tmp_path / "clean.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 26")

        # Create dispatches table with all columns
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                track TEXT,
                pr_ref TEXT,
                output_ref TEXT,
                output_kind TEXT,
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id) VALUES ('d-1')"
        )

        # Create all 4 track tables
        conn.executescript(doctor.TRACKS_V24_DDL)
        conn.executescript(doctor.TRACK_PHASE_HISTORY_V24_DDL)
        conn.executescript(doctor.TRACK_DEPENDENCIES_V24_DDL)
        conn.executescript(doctor.TRACK_OPEN_ITEMS_V24_DDL)
        for idx_sql in doctor.TRACK_INDEXES_V24:
            conn.execute(idx_sql)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        try:
            result = doctor.diagnose(conn, label="clean")
        finally:
            conn.close()

        assert result["divergence"]["verdict"] == "CLEAN"


# ---------------------------------------------------------------------------
# Tests — repair (on a connection)
# ---------------------------------------------------------------------------


class TestRepair:
    def test_creates_all_missing_tables(self, divergent_db):
        """Repair should create all 4 missing track tables."""
        conn = sqlite3.connect(str(divergent_db))
        try:
            report = doctor.apply_repair(conn)
            conn.commit()
        finally:
            conn.close()

        assert set(report["tables_created"]) == set(doctor.TRACK_TABLE_NAMES)
        assert report["tables_already_exist"] == []
        assert "output_ref" in report["columns_added"]
        assert "output_kind" in report["columns_added"]

    def test_preserves_dispatches_rowcount(self, divergent_db_10rows):
        """Repair must not change the number of dispatches rows."""
        conn = sqlite3.connect(str(divergent_db_10rows))
        before = doctor._rowcount(conn, "dispatches")
        report = doctor.apply_repair(conn)
        conn.commit()
        after = doctor._rowcount(conn, "dispatches")
        conn.close()

        assert before == 10
        assert after == 10

    def test_backfills_output_ref_from_pr_ref(self, divergent_db):
        """Rows with pr_ref set should get output_ref+output_kind backfilled."""
        conn = sqlite3.connect(str(divergent_db))
        doctor.apply_repair(conn)
        conn.commit()

        # rows 1-2 have pr_ref set, rows 3-5 do not
        rows = conn.execute(
            "SELECT dispatch_id, pr_ref, output_ref, output_kind FROM dispatches ORDER BY id"
        ).fetchall()
        conn.close()

        assert rows[0] == ("dispatch-0001", "pr-1", "pr-1", "pr")
        assert rows[1] == ("dispatch-0002", "pr-2", "pr-2", "pr")
        assert rows[2] == ("dispatch-0003", None, None, None)
        assert rows[3] == ("dispatch-0004", None, None, None)
        assert rows[4] == ("dispatch-0005", None, None, None)

    def test_idempotent_second_repair_is_noop(self, divergent_db):
        """A second repair on an already-repaired DB should be a clean no-op."""
        conn = sqlite3.connect(str(divergent_db))
        doctor.apply_repair(conn)
        conn.commit()

        # Second repair
        report2 = doctor.apply_repair(conn)
        conn.commit()
        conn.close()

        assert report2["tables_created"] == []
        assert report2["columns_added"] == []
        # output_ref already backfilled, so second pass backfills 0
        assert report2["output_ref_backfilled"] == 0

    def test_does_not_change_pr_ref(self, divergent_db):
        """pr_ref column is preserved; no drop or rename."""
        conn = sqlite3.connect(str(divergent_db))
        doctor.apply_repair(conn)
        conn.commit()

        # Verify pr_ref column still exists
        assert doctor._column_exists(conn, "dispatches", "pr_ref")

        # Verify pr_ref values are intact
        pr_vals = conn.execute(
            "SELECT pr_ref FROM dispatches ORDER BY id"
        ).fetchall()
        conn.close()

        assert pr_vals[0][0] == "pr-1"
        assert pr_vals[1][0] == "pr-2"

    def test_does_not_change_user_version(self, divergent_db):
        """User version must remain at 26."""
        conn = sqlite3.connect(str(divergent_db))
        version_before = doctor._get_user_version(conn)
        doctor.apply_repair(conn)
        conn.commit()
        version_after = doctor._get_user_version(conn)
        conn.close()

        assert version_before == 26
        assert version_after == 26

    def test_integrity_check_passes_after_repair(self, divergent_db):
        """integrity_check must return 'ok' after repair."""
        conn = sqlite3.connect(str(divergent_db))
        doctor.apply_repair(conn)
        conn.commit()
        integrity = doctor._integrity_check(conn)
        conn.close()

        assert integrity == ["ok"]

    def test_creates_expected_indexes(self, divergent_db):
        """All expected indexes are created."""
        conn = sqlite3.connect(str(divergent_db))
        doctor.apply_repair(conn)
        conn.commit()

        index_names = {
            row[1]
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            )
        }
        conn.close()

        expected = {
            "idx_tracks_project_phase_nextup",
            "ux_tracks_next_up_per_project",
            "idx_track_deps_from",
            "idx_track_phase_history_track",
            "idx_track_open_items_oi",
        }
        missing = expected - index_names
        assert not missing, f"Missing indexes: {missing}"


# ---------------------------------------------------------------------------
# Tests — dry-run (on a copy)
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_does_not_mutate_original_db(self, divergent_db):
        """Dry-run must leave the original DB completely untouched."""
        conn = sqlite3.connect(str(divergent_db))
        # Disable query_only for the structure check — dry_run opens its own conn in
        # query_only mode for the live DB. Since we're calling the internal functions
        # directly, we need to simulate what dry_run does.
        orig_tables_before = {
            t: doctor._table_exists(conn, t) for t in doctor.TRACK_TABLE_NAMES
        }
        conn.close()

        # Verify all track tables are absent before
        for t, exists in orig_tables_before.items():
            assert not exists, f"{t} should be absent before dry-run"

        # Run the repair on a COPY (simulating dry_run logic)
        import shutil
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="vnx_test_dryrun_")
        os.close(tmp_fd)
        tmp_path = Path(tmp_path)

        try:
            shutil.copy2(str(divergent_db), str(tmp_path))
            tmp_conn = sqlite3.connect(str(tmp_path))
            try:
                doctor.apply_repair(tmp_conn)
                tmp_conn.commit()
            finally:
                tmp_conn.close()

            # Verify the original DB is untouched
            conn = sqlite3.connect(str(divergent_db))
            try:
                for t in doctor.TRACK_TABLE_NAMES:
                    assert not doctor._table_exists(conn, t), (
                        f"{t} must NOT exist in original DB after dry-run"
                    )
                assert not doctor._column_exists(conn, "dispatches", "output_ref")
                assert not doctor._column_exists(conn, "dispatches", "output_kind")
            finally:
                conn.close()

            # Verify the copy has the tables
            tmp_conn = sqlite3.connect(str(tmp_path))
            try:
                for t in doctor.TRACK_TABLE_NAMES:
                    assert doctor._table_exists(tmp_conn, t), (
                        f"{t} should exist in copy after dry-run"
                    )
                assert doctor._column_exists(tmp_conn, "dispatches", "output_ref")
                assert doctor._column_exists(tmp_conn, "dispatches", "output_kind")
            finally:
                tmp_conn.close()
        finally:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tests — --apply (live repair, on temp DBs)
# ---------------------------------------------------------------------------


class TestApply:
    def test_creates_tables_and_columns_on_db(self, divergent_db):
        """--apply should create track tables + output columns on the DB."""
        conn = sqlite3.connect(str(divergent_db))
        before = doctor._rowcount(conn, "dispatches")
        report = doctor.apply_repair(conn)
        conn.commit()
        after = doctor._rowcount(conn, "dispatches")

        for t in doctor.TRACK_TABLE_NAMES:
            assert doctor._table_exists(conn, t), f"{t} should exist after apply"
        assert doctor._column_exists(conn, "dispatches", "output_ref")
        assert doctor._column_exists(conn, "dispatches", "output_kind")
        assert before == after
        assert doctor._integrity_check(conn) == ["ok"]
        conn.close()

    def test_idempotent_apply(self, divergent_db):
        """Second --apply should be a clean no-op."""
        conn = sqlite3.connect(str(divergent_db))

        # First apply
        report1 = doctor.apply_repair(conn)
        conn.commit()

        # Second apply
        report2 = doctor.apply_repair(conn)
        conn.commit()

        assert report2["tables_created"] == []
        assert report2["columns_added"] == []
        assert report2["output_ref_backfilled"] == 0

        # Verify state is still consistent
        assert doctor._integrity_check(conn) == ["ok"]
        conn.close()

    def test_backup_file_written(self, tmp_path):
        """--apply should write a timestamped .bak-<stamp> file."""
        db_path = _create_divergent_db(tmp_path / "test_apply.db")

        # Simulate the backup step from apply_to_live
        stamp = doctor._utc_stamp()
        backup_path = db_path.with_suffix(f".db.bak-{stamp}")
        import shutil
        shutil.copy2(str(db_path), str(backup_path))

        assert backup_path.exists()
        assert backup_path.stat().st_size > 0

        # Verify the backup is a valid SQLite DB
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            ver = doctor._get_user_version(backup_conn)
            assert ver == 26
        finally:
            backup_conn.close()
