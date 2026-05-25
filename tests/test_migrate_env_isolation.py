"""Regression tests for env isolation pre-flight (PR-WAVE2A-5).

Covers migrator WARN behaviour when VNX_DATA_DIR env var is set:

  - No warning logged when VNX_DATA_DIR is unset
  - No warning logged when VNX_DATA_DIR matches --central-state (same resolved path)
  - WARNING logged when VNX_DATA_DIR points to a different project path
  - WARNING message contains VNX_DATA_DIR value and --central-state value
  - WARNING message references check_env_isolation.sh
  - main() does NOT abort (still returns 0 or non-3) when env leak detected

Also covers check_env_isolation.sh exit codes:

  - Exit 0 when all VNX_* vars are unset
  - Exit 1 when VNX_DATA_DIR is an absolute path from a different project
  - Exit 0 when VNX_DATA_DIR matches the current project root
  - Unset command printed on leak detection
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402
from scripts.aggregator.build_central_view import ProjectEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHECK_SCRIPT = ROOT / "scripts" / "check_env_isolation.sh"


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


@pytest.fixture
def env_isolation_fixture(tmp_path, monkeypatch):
    """Minimal env for testing env pre-flight in main()."""
    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    abort_dir = tmp_path / ".vnx-aggregator"
    abort_dir.mkdir()
    monkeypatch.setattr(M, "ABORT_FLAG", abort_dir / "ABORT")

    central_state = tmp_path / "central" / "state"
    central_state.mkdir(parents=True)

    # Single project
    proj = tmp_path / "proj-alpha"
    state = proj / ".vnx-data" / "state"
    _make_qi_db(state / "quality_intelligence.db")
    _make_rc_db(state / "runtime_coordination.db")
    specs = [{"name": "proj-alpha", "path": str(proj), "project_id": "proj-alpha"}]

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    return {
        "tmp_path": tmp_path,
        "backup_base": backup_base,
        "central_state": central_state,
        "registry": registry,
    }


def _run_main_dry(env: dict, extra_env: dict | None = None) -> tuple[int, list[logging.LogRecord]]:
    """Run main() in dry-run mode; return (exit_code, log_records).

    Passes --central-state so the env-leak comparison is made against the
    fixture's central state dir, not the real default ~/.vnx-data/state.
    """
    records: list[logging.LogRecord] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
            records.append(record)

    handler = _CapturingHandler(level=logging.DEBUG)
    logger = logging.getLogger("vnx.migrate.apply")
    logger.addHandler(handler)

    # Patch env for this call
    orig_env = {k: os.environ.get(k) for k in (extra_env or {})}
    try:
        if extra_env:
            for k, v in extra_env.items():
                os.environ[k] = v

        # Dry-run just calls migrate_dry_run — intercept at subprocess.call level
        import unittest.mock as mock
        with mock.patch("subprocess.call", return_value=0):
            rc = M.main([
                "--registry", str(env["registry"]),
                "--central-state", str(env["central_state"]),
                # No --apply → dry-run mode; env check runs before subprocess.call
            ])
    finally:
        logger.removeHandler(handler)
        for k, orig in orig_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig

    return rc, records


# ---------------------------------------------------------------------------
# Unit tests: env pre-flight WARN logic in main()
# ---------------------------------------------------------------------------


class TestMigratorEnvPreFlight:
    def test_no_warn_when_env_var_unset(self, env_isolation_fixture, monkeypatch):
        """No WARNING logged when VNX_DATA_DIR is not set."""
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        env = env_isolation_fixture

        _, records = _run_main_dry(env)
        warn_records = [r for r in records if r.levelno >= logging.WARNING and "env leak" in r.getMessage().lower()]
        assert warn_records == [], f"Expected no env-leak warning. Got: {[r.getMessage() for r in warn_records]}"

    def test_no_warn_when_env_matches_central_state(self, env_isolation_fixture, monkeypatch):
        """No WARNING when VNX_DATA_DIR resolves to the same path as --central-state."""
        env = env_isolation_fixture
        # Set VNX_DATA_DIR to the same central_state path the migrator will use.
        monkeypatch.setenv("VNX_DATA_DIR", str(env["central_state"]))

        _, records = _run_main_dry(env)
        warn_records = [r for r in records if r.levelno >= logging.WARNING and "env leak" in r.getMessage().lower()]
        assert warn_records == [], f"Expected no env-leak warning when paths match. Got: {[r.getMessage() for r in warn_records]}"

    def test_warn_logged_on_mismatch(self, env_isolation_fixture, monkeypatch, tmp_path):
        """WARNING logged when VNX_DATA_DIR points to a different path than --central-state."""
        env = env_isolation_fixture
        foreign_path = tmp_path / "other-project" / ".vnx-data"
        foreign_path.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(foreign_path))

        _, records = _run_main_dry(env)
        warn_records = [r for r in records if r.levelno >= logging.WARNING and "env leak" in r.getMessage().lower()]
        assert warn_records, "Expected at least one env-leak WARNING log record."

    def test_warn_message_contains_env_value(self, env_isolation_fixture, monkeypatch, tmp_path):
        """WARNING message includes the value of VNX_DATA_DIR."""
        env = env_isolation_fixture
        foreign_path = tmp_path / "seocrawler-v2" / ".vnx-data"
        foreign_path.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(foreign_path))

        _, records = _run_main_dry(env)
        warn_records = [r for r in records if r.levelno >= logging.WARNING and "env leak" in r.getMessage().lower()]
        assert warn_records, "Expected env-leak WARNING."
        msg = warn_records[0].getMessage()
        assert str(foreign_path) in msg, f"Expected VNX_DATA_DIR value in message. Got: {msg!r}"

    def test_warn_message_references_check_script(self, env_isolation_fixture, monkeypatch, tmp_path):
        """WARNING message references check_env_isolation.sh."""
        env = env_isolation_fixture
        foreign_path = tmp_path / "other-project" / ".vnx-data"
        foreign_path.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(foreign_path))

        _, records = _run_main_dry(env)
        warn_records = [r for r in records if r.levelno >= logging.WARNING and "env leak" in r.getMessage().lower()]
        assert warn_records, "Expected env-leak WARNING."
        msg = warn_records[0].getMessage()
        assert "check_env_isolation.sh" in msg, f"Expected check_env_isolation.sh reference. Got: {msg!r}"

    def test_no_abort_on_env_mismatch(self, env_isolation_fixture, monkeypatch, tmp_path):
        """main() must NOT return exit code 3 (abort) due to env mismatch — WARN only."""
        env = env_isolation_fixture
        foreign_path = tmp_path / "other-project" / ".vnx-data"
        foreign_path.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(foreign_path))

        import unittest.mock as mock
        with mock.patch("subprocess.call", return_value=0):
            rc = M.main(["--registry", str(env["registry"])])

        # Env mismatch is WARN only — must not exit 3
        assert rc != 3, f"main() returned 3 on env mismatch; env leak should only WARN, not abort."


# ---------------------------------------------------------------------------
# check_env_isolation.sh — exit code tests
# ---------------------------------------------------------------------------


def _run_check_script(env_override: dict | None = None) -> tuple[int, str]:
    """Run check_env_isolation.sh with a clean environment + optional overrides.

    Strips all VNX_* vars from the base env, then applies env_override on top.
    Returns (exit_code, stdout).
    """
    # Start with a clean base: strip all VNX_* vars to avoid cross-test contamination.
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    if env_override:
        clean_env.update(env_override)

    result = subprocess.run(
        ["bash", str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=str(ROOT),
    )
    return result.returncode, result.stdout + result.stderr


class TestCheckEnvIsolationScript:
    def test_exit_0_when_all_vars_unset(self):
        """Exit 0 when no VNX_* vars are set — clean env."""
        rc, output = _run_check_script()
        assert rc == 0, f"Expected exit 0 for clean env. Got {rc}. Output: {output!r}"

    def test_exit_1_when_foreign_vnx_data_dir(self, tmp_path):
        """Exit 1 when VNX_DATA_DIR is an absolute path from a different project."""
        foreign_dir = tmp_path / "seocrawler-v2" / ".vnx-data"
        foreign_dir.mkdir(parents=True)
        rc, output = _run_check_script({"VNX_DATA_DIR": str(foreign_dir)})
        assert rc == 1, f"Expected exit 1 for foreign VNX_DATA_DIR. Got {rc}. Output: {output!r}"

    def test_exit_1_when_foreign_vnx_home(self, tmp_path):
        """Exit 1 when VNX_HOME is an absolute path from a different project."""
        foreign_home = tmp_path / "other-project" / ".vnx"
        foreign_home.mkdir(parents=True)
        rc, output = _run_check_script({"VNX_HOME": str(foreign_home)})
        assert rc == 1, f"Expected exit 1 for foreign VNX_HOME. Got {rc}. Output: {output!r}"

    def test_exit_0_when_matching_project_path(self):
        """Exit 0 when VNX_DATA_DIR points to the current project root's .vnx-data."""
        matching_dir = str(ROOT / ".vnx-data")
        rc, output = _run_check_script({"VNX_DATA_DIR": matching_dir})
        assert rc == 0, f"Expected exit 0 when VNX_DATA_DIR matches project root. Got {rc}. Output: {output!r}"

    def test_exit_0_for_relative_path_vars(self):
        """Relative path values (e.g. .vnx-data/state) are treated as project-local — no leak."""
        rc, output = _run_check_script({
            "VNX_DATA_DIR": ".vnx-data",
            "VNX_STATE_DIR": ".vnx-data/state",
        })
        assert rc == 0, f"Expected exit 0 for relative path vars. Got {rc}. Output: {output!r}"

    def test_unset_command_in_output_on_leak(self, tmp_path):
        """Output contains 'unset' command when leakage is detected."""
        foreign_dir = tmp_path / "other-project" / ".vnx-data"
        foreign_dir.mkdir(parents=True)
        rc, output = _run_check_script({"VNX_DATA_DIR": str(foreign_dir)})
        assert rc == 1
        assert "unset" in output.lower(), f"Expected 'unset' command in output. Got: {output!r}"

    def test_leaked_var_named_in_unset_output(self, tmp_path):
        """The specific leaked var name appears in the unset command."""
        foreign_dir = tmp_path / "different-project" / ".vnx-data"
        foreign_dir.mkdir(parents=True)
        rc, output = _run_check_script({"VNX_DATA_DIR": str(foreign_dir)})
        assert rc == 1
        assert "VNX_DATA_DIR" in output, f"Expected VNX_DATA_DIR in unset output. Got: {output!r}"

    def test_script_is_executable(self):
        """check_env_isolation.sh must have the executable bit set."""
        mode = CHECK_SCRIPT.stat().st_mode
        assert mode & 0o111, f"Expected executable bit on {CHECK_SCRIPT}. Mode: {oct(mode)}"
