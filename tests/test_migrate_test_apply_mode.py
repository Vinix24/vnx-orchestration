"""Tests for --test-apply flag (PR-WAVE2A-4): test-mode apply.

Covers all 6 acceptance criteria from the Wave 2a blueprint:
  AC1 - --test-apply runs the full bootstrap + migration chain
  AC2 - Live ~/.vnx-data/state/ is unchanged after --test-apply
  AC3 - Source DBs remain read-only (never modified by --test-apply)
  AC4 - Output prints 'TEST MODE' banner at the start of execution
  AC5 - Failure exit codes from --test-apply match real apply (2/3/4)
  AC6 - --test-apply succeeds on fixture source DBs; temp central dir
         is cleaned up after the run (central DB gone post-execution)

Dispatch-ID: 20260525-081003-wave2a-4-test-apply-mode
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders (mirrors per-project test pattern)
# ---------------------------------------------------------------------------


def _make_source_qi_db(path: Path, project_id: str, row_count: int = 3) -> None:
    """Create a minimal quality_intelligence source DB with dispatch_metadata rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS success_patterns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,
                category     TEXT NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id   TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE IF NOT EXISTS dispatch_metadata (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal    TEXT NOT NULL,
                track       TEXT NOT NULL,
                role        TEXT,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        for i in range(row_count):
            con.execute(
                "INSERT INTO dispatch_metadata "
                "(dispatch_id, terminal, track, role, project_id)"
                " VALUES (?, 'T1', 'A', 'developer', ?)",
                (f"{project_id}-dispatch-{i}", project_id),
            )
        con.commit()
    finally:
        con.close()


def _make_source_rc_db(path: Path, project_id: str) -> None:
    """Create a minimal runtime_coordination source DB."""
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


def _build_fixture(
    tmp_path: Path,
    project_ids: list[str],
    rows_per_project: int = 3,
) -> tuple[Path, Path]:
    """Build source projects with minimal DBs and a registry JSON.

    Returns (registry_path, backup_base).
    """
    specs = []
    for pid in project_ids:
        proj_dir = tmp_path / pid
        state_dir = proj_dir / ".vnx-data" / "state"
        _make_source_qi_db(state_dir / "quality_intelligence.db", pid, rows_per_project)
        _make_source_rc_db(state_dir / "runtime_coordination.db", pid)
        specs.append({"name": pid, "path": str(proj_dir), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    return registry, backup_base


def _run_test_apply(
    tmp_path: Path,
    registry: Path,
    backup_base: Path,
    *,
    project: str | None = None,
    extra_args: list[str] | None = None,
) -> int:
    """Invoke M.main with --test-apply and return the exit code."""
    cmd = [
        "--test-apply",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
    ]
    if project is not None:
        cmd.extend(["--project", project])
    if extra_args:
        cmd.extend(extra_args)
    return M.main(cmd)


# ---------------------------------------------------------------------------
# AC1: --test-apply runs full bootstrap + migration chain
# ---------------------------------------------------------------------------


class TestAC1BootstrapChainRuns:
    def test_test_apply_exit_0_on_fixture_sources(self, tmp_path, monkeypatch):
        """AC1: --test-apply exits 0 and runs bootstrap + migration chain on fixture DBs."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a", "proj-b"])

        rc = _run_test_apply(tmp_path, registry, backup_base)

        assert rc == 0, f"--test-apply must exit 0 on clean fixture DBs; got {rc}"

    def test_test_apply_with_project_filter(self, tmp_path, monkeypatch):
        """AC1: --test-apply + --project runs the chain for the targeted project only."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(
            tmp_path, ["proj-a", "proj-b", "proj-c"]
        )

        rc = _run_test_apply(tmp_path, registry, backup_base, project="proj-a")

        assert rc == 0, f"--test-apply --project proj-a must exit 0; got {rc}"

    def test_test_apply_bootstraps_central_db_during_run(self, tmp_path, monkeypatch, caplog):
        """AC1: bootstrap INFO messages appear in logs during --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        with caplog.at_level(logging.INFO, logger="vnx.migrate.apply"):
            rc = _run_test_apply(tmp_path, registry, backup_base)

        assert rc == 0
        info_messages = " ".join(r.message for r in caplog.records if r.levelname == "INFO")
        # bootstrap is invoked because the temp dir is fresh
        assert "canonical bootstrap" in info_messages or "test-apply" in info_messages, (
            f"Expected bootstrap or test-apply log; got: {info_messages!r}"
        )

    def test_test_apply_implicit_fresh_central_no_flag_required(self, tmp_path, monkeypatch):
        """AC1: --test-apply does NOT require explicit --fresh-central flag from operator."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        # No --fresh-central in the args — test-apply must set it implicitly.
        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
        ])

        # Exit 1 would mean "fresh central needs --fresh-central acknowledgement"
        assert rc != 1, (
            "--test-apply must implicitly set --fresh-central; "
            "operator must NOT be required to pass it manually"
        )
        assert rc == 0, f"Expected exit 0; got {rc}"


