"""tests/test_launchd_dashboard_fleet_drift_drivers.py — absence-is-loud, punt 3.

Two producers worked and had no driver that survives a restart:

  - ``generate_valid_dashboard.sh`` writes ``dashboard_status.json`` (88 days old
    on 2026-09-23). It only ever ran as a nohup child of ``vnx start`` or of the
    tmux supervisor.
  - ``fleet_role_drift.py --write-state`` writes a beacon with
    ``expected_interval_seconds=86400``. Nothing invoked it, so the beacon did
    not exist.

``scripts/launchd/com.vnx.dashboard-generator.plist`` and
``com.vnx.fleet-role-drift.plist`` are the drivers. These tests pin what makes a
driver a driver and not a file that exists: the cadence is provably inside the
beacon window (read from the tool's own call site, never copied as a literal),
the templates carry no store path, the real ``reload_plist.sh`` installs them,
and the dashboard job cannot double-start next to ``vnx start`` because the
script's own singleton lock says no (run for real, not asserted from a comment).
"""
from __future__ import annotations

import ast
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO / "scripts" / "launchd"
DASHBOARD_TEMPLATE = LAUNCHD_DIR / "com.vnx.dashboard-generator.plist"
FLEET_TEMPLATE = LAUNCHD_DIR / "com.vnx.fleet-role-drift.plist"
DASHBOARD_SCRIPT = REPO / "scripts" / "generate_valid_dashboard.sh"
FLEET_SCRIPT = REPO / "scripts" / "fleet_role_drift.py"
RELOAD = LAUNCHD_DIR / "reload_plist.sh"

sys.path.insert(0, str(LAUNCHD_DIR))
import launchd_project_scope as lps  # noqa: E402


def _load(path: Path) -> Dict[str, Any]:
    with path.open("rb") as fh:
        return plistlib.load(fh)


