"""Regression tests for two migrator bugs found during 2026-05-24 MC cutover attempt.

Bug 1: dispatch_experiments missing from canonical bootstrap
    _assert_central_tables_exist required dispatch_experiments in the central QI DB,
    but quality_db_init.bootstrap_qi_db did not create the table. Any source project
    that had dispatch_experiments in its quality_intelligence.db triggered a
    BootstrapFailure immediately after --fresh-central bootstrap.

    Fix: V18 migration block in quality_db_init.bootstrap_qi_db creates
    dispatch_experiments with the canonical schema (matching
    dispatch_parameter_tracker.init_schema + project_id column).

Bug 2: _restore_snapshot swallows primary exception
    When the primary exception (e.g. BootstrapFailure) triggered the rollback path,
    _restore_snapshot raised sqlite3.OperationalError (disk I/O error) which replaced
    the primary exception in the operator's terminal output. The actual root cause was
    buried in the stderr log only, causing confusion (operator thought disk was full).

    Fix: _restore_snapshot_safe wraps _restore_snapshot in try/except, logs the
    rollback failure at ERROR level, and does NOT re-raise — leaving the primary
    exception as the one the caller's except-block handles.

Dispatch-ID: 20260524-221305-migrator-v2-dispatch-experiments-rollback
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M
from scripts.quality_db_init import bootstrap_qi_db


# ---------------------------------------------------------------------------
# Helpers shared between tests
# ---------------------------------------------------------------------------

def _make_source_qi_with_dispatch_experiments(path: Path, project_id: str) -> None:
    """Create a source QI DB that includes dispatch_experiments rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS dispatch_metadata (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal    TEXT NOT NULL,
                track       TEXT NOT NULL,
                role        TEXT,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE IF NOT EXISTS dispatch_experiments (
                id          INTEGER PRIMARY KEY,
                dispatch_id TEXT UNIQUE,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                role        TEXT,
                success     BOOLEAN,
                project_id  TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, role, project_id)"
            " VALUES (?, 'T1', 'A', 'developer', ?)",
            (f"{project_id}-d-0", project_id),
        )
        con.execute(
            "INSERT INTO dispatch_experiments (dispatch_id, role, project_id)"
            " VALUES (?, 'developer', ?)",
            (f"{project_id}-d-0", project_id),
        )
        con.commit()
    finally:
        con.close()


