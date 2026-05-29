"""Tests for scripts/dream/scheduler.py (ADR-019 auto-dream scheduler v2).

Coverage:
- install_scheduler (macOS): plist written, launchctl called
- install_scheduler (Linux): crontab line added idempotently
- uninstall_scheduler (macOS): plist removed, launchctl unload called
- uninstall_scheduler (Linux): crontab line removed
- unsupported platform raises RuntimeError
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))

import scheduler


# ---------------------------------------------------------------------------
# macOS tests
# ---------------------------------------------------------------------------


class TestInstallSchedulerMacOS:
    def test_plist_written_and_launchctl_called(self, tmp_path):
        """install_scheduler on Darwin writes plist + calls launchctl load -w."""
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)

        completed_ok = MagicMock()
        completed_ok.returncode = 0
        completed_ok.stderr = ""
        completed_ok.stdout = ""

        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", return_value=completed_ok) as mock_run,
            patch("scheduler.resolve_project_root", return_value=tmp_path),
        ):
            result = scheduler.install_scheduler(
                project_id="vnx-dev",
                project_root=tmp_path,
                vnx_bin="/usr/local/bin/vnx",
            )

        plist_path = launch_agents / "com.vnx.auto-dream.plist"
        assert plist_path.exists(), "plist not written"
        content = plist_path.read_text()
        assert "vnx-dev" in content
        assert "/usr/local/bin/vnx" in content

        # launchctl unload + load should have been called
        calls_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("unload" in cmd for cmd in calls_cmds)
        assert any("load" in cmd for cmd in calls_cmds)
        assert "Installed and loaded" in result

    def test_install_idempotent(self, tmp_path):
        """Second install overwrites plist + unloads before re-loading."""
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)

        ok = MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", return_value=ok),
            patch("scheduler.resolve_project_root", return_value=tmp_path),
        ):
            scheduler.install_scheduler("vnx-dev", project_root=tmp_path, vnx_bin="vnx")
            scheduler.install_scheduler("vnx-dev", project_root=tmp_path, vnx_bin="vnx")

        plist_path = launch_agents / "com.vnx.auto-dream.plist"
        assert plist_path.exists()

    def test_launchctl_failure_raises(self, tmp_path):
        """RuntimeError raised when launchctl load returns non-zero."""
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            if "load" in cmd and "unload" not in cmd:
                m.returncode = 1
                m.stderr = "service already loaded"
                m.stdout = ""
            else:
                m.returncode = 0
                m.stderr = ""
                m.stdout = ""
            return m

        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", side_effect=mock_run),
            patch("scheduler.resolve_project_root", return_value=tmp_path),
        ):
            with pytest.raises(RuntimeError, match="launchctl load failed"):
                scheduler.install_scheduler("vnx-dev", project_root=tmp_path, vnx_bin="vnx")


class TestUninstallSchedulerMacOS:
    def test_uninstall_removes_plist(self, tmp_path):
        """uninstall_scheduler on Darwin unloads + removes plist."""
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        plist_path = launch_agents / "com.vnx.auto-dream.plist"
        plist_path.write_text("<plist/>", encoding="utf-8")

        ok = MagicMock(returncode=0)

        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
            patch("scheduler.subprocess.run", return_value=ok),
        ):
            result = scheduler.uninstall_scheduler()

        assert not plist_path.exists()
        assert "Unloaded and removed" in result

    def test_uninstall_when_not_installed(self, tmp_path):
        """uninstall_scheduler returns gracefully when plist absent."""
        with (
            patch("scheduler.platform.system", return_value="Darwin"),
            patch("scheduler.Path.home", return_value=tmp_path),
        ):
            result = scheduler.uninstall_scheduler()

        assert "Not installed" in result


# ---------------------------------------------------------------------------
# Linux tests
# ---------------------------------------------------------------------------


class TestInstallSchedulerLinux:
    def test_cron_entry_added(self):
        """install_scheduler on Linux adds crontab entry with project_id."""
        existing_cron = "30 6 * * * /usr/bin/backup\n"

        captured = {}

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if cmd == ["crontab", "-l"]:
                m.stdout = existing_cron
            elif cmd == ["crontab", "-"]:
                captured["new"] = kwargs.get("input", "")
            return m

        with (
            patch("scheduler.platform.system", return_value="Linux"),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            result = scheduler.install_scheduler("vnx-dev", vnx_bin="/usr/bin/vnx")

        assert "vnx-dev" in captured["new"]
        assert "vnx-auto-dream" in captured["new"]
        assert "# vnx-auto-dream" in result or "Cron entry" in result

    def test_cron_idempotent(self):
        """Re-installing replaces existing auto-dream cron line (no duplicates)."""
        existing = "0 3 * * * /usr/bin/vnx dream run --project-id vnx-dev  # vnx-auto-dream:com.vnx.auto-dream\n"
        captured = {}

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if cmd == ["crontab", "-l"]:
                m.stdout = existing
            elif cmd == ["crontab", "-"]:
                captured["new"] = kwargs.get("input", "")
            return m

        with (
            patch("scheduler.platform.system", return_value="Linux"),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            scheduler.install_scheduler("vnx-dev", vnx_bin="/usr/bin/vnx")

        lines = [l for l in captured["new"].splitlines() if "vnx-auto-dream" in l]
        assert len(lines) == 1, "Expected exactly one auto-dream cron line"


class TestUninstallSchedulerLinux:
    def test_cron_entry_removed(self):
        """uninstall_scheduler on Linux removes the auto-dream cron line."""
        existing = (
            "30 6 * * * /usr/bin/backup\n"
            "0 3 * * * /usr/bin/vnx dream run --project-id vnx-dev  # vnx-auto-dream:com.vnx.auto-dream\n"
        )
        captured = {}

        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if cmd == ["crontab", "-l"]:
                m.stdout = existing
            elif cmd == ["crontab", "-"]:
                captured["new"] = kwargs.get("input", "")
            return m

        with (
            patch("scheduler.platform.system", return_value="Linux"),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            result = scheduler.uninstall_scheduler()

        assert "vnx-auto-dream" not in captured.get("new", "")
        assert "removed" in result.lower()

    def test_uninstall_when_not_present(self):
        """uninstall_scheduler returns gracefully when no auto-dream line exists."""
        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "30 6 * * * /usr/bin/backup\n"
            return m

        with (
            patch("scheduler.platform.system", return_value="Linux"),
            patch("scheduler.subprocess.run", side_effect=mock_run),
        ):
            result = scheduler.uninstall_scheduler()

        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Unsupported platform
# ---------------------------------------------------------------------------


class TestUnsupportedPlatform:
    def test_install_raises_on_windows(self):
        with patch("scheduler.platform.system", return_value="Windows"):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                scheduler.install_scheduler("vnx-dev")

    def test_uninstall_raises_on_windows(self):
        with patch("scheduler.platform.system", return_value="Windows"):
            with pytest.raises(RuntimeError, match="Unsupported platform"):
                scheduler.uninstall_scheduler()
