"""Regression tests for TCC pre-flight check (PR-WAVE2A-1).

Covers:
  - _check_backup_access() returns empty list when all dirs are accessible
  - _check_backup_access() returns (project_id, path, error_msg) tuples for
    PermissionError-inaccessible directories
  - _check_backup_access() returns tuples for OSError-inaccessible directories
  - _check_backup_access() skips projects whose .vnx-data dir does not exist
  - main() returns exit code 3 when any project is inaccessible via TCC
  - main() error message contains 'Full Disk Access' for operator guidance
  - main() does NOT proceed to backup when pre-flight fails
"""

from __future__ import annotations

import json
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
from scripts.aggregator.build_central_view import ProjectEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_qi_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_rc_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT
            );
            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT INTO runtime_schema_version (version, description) VALUES (10, 'phase-0');
            """
        )
        con.commit()
    finally:
        con.close()


def _project_entry(tmp_path: Path, name: str, pid: str) -> ProjectEntry:
    """Create a ProjectEntry with a real .vnx-data dir on disk."""
    proj = tmp_path / name
    state = proj / ".vnx-data" / "state"
    _make_qi_db(state / "quality_intelligence.db")
    _make_rc_db(state / "runtime_coordination.db")
    return ProjectEntry(name=name, path=proj, project_id=pid)


# ---------------------------------------------------------------------------
# Unit tests: _check_backup_access()
# ---------------------------------------------------------------------------


class TestCheckBackupAccess:
    def test_returns_empty_when_all_accessible(self, tmp_path):
        """All .vnx-data dirs exist and are readable — no failures."""
        p1 = _project_entry(tmp_path, "proj-a", "proj-a")
        p2 = _project_entry(tmp_path, "proj-b", "proj-b")
        result = M._check_backup_access([p1, p2])
        assert result == []

    def test_returns_tuple_for_permission_error(self, tmp_path):
        """PermissionError on os.listdir produces a (pid, path, err) tuple."""
        p1 = _project_entry(tmp_path, "proj-a", "proj-a")
        p2 = _project_entry(tmp_path, "proj-b", "proj-b")

        def fake_listdir(path):
            if "proj-a" in str(path):
                raise PermissionError(1, "Operation not permitted", str(path))
            return os.listdir.__wrapped__(path) if hasattr(os.listdir, "__wrapped__") else []

        with patch("os.listdir", side_effect=fake_listdir):
            result = M._check_backup_access([p1, p2])

        assert len(result) == 1
        pid, path, err_msg = result[0]
        assert pid == "proj-a"
        assert "proj-a" in path
        assert "Operation not permitted" in err_msg or err_msg  # non-empty

    def test_returns_tuple_for_os_error(self, tmp_path):
        """OSError (non-permission) on os.listdir also produces a failure tuple."""
        p1 = _project_entry(tmp_path, "proj-a", "proj-a")

        with patch("os.listdir", side_effect=OSError(5, "Input/output error", str(p1.path / ".vnx-data"))):
            result = M._check_backup_access([p1])

        assert len(result) == 1
        pid, path, err_msg = result[0]
        assert pid == "proj-a"
        assert err_msg  # non-empty error message

    def test_skips_missing_vnx_data_dir(self, tmp_path):
        """Projects without a .vnx-data dir are skipped (BackupFailure handles them)."""
        proj = tmp_path / "no-data-proj"
        proj.mkdir()
        # No .vnx-data subdirectory created
        entry = ProjectEntry(name="no-data-proj", path=proj, project_id="no-data")
        result = M._check_backup_access([entry])
        assert result == []

    def test_collects_multiple_failures(self, tmp_path):
        """Multiple inaccessible projects each produce a tuple."""
        p1 = _project_entry(tmp_path, "proj-a", "proj-a")
        p2 = _project_entry(tmp_path, "proj-b", "proj-b")
        p3 = _project_entry(tmp_path, "proj-c", "proj-c")

        with patch("os.listdir", side_effect=PermissionError(1, "Operation not permitted", ".")):
            result = M._check_backup_access([p1, p2, p3])

        assert len(result) == 3
        pids = {r[0] for r in result}
        assert pids == {"proj-a", "proj-b", "proj-c"}

    def test_only_inaccessible_projects_returned(self, tmp_path):
        """Accessible projects are excluded; only failing ones appear."""
        p1 = _project_entry(tmp_path, "proj-ok", "proj-ok")
        p2 = _project_entry(tmp_path, "proj-bad", "proj-bad")

        original_listdir = os.listdir

        def selective_listdir(path):
            if "proj-bad" in str(path):
                raise PermissionError(1, "Operation not permitted", str(path))
            return original_listdir(path)

        with patch("os.listdir", side_effect=selective_listdir):
            result = M._check_backup_access([p1, p2])

        assert len(result) == 1
        assert result[0][0] == "proj-bad"


# ---------------------------------------------------------------------------
# Integration tests: main() TCC pre-flight behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def tcc_fixture_env(tmp_path, monkeypatch):
    """Minimal env for testing TCC pre-flight in main()."""
    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    abort_dir = tmp_path / ".vnx-aggregator"
    abort_dir.mkdir()
    monkeypatch.setattr(M, "ABORT_FLAG", abort_dir / "ABORT")

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True)

    # Build 2-project registry: one accessible, one will be mocked inaccessible
    specs = []
    for name, pid in [("proj-ok", "proj-ok"), ("proj-tcc", "proj-tcc")]:
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        _make_qi_db(state / "quality_intelligence.db")
        _make_rc_db(state / "runtime_coordination.db")
        specs.append({"name": name, "path": str(proj), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    return {
        "tmp_path": tmp_path,
        "backup_base": backup_base,
        "central_state": central_state,
        "registry": registry,
        "abort_flag": abort_dir / "ABORT",
    }


def _apply_tcc(env: dict, extra_args: list[str] | None = None) -> tuple[int, str]:
    """Run main() in apply mode; return (exit_code, captured_stderr)."""
    import io
    import logging

    # Capture log output via handler
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.DEBUG)
    root_logger = logging.getLogger("vnx.migrate.apply")
    root_logger.addHandler(handler)

    cmd = [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(env["registry"]),
        "--backup-base", str(env["backup_base"]),
        "--central-state", str(env["central_state"]),
    ]
    if extra_args:
        cmd.extend(extra_args)

    try:
        rc = M.main(cmd)
    finally:
        root_logger.removeHandler(handler)

    return rc, log_stream.getvalue()


class TestMainTccPreFlight:
    def test_main_exits_3_on_permission_error(self, tcc_fixture_env, caplog):
        """main() returns exit code 3 when os.listdir raises PermissionError for a project."""
        env = tcc_fixture_env
        tcc_proj_path = env["tmp_path"] / "proj-tcc" / ".vnx-data"
        original_listdir = os.listdir

        def selective_listdir(path):
            if str(tcc_proj_path) in str(path):
                raise PermissionError(1, "Operation not permitted", str(path))
            return original_listdir(path)

        import logging
        with caplog.at_level(logging.ERROR, logger="vnx.migrate.apply"):
            with patch("os.listdir", side_effect=selective_listdir):
                rc = M.main([
                    "--apply",
                    "--confirm", M.CONFIRMATION_PHRASE,
                    "--no-prompt",
                    "--registry", str(env["registry"]),
                    "--backup-base", str(env["backup_base"]),
                    "--central-state", str(env["central_state"]),
                ])

        assert rc == 3

    def test_main_error_contains_full_disk_access(self, tcc_fixture_env, caplog):
        """Error message contains 'Full Disk Access' for operator actionability."""
        env = tcc_fixture_env
        tcc_proj_path = env["tmp_path"] / "proj-tcc" / ".vnx-data"
        original_listdir = os.listdir

        def selective_listdir(path):
            if str(tcc_proj_path) in str(path):
                raise PermissionError(1, "Operation not permitted", str(path))
            return original_listdir(path)

        import logging
        with caplog.at_level(logging.ERROR, logger="vnx.migrate.apply"):
            with patch("os.listdir", side_effect=selective_listdir):
                M.main([
                    "--apply",
                    "--confirm", M.CONFIRMATION_PHRASE,
                    "--no-prompt",
                    "--registry", str(env["registry"]),
                    "--backup-base", str(env["backup_base"]),
                    "--central-state", str(env["central_state"]),
                ])

        # The error log must contain the actionable TCC guidance
        error_logs = " ".join(r.message for r in caplog.records if r.levelname == "ERROR")
        assert "Full Disk Access" in error_logs, (
            f"Expected 'Full Disk Access' in error output. Got: {error_logs!r}"
        )

    def test_main_no_backup_created_on_tcc_failure(self, tcc_fixture_env):
        """No backup directory is created when the TCC pre-flight fails."""
        env = tcc_fixture_env
        backup_base = env["backup_base"]

        with patch("os.listdir", side_effect=PermissionError(1, "Operation not permitted", ".")):
            M.main([
                "--apply",
                "--confirm", M.CONFIRMATION_PHRASE,
                "--no-prompt",
                "--registry", str(env["registry"]),
                "--backup-base", str(backup_base),
                "--central-state", str(env["central_state"]),
            ])

        backup_dirs = list(backup_base.glob("vnx-pre-p4-auto-backup-*"))
        assert backup_dirs == [], (
            f"Expected no backup dirs, but found: {backup_dirs}"
        )

    def test_main_proceeds_normally_when_all_accessible(self, tcc_fixture_env):
        """main() does NOT return 3 when all .vnx-data dirs are accessible."""
        env = tcc_fixture_env
        # No mock — real os.listdir, real filesystem access
        rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(env["registry"]),
            "--backup-base", str(env["backup_base"]),
            "--central-state", str(env["central_state"]),
        ])
        # Should not be exit code 3 (TCC failure). May be 0 or other codes
        # depending on schema availability in test env — but not 3.
        assert rc != 3, f"main() returned 3 unexpectedly (TCC pre-flight false positive)"
