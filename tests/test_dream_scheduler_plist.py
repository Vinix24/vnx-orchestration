"""Rendered-output tests for the auto-dream scheduler installers (OI-895).

These tests assert on the *rendered* plist/crontab, not on intent: a fresh
``vnx dream install-scheduler`` must produce a job that can actually run under
launchd/cron:

- Gebrek 1: EnvironmentVariables with VNX_DREAM_SCHEDULER_ENABLED=1 + usable PATH
- Gebrek 2: WorkingDirectory + VNX_CANONICAL_ROOT (launchd/cron run outside any
  project; without them root resolution fails with "not in a VNX project")
- Gebrek 3: StandardOutPath/StandardErrorPath under the *central* data dir
  (resolve_central_data_dir, ADR-026 SSOT), never repo-local .vnx-data — and the
  log directory must exist before launchd tries to write to it.

The Linux/cron path has the same gaps measured the same way: no feature-flag in
the environment, cwd=$HOME outside any project, and no log redirection at all.
"""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))

import scheduler


PROJECT_ID = "vnx-dev"
VNX_BIN = "/usr/local/bin/vnx"


def _install_macos(tmp_path: Path) -> tuple[Path, Path, list]:
    """Run install_scheduler on a mocked Darwin; return (plist, log_dir, run_calls)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    central = tmp_path / ".vnx-data" / PROJECT_ID
    log_dir = central / "events" / "dream"

    ok = MagicMock(returncode=0, stderr="", stdout="")

    with (
        patch("scheduler.platform.system", return_value="Darwin"),
        patch("scheduler.Path.home", return_value=tmp_path),
        patch("scheduler.subprocess.run", return_value=ok) as mock_run,
    ):
        scheduler.install_scheduler(
            project_id=PROJECT_ID,
            project_root=project_root,
            vnx_bin=VNX_BIN,
        )

    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.vnx.auto-dream.plist"
    return plist_path, log_dir, mock_run.call_args_list


class TestRenderedPlistMacOS:
    """Assertions on the plist bytes actually written to ~/Library/LaunchAgents."""

    def test_plist_is_valid_xml(self, tmp_path):
        plist_path, _, _ = _install_macos(tmp_path)
        plist = plistlib.loads(plist_path.read_bytes())
        assert plist["Label"] == "com.vnx.auto-dream"
        assert plist["ProgramArguments"] == [
            VNX_BIN, "dream", "run", "--project-id", PROJECT_ID,
        ]

    def test_feature_flag_and_path_in_environment(self, tmp_path):
        """Gebrek 1: launchd gets VNX_DREAM_SCHEDULER_ENABLED=1 + usable PATH."""
        plist_path, _, _ = _install_macos(tmp_path)
        plist = plistlib.loads(plist_path.read_bytes())
        env = plist.get("EnvironmentVariables", {})
        assert env.get("VNX_DREAM_SCHEDULER_ENABLED") == "1"
        assert env.get("PATH"), "PATH missing from EnvironmentVariables"

    def test_working_directory_and_canonical_root(self, tmp_path):
        """Gebrek 2: job runs inside the project even though launchd starts in /."""
        project_root = tmp_path / "project"
        plist_path, _, _ = _install_macos(tmp_path)
        plist = plistlib.loads(plist_path.read_bytes())
        assert plist.get("WorkingDirectory") == str(project_root)
        env = plist.get("EnvironmentVariables", {})
        assert env.get("VNX_CANONICAL_ROOT") == str(project_root)

    def test_log_paths_under_central_data_dir(self, tmp_path):
        """Gebrek 3: logs go to resolve_central_data_dir(project_id), ADR-026."""
        project_root = tmp_path / "project"
        plist_path, log_dir, _ = _install_macos(tmp_path)
        plist = plistlib.loads(plist_path.read_bytes())
        assert plist.get("StandardOutPath") == str(log_dir / "launchd.out.log")
        assert plist.get("StandardErrorPath") == str(log_dir / "launchd.err.log")
        repo_local = str(project_root / ".vnx-data")
        assert not plist["StandardOutPath"].startswith(repo_local)
        assert not plist["StandardErrorPath"].startswith(repo_local)

    def test_log_dir_created_before_launchctl_load(self, tmp_path):
        """The log dir must exist before launchd can write to it."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        central = tmp_path / ".vnx-data" / PROJECT_ID
        log_dir = central / "events" / "dream"

        def mock_run(cmd, **kwargs):
            m = MagicMock(returncode=0, stderr="", stdout="")
            if "load" in cmd and "unload" not in cmd:
                assert log_dir.is_dir(), (
                    "log dir must be created before `launchctl load`"
                )
            return m

        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            scheduler.install_scheduler(
                project_id=PROJECT_ID, project_root=project_root, vnx_bin=VNX_BIN
            )

        assert log_dir.is_dir()


class TestRenderedCronLinux:
    """The cron path has the same gaps: no flag, no project root, no logs."""

    def _install_linux(self, tmp_path: Path) -> tuple[str, Path]:
        project_root = tmp_path / "project"
        project_root.mkdir()
        log_dir = tmp_path / ".vnx-data" / PROJECT_ID / "events" / "dream"
        captured: dict[str, str] = {}

        def mock_run(cmd, **kwargs):
            m = MagicMock(returncode=0, stderr="", stdout="")
            if cmd == ["crontab", "-l"]:
                m.stdout = "30 6 * * * /usr/bin/backup\n"
            elif cmd == ["crontab", "-"]:
                captured["new"] = kwargs.get("input", "")
            return m

        with (
            patch("scheduler.platform.system", return_value="Linux"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            scheduler.install_scheduler(
                project_id=PROJECT_ID, project_root=project_root, vnx_bin=VNX_BIN
            )

        lines = [ln for ln in captured["new"].splitlines() if "vnx-auto-dream" in ln]
        assert len(lines) == 1, "expected exactly one auto-dream cron line"
        return lines[0], log_dir

    def test_cron_line_sets_feature_flag(self, tmp_path):
        line, _ = self._install_linux(tmp_path)
        assert "VNX_DREAM_SCHEDULER_ENABLED=1" in line

    def test_cron_line_runs_inside_project(self, tmp_path):
        project_root = tmp_path / "project"
        line, _ = self._install_linux(tmp_path)
        assert f"VNX_CANONICAL_ROOT={project_root}" in line
        assert f"cd {project_root}" in line

    def test_cron_line_logs_to_central_data_dir(self, tmp_path):
        line, log_dir = self._install_linux(tmp_path)
        assert str(log_dir / "cron.out.log") in line
        assert "2>&1" in line
        assert log_dir.is_dir(), "log dir must be created at install time"