def _make_source_rc_db(path: Path, project_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state       TEXT NOT NULL DEFAULT 'queued',
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT OR IGNORE INTO runtime_schema_version (version, description)
            VALUES (10, 'test-fixture');
            """
        )
        con.commit()
    finally:
        con.close()


def _build_fixture_with_dispatch_experiments(tmp_path: Path) -> tuple[Path, Path]:
    """Build 2-project fixture where both projects have dispatch_experiments rows."""
    project_ids = ["proj-x", "proj-y"]
    specs = []
    for pid in project_ids:
        proj_dir = tmp_path / pid
        state_dir = proj_dir / ".vnx-data" / "state"
        _make_source_qi_with_dispatch_experiments(state_dir / "quality_intelligence.db", pid)
        _make_source_rc_db(state_dir / "runtime_coordination.db", pid)
        specs.append({"name": pid, "path": str(proj_dir), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))
    backup_base = tmp_path / "backups"
    backup_base.mkdir()
    return registry, backup_base


# ---------------------------------------------------------------------------
# Bug 1 regression: dispatch_experiments in canonical bootstrap
# ---------------------------------------------------------------------------

class TestDispatchExperimentsInCanonicalBootstrap:
    """Bug 1: dispatch_experiments must be present after bootstrap_qi_db."""

    def test_bootstrap_creates_dispatch_experiments_table(self, tmp_path):
        """bootstrap_qi_db must create dispatch_experiments so central DB satisfies
        _assert_central_tables_exist when source DBs contain the table.
        """
        db_path = tmp_path / "quality_intelligence.db"
        schema_file = ROOT / "schemas" / "quality_intelligence.sql"

        result = bootstrap_qi_db(db_path, schema_file)

        assert result, "bootstrap_qi_db must return True on success"
        assert db_path.exists(), "DB file must exist after bootstrap"

        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='dispatch_experiments'"
            ).fetchone()
        finally:
            con.close()

        assert row is not None, (
            "dispatch_experiments table absent from canonical bootstrap output. "
            "V18 migration block in bootstrap_qi_db must create it."
        )

    def test_bootstrap_dispatch_experiments_has_required_columns(self, tmp_path):
        """dispatch_experiments must have the full column set including project_id."""
        db_path = tmp_path / "quality_intelligence.db"
        schema_file = ROOT / "schemas" / "quality_intelligence.sql"

        bootstrap_qi_db(db_path, schema_file)

        con = sqlite3.connect(str(db_path))
        try:
            columns = {r[1] for r in con.execute("PRAGMA table_info(dispatch_experiments)")}
        finally:
            con.close()

        required = {
            "id", "dispatch_id", "timestamp", "role", "success",
            "cqs", "completion_minutes", "committed", "lines_changed", "project_id",
        }
        missing = required - columns
        assert not missing, (
            f"dispatch_experiments is missing required columns after bootstrap: {missing}. "
            f"Columns present: {columns}"
        )

    def test_fresh_central_succeeds_with_source_dispatch_experiments(self, tmp_path, monkeypatch):
        """--fresh-central must exit 0 when source DBs contain dispatch_experiments.

        Before Bug 1 fix: BootstrapFailure 'central DB(s) missing required
        import-target tables: quality_intelligence.dispatch_experiments'.
        After fix: bootstrap creates the table; assertion passes.
        """
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture_with_dispatch_experiments(tmp_path)
        central_state = tmp_path / "central" / "state"

        rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(central_state),
        ])

        assert rc == 0, (
            f"Expected exit 0; got {rc}. "
            "Check that dispatch_experiments is created during canonical bootstrap."
        )

    def test_fresh_central_dispatch_experiments_rows_imported(self, tmp_path, monkeypatch):
        """After --fresh-central, dispatch_experiments rows from source DBs must be in central."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture_with_dispatch_experiments(tmp_path)
        central_state = tmp_path / "central" / "state"

        rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(central_state),
        ])

        assert rc == 0
        qi_db = central_state / "quality_intelligence.db"
        con = sqlite3.connect(str(qi_db))
        try:
            count = con.execute("SELECT COUNT(*) FROM dispatch_experiments").fetchone()[0]
            project_ids = sorted(
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT project_id FROM dispatch_experiments"
                )
            )
        finally:
            con.close()

        assert count >= 2, f"Expected at least 2 dispatch_experiments rows (one per project); got {count}"
        assert "proj-x" in project_ids and "proj-y" in project_ids, (
            f"project_ids in dispatch_experiments after import: {project_ids}"
        )

    def test_bootstrap_qi_db_dispatch_experiments_idempotent(self, tmp_path):
        """Running bootstrap_qi_db twice must not raise (CREATE TABLE IF NOT EXISTS is idempotent)."""
        db_path = tmp_path / "quality_intelligence.db"
        schema_file = ROOT / "schemas" / "quality_intelligence.sql"

        first = bootstrap_qi_db(db_path, schema_file)
        second = bootstrap_qi_db(db_path, schema_file)

        assert first and second, "Both bootstrap_qi_db calls must return True"


# ---------------------------------------------------------------------------
# Bug 2 regression: _restore_snapshot_safe does not mask primary exception
# ---------------------------------------------------------------------------