# ---------------------------------------------------------------------------
# AC2: Live ~/.vnx-data/state/ unchanged after --test-apply
# ---------------------------------------------------------------------------


class TestAC2LiveCentralUnchanged:
    def test_real_central_state_dir_not_modified(self, tmp_path, monkeypatch):
        """AC2: the real CENTRAL_DATA_DIR is not touched during --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        # Use a fake "real" central dir that doesn't exist before the run.
        fake_real_central = tmp_path / "fake-real-central"
        # Override the module-level default so main() uses our fake path when
        # --central-state is not passed. Pass it explicitly so we know where
        # the "live" central is supposed to be.
        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(fake_real_central),
        ])

        # The real central path must NOT have been written to.
        assert rc == 0, f"Expected exit 0; got {rc}"
        assert not fake_real_central.exists(), (
            "--test-apply must NOT create or write the live central state dir; "
            f"found: {fake_real_central}"
        )

    def test_real_central_qi_db_not_created(self, tmp_path, monkeypatch):
        """AC2: quality_intelligence.db is not created in the real central path."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        real_central = tmp_path / "real-central"
        real_qi = real_central / "quality_intelligence.db"

        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(real_central),
        ])

        assert rc == 0
        assert not real_qi.exists(), (
            f"quality_intelligence.db must not be created in the real central path; "
            f"found {real_qi}"
        )

    def test_real_central_rc_db_not_created(self, tmp_path, monkeypatch):
        """AC2: runtime_coordination.db is not created in the real central path."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        real_central = tmp_path / "real-central"
        real_rc = real_central / "runtime_coordination.db"

        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(real_central),
        ])

        assert rc == 0
        assert not real_rc.exists(), (
            f"runtime_coordination.db must not be created in the real central path; "
            f"found {real_rc}"
        )

    def test_pre_existing_real_central_unchanged(self, tmp_path, monkeypatch):
        """AC2: a pre-existing real central DB is not modified by --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        # Create a real central with known content.
        real_central = tmp_path / "real-central"
        real_central.mkdir(parents=True)
        sentinel_db = real_central / "quality_intelligence.db"
        con = sqlite3.connect(str(sentinel_db))
        try:
            con.execute("CREATE TABLE sentinel (value TEXT)")
            con.execute("INSERT INTO sentinel VALUES ('original')")
            con.commit()
        finally:
            con.close()

        # Capture mtime before the run.
        mtime_before = sentinel_db.stat().st_mtime

        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(real_central),
        ])

        assert rc == 0
        # mtime must be unchanged — no writes to the real central DB.
        mtime_after = sentinel_db.stat().st_mtime
        assert mtime_before == mtime_after, (
            "Real central DB mtime changed — --test-apply must NOT write to it"
        )

        # Sentinel content must still be intact.
        con2 = sqlite3.connect(str(sentinel_db))
        try:
            row = con2.execute("SELECT value FROM sentinel").fetchone()
        finally:
            con2.close()
        assert row is not None and row[0] == "original", (
            "Real central DB content was modified by --test-apply"
        )


# ---------------------------------------------------------------------------
# AC3: Source DBs remain read-only
# ---------------------------------------------------------------------------