def _raw(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _beacon_interval_declared_by(script: Path) -> int:
    """The ``expected_interval_seconds`` the tool passes to its HealthBeacon,
    read from the call site itself."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HealthBeacon":
            for kw in node.keywords:
                if kw.arg == "expected_interval_seconds" and isinstance(kw.value, ast.Constant):
                    return int(kw.value.value)
    raise AssertionError(f"no HealthBeacon(..., expected_interval_seconds=<literal>) call in {script}")


class TestTemplatesAreWellFormed:
    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_parses_as_a_plist(self, template: Path) -> None:
        data = _load(template)
        assert isinstance(data["Label"], str) and data["Label"]
        assert data["ProgramArguments"][:2] == ["/bin/bash", "-c"]

    @pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_plutil_lint_passes(self, template: Path) -> None:
        result = subprocess.run(["plutil", "-lint", str(template)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_no_store_path_is_hardcoded(self, template: Path) -> None:
        """A copied template must run against the project it was installed for."""
        raw = _raw(template)
        assert "/.vnx-data" not in raw
        assert "/Users/" not in raw

    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_project_and_home_come_from_placeholders(self, template: Path) -> None:
        env = _load(template)["EnvironmentVariables"]
        assert env["VNX_HOME"] == "${VNX_HOME}"
        assert env["VNX_PROJECT_ID"] == "${VNX_PROJECT_ID}"

    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_logs_go_to_tmp(self, template: Path) -> None:
        data = _load(template)
        assert data["StandardOutPath"].startswith("/tmp/")
        assert data["StandardErrorPath"].startswith("/tmp/")

    @pytest.mark.parametrize("template", [DASHBOARD_TEMPLATE, FLEET_TEMPLATE], ids=lambda p: p.name)
    def test_has_an_explanatory_comment_block(self, template: Path) -> None:
        raw = _raw(template)
        assert "<!--" in raw and "-->" in raw
        assert "absence-is-loud" in raw


class TestDashboardGeneratorTemplate:
    def test_label_is_project_scoped(self) -> None:
        """Two projects installing this must not collide on one launchd Label
        (OI-1509/OI-1510)."""
        result = lps.check_template_contract(LAUNCHD_DIR, families=("com.vnx.dashboard-generator",))
        assert result["ok"], result["violations"]

    def test_is_a_keepalive_job_not_an_interval_job(self) -> None:
        """The script is an endless loop that rewrites the artifact every 2s."""
        data = _load(DASHBOARD_TEMPLATE)
        assert data["RunAtLoad"] is True
        assert data["KeepAlive"] == {"SuccessfulExit": False}
        assert "StartInterval" not in data

    def test_runs_the_real_producer(self) -> None:
        command = _load(DASHBOARD_TEMPLATE)["ProgramArguments"][2]
        assert "scripts/generate_valid_dashboard.sh" in command
        assert DASHBOARD_SCRIPT.is_file()

    def test_comment_records_why_launchd_and_vnx_start_never_double_start(self) -> None:
        raw = _raw(DASHBOARD_TEMPLATE)
        assert "enforce_singleton" in raw
        assert "flock" in raw
        assert "vnx start" in raw


class TestFleetRoleDriftTemplate:
    def test_start_interval_is_inside_the_beacon_window(self) -> None:
        """A StartInterval at or above the beacon's expected interval makes the
        beacon stale by construction. Both numbers are read live."""
        interval = _load(FLEET_TEMPLATE)["StartInterval"]
        window = _beacon_interval_declared_by(FLEET_SCRIPT)
        assert interval < window, f"StartInterval={interval}s must be below expected_interval_seconds={window}s"

    def test_several_missed_runs_still_land_inside_the_window(self) -> None:
        """Same margin ledger-health argues for: 3 consecutive missed runs (an
        18h gap at a 6h interval) must not push the beacon past its window."""
        interval = _load(FLEET_TEMPLATE)["StartInterval"]
        assert 3 * interval < _beacon_interval_declared_by(FLEET_SCRIPT)

    def test_runs_with_write_state_or_no_beacon_is_ever_written(self) -> None:
        command = _load(FLEET_TEMPLATE)["ProgramArguments"][2]
        assert "scripts/fleet_role_drift.py" in command
        assert "--write-state" in command
        help_text = subprocess.run(
            [sys.executable, str(FLEET_SCRIPT), "--help"], capture_output=True, text=True, timeout=30
        ).stdout
        assert "--write-state" in help_text

    def test_first_run_happens_on_install(self) -> None:
        """Without RunAtLoad the beacon would stay absent for one whole interval."""
        assert _load(FLEET_TEMPLATE)["RunAtLoad"] is True

    def test_label_is_bare_like_ledger_health(self) -> None:
        """One instance per machine: the tool measures the whole fleet."""
        assert _load(FLEET_TEMPLATE)["Label"] == "com.vnx.fleet-role-drift"

    def test_comment_says_nonzero_exit_is_normal(self) -> None:
        raw = _raw(FLEET_TEMPLATE)
        assert "non-zero exit is expected and normal" in raw
        assert "Exit 1" in raw


class TestInstallableByReloadPlist:
    """Runs the real ``reload_plist.sh`` with a fake HOME and a stub launchctl,
    so nothing is loaded on the machine running the test."""

    PROJECT = "drivers-test"

    def _install(self, tmp_path: Path, name: str) -> subprocess.CompletedProcess:
        home = tmp_path / "home"
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        loaded = tmp_path / "loaded.txt"
        stub = stub_dir / "launchctl"
        stub.write_text(
            "#!/bin/bash\n"
            'case "$1" in\n'
            f'  load) basename "$2" .plist >> "{loaded}" ;;\n'
            f'  list) while read -r label; do printf -- "-\\t0\\t%s\\n" "$label"; done < "{loaded}" ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        loaded.write_text("", encoding="utf-8")
        env = {
            "HOME": str(home),
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "VNX_HOME": str(REPO),
            "VNX_PROJECT_ID": self.PROJECT,
        }
        return subprocess.run(
            ["bash", str(RELOAD), name], capture_output=True, text=True, env=env, timeout=60
        )

    def _installed(self, tmp_path: Path, label: str) -> Dict[str, Any]:
        path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{label}.plist"
        assert path.is_file(), f"reload_plist.sh did not write {path}"
        assert "${" not in path.read_text(encoding="utf-8"), "an unresolved placeholder was installed"
        return _load(path)

    def test_dashboard_generator_installs_under_a_project_scoped_label(self, tmp_path: Path) -> None:
        result = self._install(tmp_path, "com.vnx.dashboard-generator")
        assert result.returncode == 0, result.stdout + result.stderr

        data = self._installed(tmp_path, f"com.vnx.dashboard-generator.{self.PROJECT}")
        assert data["Label"] == f"com.vnx.dashboard-generator.{self.PROJECT}"
        assert data["EnvironmentVariables"]["VNX_PROJECT_ID"] == self.PROJECT
        assert data["EnvironmentVariables"]["VNX_HOME"] == str(REPO)
        assert data["StandardOutPath"] == f"/tmp/vnx-dashboard-generator-{self.PROJECT}.log"
        assert data["WorkingDirectory"] == str(REPO)
        assert f"cd {REPO} && exec bash scripts/generate_valid_dashboard.sh" in data["ProgramArguments"][2]

    def test_fleet_role_drift_installs_with_the_project_id_in_its_environment(self, tmp_path: Path) -> None:
        result = self._install(tmp_path, "com.vnx.fleet-role-drift")
        assert result.returncode == 0, result.stdout + result.stderr

        data = self._installed(tmp_path, "com.vnx.fleet-role-drift")
        assert data["EnvironmentVariables"]["VNX_PROJECT_ID"] == self.PROJECT
        assert data["StartInterval"] == _load(FLEET_TEMPLATE)["StartInterval"]
        assert f"cd {REPO} && exec python3 scripts/fleet_role_drift.py --write-state" in data["ProgramArguments"][2]


@pytest.mark.skipif(shutil.which("flock") is None, reason="enforce_singleton needs flock(1)")
class TestDashboardSingletonLockHolds:
    """The claim in the plist comment, run for real: the script's own singleton
    lock is what keeps launchd, ``vnx start`` and the supervisor from starting
    it twice. An isolated data home, so the live store is never touched."""

    def _env(self, data_home: Path) -> Dict[str, str]:
        return {
            "HOME": os.environ["HOME"],
            "PATH": os.environ["PATH"],
            "VNX_HOME": str(REPO),
            "VNX_PROJECT_ID": "lockprobe",
            "VNX_DATA_HOME": str(data_home),
        }

    def _start_holder(self, env: Dict[str, str]) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(
            ["bash", "-c", f'cd "{REPO}" && exec bash scripts/generate_valid_dashboard.sh'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_for_pidfile(self, pidfile: Path, pid: int) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if pidfile.is_file() and pidfile.read_text().strip() == str(pid):
                return
            time.sleep(0.1)
        raise AssertionError(f"{pidfile} never carried pid {pid}")

    def _kill(self, proc: Optional["subprocess.Popen[bytes]"]) -> None:
        if proc is None or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

    def test_a_second_instance_exits_zero_and_leaves_the_holder_alone(self, tmp_path: Path) -> None:
        data_home = tmp_path / "vnx-data-home"
        (data_home / "lockprobe" / "state").mkdir(parents=True)
        pidfile = data_home / "lockprobe" / "pids" / "dashboard.pid"
        env = self._env(data_home)
        holder = None
        try:
            holder = self._start_holder(env)
            self._wait_for_pidfile(pidfile, holder.pid)

            second = subprocess.run(
                ["bash", "-c", f'cd "{REPO}" && exec bash scripts/generate_valid_dashboard.sh'],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert second.returncode == 0, second.stdout + second.stderr
            assert "Another instance of dashboard is already running" in second.stdout
            assert f"PID: {holder.pid}" in second.stdout
            assert holder.poll() is None, "the second start must not disturb the running instance"
            assert pidfile.read_text().strip() == str(holder.pid)
        finally:
            self._kill(holder)

    def test_a_killed_holder_does_not_leave_a_stale_lock(self, tmp_path: Path) -> None:
        """kill -9 cannot run the script's trap. The kernel releases the flock,
        so the next start (launchd's restart) must acquire it."""
        data_home = tmp_path / "vnx-data-home"
        (data_home / "lockprobe" / "state").mkdir(parents=True)
        pidfile = data_home / "lockprobe" / "pids" / "dashboard.pid"
        env = self._env(data_home)
        first = successor = None
        try:
            first = self._start_holder(env)
            self._wait_for_pidfile(pidfile, first.pid)
            self._kill(first)

            successor = self._start_holder(env)
            self._wait_for_pidfile(pidfile, successor.pid)

            assert successor.poll() is None
        finally:
            self._kill(first)
            self._kill(successor)