class TestRestoreSnapshotSafe:
    """Bug 2: _restore_snapshot_safe must log rollback failure without raising."""

    def test_restore_snapshot_safe_swallows_oserror(self, tmp_path, caplog):
        """_restore_snapshot_safe must not raise when _restore_snapshot raises."""
        import logging

        # Provide an empty snapshots dict so _restore_snapshot is a no-op,
        # but patch _restore_snapshot to always raise to simulate the I/O error.
        with patch.object(M, "_restore_snapshot", side_effect=sqlite3.OperationalError("disk I/O error")):
            with caplog.at_level(logging.ERROR, logger="vnx.migrate.apply"):
                # Must not raise.
                M._restore_snapshot_safe({}, tmp_path / "qi.db", tmp_path / "rc.db")

        assert any(
            "snapshot restore (rollback) failed" in record.message
            for record in caplog.records
        ), "Expected ERROR log about rollback failure; got: " + str([r.message for r in caplog.records])

    def test_restore_snapshot_safe_logs_full_exception_repr(self, tmp_path, caplog):
        """The logged ERROR must include a repr of the original rollback exception."""
        import logging

        with patch.object(
            M,
            "_restore_snapshot",
            side_effect=sqlite3.OperationalError("disk I/O error"),
        ):
            with caplog.at_level(logging.ERROR, logger="vnx.migrate.apply"):
                M._restore_snapshot_safe({}, tmp_path / "qi.db", tmp_path / "rc.db")

        error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("disk I/O error" in msg for msg in error_messages), (
            f"Expected 'disk I/O error' in logged ERROR; got: {error_messages}"
        )

    def test_primary_exception_propagates_when_rollback_fails(self, tmp_path, monkeypatch):
        """Primary exception (BootstrapFailure) must survive even when rollback also fails.

        Simulates the exact failure mode from 2026-05-24:
        1. _assert_central_tables_exist raises BootstrapFailure
        2. _restore_snapshot raises sqlite3.OperationalError (disk I/O error)

        Before fix: operator saw only the I/O error (BootstrapFailure hidden).
        After fix: main() returns exit code 3 (from the BootstrapFailure handler),
        and the BootstrapFailure ERROR log is emitted before any rollback error.
        """
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture_with_dispatch_experiments(tmp_path)
        central_state = tmp_path / "central" / "state"

        # Force _assert_central_tables_exist to raise BootstrapFailure,
        # AND force _restore_snapshot to raise OperationalError.
        def _raise_bootstrap_failure(*args, **kwargs):
            raise M.BootstrapFailure("injected: test table missing")

        def _raise_io_error(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        with patch.object(M, "_assert_central_tables_exist", side_effect=_raise_bootstrap_failure):
            with patch.object(M, "_restore_snapshot", side_effect=_raise_io_error):
                rc = M.main([
                    "--apply",
                    "--confirm", M.CONFIRMATION_PHRASE,
                    "--no-prompt",
                    "--fresh-central",
                    "--registry", str(registry),
                    "--backup-base", str(backup_base),
                    "--central-state", str(central_state),
                ])

        # Exit code 3 comes from the BootstrapFailure handler, not from the I/O error.
        # Before the fix, sqlite3.OperationalError propagated up and the caller raised
        # SystemExit with that as the message instead.
        assert rc == 3, (
            f"Expected exit code 3 (BootstrapFailure path); got {rc}. "
            "The primary exception must determine the exit code, not the rollback error."
        )

    def test_restore_snapshot_safe_succeeds_on_empty_snapshots(self, tmp_path):
        """_restore_snapshot_safe with empty snapshots dict is a no-op (no raise)."""
        # Should complete without error when there is nothing to restore.
        M._restore_snapshot_safe({}, tmp_path / "qi.db", tmp_path / "rc.db")

    def test_restore_snapshot_safe_succeeds_on_real_db(self, tmp_path):
        """_restore_snapshot_safe restores real snapshots without raising."""
        qi = tmp_path / "qi.db"
        rc = tmp_path / "rc.db"

        # Create minimal DBs.
        for path in (qi, rc):
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE t (x INTEGER)")
            con.execute("INSERT INTO t VALUES (42)")
            con.commit()
            con.close()

        # Take snapshots.
        snaps = M._snapshot_central(qi, rc)

        # Corrupt live DBs to confirm restore actually does something.
        for path in (qi, rc):
            con = sqlite3.connect(str(path))
            con.execute("DELETE FROM t")
            con.commit()
            con.close()

        # Restore via safe wrapper.
        M._restore_snapshot_safe(snaps, qi, rc)

        # Verify restore worked.
        for path in (qi, rc):
            con = sqlite3.connect(str(path))
            val = con.execute("SELECT x FROM t").fetchone()
            con.close()
            assert val is not None and val[0] == 42, (
                f"Expected restored row (42) in {path.name}; got {val}"
            )