class TestAC3SourceDBsReadOnly:
    def test_source_qi_db_not_modified(self, tmp_path, monkeypatch):
        """AC3: source quality_intelligence.db mtime is unchanged after --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        src_qi = tmp_path / "proj-a" / ".vnx-data" / "state" / "quality_intelligence.db"
        mtime_before = src_qi.stat().st_mtime

        _run_test_apply(tmp_path, registry, backup_base)

        mtime_after = src_qi.stat().st_mtime
        assert mtime_before == mtime_after, (
            "Source quality_intelligence.db mtime changed — "
            "--test-apply must open source DBs read-only"
        )

    def test_source_rc_db_not_modified(self, tmp_path, monkeypatch):
        """AC3: source runtime_coordination.db mtime is unchanged after --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        src_rc = tmp_path / "proj-a" / ".vnx-data" / "state" / "runtime_coordination.db"
        mtime_before = src_rc.stat().st_mtime

        _run_test_apply(tmp_path, registry, backup_base)

        mtime_after = src_rc.stat().st_mtime
        assert mtime_before == mtime_after, (
            "Source runtime_coordination.db mtime changed — "
            "--test-apply must open source DBs read-only"
        )

    def test_no_wal_or_shm_written_to_source(self, tmp_path, monkeypatch):
        """AC3: no WAL or SHM sidecar files appear next to source DBs."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        src_state = tmp_path / "proj-a" / ".vnx-data" / "state"

        _run_test_apply(tmp_path, registry, backup_base)

        # SQLite opens in write mode when it creates WAL/SHM files.
        # These must not appear for read-only attached sources.
        for sidecar in src_state.glob("*.db-wal"):
            assert False, f"WAL sidecar created in source dir: {sidecar}"
        for sidecar in src_state.glob("*.db-shm"):
            assert False, f"SHM sidecar created in source dir: {sidecar}"

    def test_source_row_count_unchanged(self, tmp_path, monkeypatch):
        """AC3: source DB row count is identical before and after --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"], rows_per_project=5)

        src_qi = tmp_path / "proj-a" / ".vnx-data" / "state" / "quality_intelligence.db"

        def _count():
            con = sqlite3.connect(str(src_qi))
            try:
                return con.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
            finally:
                con.close()

        count_before = _count()
        _run_test_apply(tmp_path, registry, backup_base)
        count_after = _count()

        assert count_before == count_after, (
            f"Source dispatch_metadata row count changed: {count_before} → {count_after}; "
            "--test-apply must not modify source DBs"
        )


# ---------------------------------------------------------------------------
# AC4: Output banner 'TEST MODE'
# ---------------------------------------------------------------------------


class TestAC4TestModeBanner:
    def test_test_mode_banner_in_stdout(self, tmp_path, monkeypatch, capsys):
        """AC4: 'TEST MODE' appears in stdout output during --test-apply."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        _run_test_apply(tmp_path, registry, backup_base)

        captured = capsys.readouterr()
        assert "TEST MODE" in captured.out, (
            f"Expected 'TEST MODE' in stdout; got: {captured.out!r}"
        )

    def test_test_mode_banner_contains_real_path(self, tmp_path, monkeypatch, capsys):
        """AC4: banner includes the live central DB path so operator knows what is protected."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        live_central = tmp_path / "live-central"

        M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(live_central),
        ])

        captured = capsys.readouterr()
        assert "TEST MODE" in captured.out, (
            f"Expected 'TEST MODE' in stdout; got: {captured.out!r}"
        )
        assert str(live_central) in captured.out, (
            f"Expected live central path {live_central!s} in banner; "
            f"got: {captured.out!r}"
        )

    def test_no_test_mode_banner_in_normal_apply(self, tmp_path, monkeypatch, capsys):
        """AC4: 'TEST MODE' does NOT appear in stdout during a normal --apply run."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])
        central_state = tmp_path / "central" / "state"

        M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(central_state),
        ])

        captured = capsys.readouterr()
        assert "TEST MODE" not in captured.out, (
            "Normal --apply must NOT print 'TEST MODE' banner"
        )


# ---------------------------------------------------------------------------
# AC5: Failure exit codes match real apply (2/3/4)
# ---------------------------------------------------------------------------


class TestAC5FailureExitCodes:
    def test_unknown_project_id_exits_2(self, tmp_path, monkeypatch):
        """AC5: --test-apply with unknown --project exits 2 (same as real apply)."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a", "proj-b"])

        rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--project", "does-not-exist",
        ])

        assert rc == 2, f"Expected exit 2 for unknown --project in --test-apply; got {rc}"

    def test_verify_only_mutually_exclusive_exits_2(self, tmp_path, monkeypatch):
        """AC5: --test-apply + --verify-only exits 2 (mutually exclusive)."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        rc = M.main([
            "--test-apply",
            "--verify-only",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
        ])

        assert rc == 2, (
            f"Expected exit 2 for mutually exclusive --test-apply + --verify-only; got {rc}"
        )

    def test_bootstrap_failure_exits_3(self, tmp_path, monkeypatch):
        """AC5: when bootstrap raises BootstrapFailure, --test-apply exits 3."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        original_init = M._init_central_if_missing

        def _failing_init(qi_db, rc_db):
            raise M.BootstrapFailure("injected bootstrap failure for AC5 test")

        with patch.object(M, "_init_central_if_missing", side_effect=_failing_init):
            rc = M.main([
                "--test-apply",
                "--registry", str(registry),
                "--backup-base", str(backup_base),
            ])

        assert rc == 3, (
            f"Expected exit 3 when bootstrap fails inside --test-apply; got {rc}"
        )

    def test_verification_failure_exits_4(self, tmp_path, monkeypatch):
        """AC5: when verify_import raises VerificationFailure, --test-apply exits 4."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        original_verify = M.verify_import

        def _failing_verify(qi, rc, projects, run_id=None):
            result = original_verify(qi, rc, projects, run_id=run_id)
            result["discrepancies"] = [
                {"type": "count_mismatch", "project_id": "proj-a",
                 "table": "quality_intelligence.db.dispatch_metadata",
                 "source_rows": 3, "central_rows_for_project": 0}
            ]
            return result

        with patch.object(M, "verify_import", side_effect=_failing_verify):
            rc = M.main([
                "--test-apply",
                "--registry", str(registry),
                "--backup-base", str(backup_base),
            ])

        assert rc == 4, (
            f"Expected exit 4 when verification fails inside --test-apply; got {rc}"
        )

    def test_exit_codes_match_between_test_apply_and_real_apply(
        self, tmp_path, monkeypatch
    ):
        """AC5: --test-apply + --project unknown exits 2, same as --apply + --project unknown."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])
        central_state = tmp_path / "central" / "state"

        # Real apply with unknown project
        real_rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(central_state),
            "--project", "nonexistent",
        ])

        # Test-apply with same unknown project
        test_rc = M.main([
            "--test-apply",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--project", "nonexistent",
        ])

        assert real_rc == test_rc == 2, (
            f"Exit codes must match: real_apply={real_rc}, test_apply={test_rc}"
        )


# ---------------------------------------------------------------------------
# AC6: temp dir auto-cleaned after --test-apply; central DB inaccessible post-run
# ---------------------------------------------------------------------------


class TestAC6TempDirCleanup:
    def test_temp_dir_cleaned_on_success(self, tmp_path, monkeypatch):
        """AC6: the temp central dir created by --test-apply is removed after exit 0."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        # Intercept tempfile.mkdtemp so we know the temp dir path.
        created_tmp_dirs: list[str] = []
        original_mkdtemp = __import__("tempfile").mkdtemp

        def tracking_mkdtemp(prefix="", suffix="", dir=None):
            d = original_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
            if "vnx-test-apply-" in prefix:
                created_tmp_dirs.append(d)
            return d

        import tempfile
        with patch.object(tempfile, "mkdtemp", side_effect=tracking_mkdtemp):
            rc = _run_test_apply(tmp_path, registry, backup_base)

        assert rc == 0, f"Expected exit 0; got {rc}"
        assert len(created_tmp_dirs) == 1, (
            f"Expected exactly 1 temp dir created; got {created_tmp_dirs}"
        )
        tmp_dir_path = Path(created_tmp_dirs[0])
        assert not tmp_dir_path.exists(), (
            f"Temp central dir {tmp_dir_path} still exists after --test-apply exit 0; "
            "must be auto-cleaned"
        )

    def test_temp_dir_cleaned_on_failure(self, tmp_path, monkeypatch):
        """AC6: the temp central dir is removed even when --test-apply exits non-zero."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        created_tmp_dirs: list[str] = []
        original_mkdtemp = __import__("tempfile").mkdtemp

        def tracking_mkdtemp(prefix="", suffix="", dir=None):
            d = original_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
            if "vnx-test-apply-" in prefix:
                created_tmp_dirs.append(d)
            return d

        import tempfile

        def _failing_init(qi_db, rc_db):
            raise M.BootstrapFailure("injected failure for cleanup test")

        with patch.object(tempfile, "mkdtemp", side_effect=tracking_mkdtemp):
            with patch.object(M, "_init_central_if_missing", side_effect=_failing_init):
                rc = M.main([
                    "--test-apply",
                    "--registry", str(registry),
                    "--backup-base", str(backup_base),
                ])

        assert rc == 3, f"Expected exit 3; got {rc}"
        assert len(created_tmp_dirs) == 1
        tmp_dir_path = Path(created_tmp_dirs[0])
        assert not tmp_dir_path.exists(), (
            f"Temp central dir {tmp_dir_path} still exists after --test-apply exit 3; "
            "must be auto-cleaned regardless of exit code"
        )

    def test_no_backup_dirs_created_during_test_apply(self, tmp_path, monkeypatch):
        """AC6: --test-apply does NOT create vnx-pre-p4-auto-backup-* dirs in backup_base."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        _run_test_apply(tmp_path, registry, backup_base)

        backup_dirs = list(backup_base.glob("vnx-pre-p4-auto-backup-*"))
        assert backup_dirs == [], (
            f"--test-apply must NOT create backup dirs; found: {backup_dirs}"
        )

    def test_test_apply_prefix_used_for_temp_dir(self, tmp_path, monkeypatch):
        """AC6: the temp dir is created with prefix 'vnx-test-apply-' for identifiability."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-a"])

        captured_prefixes: list[str] = []
        original_mkdtemp = __import__("tempfile").mkdtemp

        def capturing_mkdtemp(prefix="", suffix="", dir=None):
            captured_prefixes.append(prefix)
            return original_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)

        import tempfile
        with patch.object(tempfile, "mkdtemp", side_effect=capturing_mkdtemp):
            rc = _run_test_apply(tmp_path, registry, backup_base)

        assert rc == 0
        # At least one mkdtemp call must have used the vnx-test-apply- prefix.
        assert any("vnx-test-apply-" in p for p in captured_prefixes), (
            f"Expected a mkdtemp call with prefix 'vnx-test-apply-'; "
            f"captured prefixes: {captured_prefixes}"
        )

    def test_successful_test_apply_imports_data_into_temp_central(
        self, tmp_path, monkeypatch
    ):
        """AC6: during the run, data IS imported into temp central (proves chain ran)."""
        monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
        registry, backup_base = _build_fixture(tmp_path, ["proj-x"], rows_per_project=4)

        # Track the temp dir path during execution by capturing mkdtemp call.
        created_dirs: list[str] = []
        original_mkdtemp = __import__("tempfile").mkdtemp

        def tracking_mkdtemp(prefix="", suffix="", dir=None):
            d = original_mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
            if "vnx-test-apply-" in prefix:
                created_dirs.append(d)
            return d

        # We need to snapshot the central QI DB BEFORE cleanup.
        # Hook into shutil.rmtree to capture the state before deletion.
        import shutil
        original_rmtree = shutil.rmtree
        pre_cleanup_row_count: list[int] = []

        def capturing_rmtree(path, ignore_errors=False, onerror=None):
            if created_dirs and str(path) == created_dirs[0]:
                # Capture the DB state before it's deleted.
                qi_db = Path(path) / "quality_intelligence.db"
                if qi_db.exists():
                    try:
                        con = sqlite3.connect(str(qi_db))
                        try:
                            rows = con.execute(
                                "SELECT COUNT(*) FROM dispatch_metadata "
                                "WHERE project_id = 'proj-x'"
                            ).fetchone()
                            pre_cleanup_row_count.append(rows[0] if rows else 0)
                        finally:
                            con.close()
                    except sqlite3.Error:
                        pass
            original_rmtree(path, ignore_errors=ignore_errors)

        import tempfile
        with patch.object(tempfile, "mkdtemp", side_effect=tracking_mkdtemp):
            with patch.object(shutil, "rmtree", side_effect=capturing_rmtree):
                rc = _run_test_apply(tmp_path, registry, backup_base)

        assert rc == 0
        assert len(created_dirs) == 1
        assert len(pre_cleanup_row_count) == 1, (
            "Failed to capture row count from temp central DB before cleanup"
        )
        assert pre_cleanup_row_count[0] == 4, (
            f"Expected 4 rows for proj-x in temp central DB during test-apply; "
            f"got {pre_cleanup_row_count[0]}"
        )
        # Confirm the temp dir is gone post-cleanup.
        assert not Path(created_dirs[0]).exists(), (
            "Temp central dir still exists after test-apply completed"
        )
