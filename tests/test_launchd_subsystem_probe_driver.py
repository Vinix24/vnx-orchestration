"""tests/test_launchd_subsystem_probe_driver.py: absence-is-loud, punt 1.

``governance-enforcement-stack`` and ``plan-gate-panel`` sat on ``stale`` in
t0_state from 12-07 to 24-09. Their producer, ``subsystem_health.aggregate()``,
writes one beacon per subsystem with the default ``expected_interval_seconds``
of 86400 and only ran when someone typed ``vnx subsystems --probe``. Both probes
work; nothing drove them.

``scripts/launchd/com.vnx.subsystem-probe.plist`` is the driver. These tests pin
what makes it a driver and not a file that exists:

  - the cadence is provably inside the beacon window, read from the code that
    writes the beacon and again from a beacon the job actually wrote;
  - the command, resolved by the real ``reload_plist.sh`` and executed with the
    environment launchd would give it (a tmp HOME, no shell profile), lands both
    beacons in ``<data_dir>/health/`` and nowhere else;
  - the job is an expected launchd job in t0_state, and the parity guard can see
    which interpreter it runs.

Nothing here touches the real store: every run gets a tmp HOME and a throwaway
engine checkout, because the probes read the git-ignored ``.vnx-attest/`` ledger
of the checkout they run from.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = REPO / "scripts" / "launchd"
TEMPLATE = LAUNCHD_DIR / "com.vnx.subsystem-probe.plist"
RELOAD = LAUNCHD_DIR / "reload_plist.sh"
SUBSYSTEM_HEALTH = REPO / "scripts" / "lib" / "subsystem_health.py"
SUBSYSTEMS_SH = REPO / "scripts" / "commands" / "subsystems.sh"

LABEL = "com.vnx.subsystem-probe"
BEACONS = ("governance-enforcement-stack", "plan-gate-panel")
PROJECT = "probe-test"

# What vnx_cli.main needs to run the probes: the CLI package and the engine
# scripts. The probes resolve the repo they measure from their own git root, so
# the copy is a git repo of its own.
_ENGINE_TREES = ("scripts", "vnx_cli")


def _lib(name: str) -> ModuleType:
    """Import an engine module lazily, with scripts/lib and scripts on the path."""
    for entry in (REPO / "scripts" / "lib", REPO / "scripts"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    return importlib.import_module(name)


def _load(path: Path) -> Dict[str, Any]:
    with path.open("rb") as fh:
        return plistlib.load(fh)


def _raw(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _beacon_window_seconds() -> int:
    """The ``expected_interval_seconds`` a beacon from ``aggregate()`` carries.

    ``aggregate()`` builds ``HealthBeacon(state_dir, name)`` with no interval, so
    the effective value is HealthBeacon's own default. Both halves are read: the
    call site must not override it, and the default is what then applies."""
    tree = ast.parse(_raw(SUBSYSTEM_HEALTH))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HealthBeacon"
    ]
    assert calls, f"no HealthBeacon(...) call in {SUBSYSTEM_HEALTH}"
    for call in calls:
        assert len(call.args) < 3, "the interval is passed positionally: this test no longer reads the truth"
        assert "expected_interval_seconds" not in {kw.arg for kw in call.keywords}
    default = inspect.signature(_lib("health_beacon").HealthBeacon.__init__).parameters[
        "expected_interval_seconds"
    ].default
    assert isinstance(default, int)
    return default


def _build_engine(dest: Path, *, with_ledger: bool) -> Path:
    """A throwaway engine checkout: the code the job runs, as its own git repo.

    The probes read ``<git root of their own code>/.vnx-attest/plan-gates.ndjson``
    and that directory is git-ignored, so a fresh copy has none unless it is
    seeded here."""
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for tree in _ENGINE_TREES:
        shutil.copytree(REPO / tree, dest / tree, ignore=ignore, symlinks=True)
    subprocess.run(["git", "init", "-q", str(dest)], check=True, capture_output=True)
    if with_ledger:
        ledger = dest / ".vnx-attest" / "plan-gates.ndjson"
        ledger.parent.mkdir()
        ledger.write_text(
            "".join(
                json.dumps({"type": "plan_gate_pass", "resolver": "run", "track": track}) + "\n"
                for track in ("probe-a", "probe-b")
            ),
            encoding="utf-8",
        )
    return dest


def _install(root: Path, engine: Path) -> Dict[str, Any]:
    """Run the real ``reload_plist.sh`` with a tmp HOME and a stub launchctl, so
    nothing is loaded on the machine running the test. Returns the plist it wrote."""
    home = root / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    stub_dir = root / "bin"
    stub_dir.mkdir()
    loaded = root / "loaded.txt"
    loaded.write_text("", encoding="utf-8")
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
    env = {
        "HOME": str(home),
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "VNX_HOME": str(engine),
        "VNX_PROJECT_ID": PROJECT,
    }
    result = subprocess.run(
        ["bash", str(RELOAD), LABEL], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert installed.is_file(), f"reload_plist.sh did not write {installed}"
    assert "${" not in _raw(installed), "an unresolved placeholder was installed"
    return _load(installed)


def _run_job(installed: Dict[str, Any], home: Path, **extra_env: str) -> subprocess.CompletedProcess:
    """Execute the installed job's command the way launchd would: its own
    ``EnvironmentVariables`` and nothing from this shell, HOME pointed at a tmp dir.

    The interpreter running the tests goes first on PATH. On the machine the
    plist's own PATH already resolves ``python3``; a CI runner keeps its python
    elsewhere."""
    env = dict(installed["EnvironmentVariables"])
    env["HOME"] = str(home)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env["PATH"]])
    env.update(extra_env)
    return subprocess.run(
        installed["ProgramArguments"], env=env, cwd=str(home), capture_output=True, text=True, timeout=120
    )


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_engine(tmp_path_factory.mktemp("engine") / "checkout", with_ledger=True)


class TestTemplateIsWellFormed:
    def test_parses_as_a_plist(self) -> None:
        data = _load(TEMPLATE)
        assert isinstance(data["Label"], str) and data["Label"]
        assert data["ProgramArguments"][:2] == ["/bin/bash", "-c"]

    @pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
    def test_plutil_lint_passes(self) -> None:
        result = subprocess.run(["plutil", "-lint", str(TEMPLATE)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_no_store_path_is_hardcoded(self) -> None:
        """A copied template must run against the project it was installed for."""
        raw = _raw(TEMPLATE)
        assert "/.vnx-data" not in raw
        assert "/Users/" not in raw

    def test_project_and_home_come_from_placeholders(self) -> None:
        env = _load(TEMPLATE)["EnvironmentVariables"]
        assert env["VNX_HOME"] == "${VNX_HOME}"
        assert env["VNX_PROJECT_ID"] == "${VNX_PROJECT_ID}"

    def test_logs_go_to_tmp(self) -> None:
        data = _load(TEMPLATE)
        assert data["StandardOutPath"].startswith("/tmp/")
        assert data["StandardErrorPath"].startswith("/tmp/")

    def test_has_an_explanatory_comment_block(self) -> None:
        raw = _raw(TEMPLATE)
        assert "<!--" in raw and "-->" in raw
        assert "absence-is-loud" in raw

    def test_comment_says_which_checkout_the_job_must_run_from(self) -> None:
        """The probes measure the checkout they run from. The comment is the only
        place that tells the installer, so it has to carry the reason."""
        raw = _raw(TEMPLATE)
        assert ".vnx-attest" in raw
        assert "worktree" in raw


class TestCadence:
    def test_label_is_bare(self) -> None:
        """One instance per machine: the probes measure the checkout that holds the
        git-ignored .vnx-attest ledger, not a project id."""
        assert _load(TEMPLATE)["Label"] == LABEL

    def test_start_interval_is_inside_the_beacon_window(self) -> None:
        """A StartInterval at or above the beacon's expected interval makes the
        beacon stale by construction. Both numbers are read live."""
        interval = _load(TEMPLATE)["StartInterval"]
        window = _beacon_window_seconds()
        assert interval < window, f"StartInterval={interval}s must be below expected_interval_seconds={window}s"

    def test_several_missed_runs_still_land_inside_the_window(self) -> None:
        """Same margin ledger-health argues for: 3 consecutive missed runs must
        not push the beacon past its window."""
        assert 3 * _load(TEMPLATE)["StartInterval"] < _beacon_window_seconds()

    def test_first_run_happens_on_install(self) -> None:
        """Without RunAtLoad the beacons would stay stale for one whole interval."""
        assert _load(TEMPLATE)["RunAtLoad"] is True

    def test_is_an_interval_job_not_a_keepalive_job(self) -> None:
        """The probes finish and exit. Restarting them on exit would spin."""
        assert "KeepAlive" not in _load(TEMPLATE)


class TestCommand:
    def test_runs_the_probe_flag_of_the_subsystems_command(self) -> None:
        command = _load(TEMPLATE)["ProgramArguments"][2]
        assert command.startswith("cd ${VNX_HOME} && ")
        assert "vnx_cli.main subsystems --probe" in command

    def test_is_the_same_entry_point_as_vnx_subsystems(self) -> None:
        """``vnx subsystems`` is a thin wrapper around this module. If it ever
        stops being one, the job no longer runs what the operator runs by hand."""
        assert "vnx_cli.main subsystems" in _raw(SUBSYSTEMS_SH)

    def test_the_parity_guard_can_see_the_interpreter(self) -> None:
        """Behind bin/vnx the interpreter is invisible to the PATH-parity guard
        ("no python invocation found"). Naming python3 in the command is what
        lets it check the interpreter against requires-python."""
        path_parity = _lib("path_parity")
        report = path_parity.check_repo_launchd_templates(
            LAUNCHD_DIR, REPO, path_parity.parse_requires_python(REPO / "pyproject.toml")
        )
        entry = next(c for c in report["consumers"] if c["file"] == TEMPLATE.name)
        assert "bare python3" in entry["reason"], entry
        assert report["ok"], report["xml_errors"] + report["mismatches"]


class TestInstallableByReloadPlist:
    def test_installs_with_the_project_id_and_home_resolved(self, tmp_path: Path, engine: Path) -> None:
        data = _install(tmp_path, engine)
        assert data["Label"] == LABEL
        assert data["EnvironmentVariables"]["VNX_PROJECT_ID"] == PROJECT
        assert data["EnvironmentVariables"]["VNX_HOME"] == str(engine)
        assert data["StartInterval"] == _load(TEMPLATE)["StartInterval"]
        assert f"cd {engine} && exec python3 -m vnx_cli.main subsystems --probe" in data["ProgramArguments"][2]


class TestExpectedByT0State:
    """``build_t0_state._measure_launchd_liveness`` expects every template under
    scripts/launchd. Until the job is installed that is a real finding, which is
    the point: the driver's absence must show up."""

    def _measure(self, tmp_path: Path, loaded: List[str]) -> Dict[str, Any]:
        bts = _lib("build_t0_state")
        lines = ["PID\tStatus\tLabel"] + [f"-\t0\t{label}" for label in loaded]

        def runner(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(stdout="\n".join(lines), returncode=0)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return bts._measure_launchd_liveness(
            state_dir,
            launchd_dir=LAUNCHD_DIR,
            runner=runner,
            which_fn=lambda _name: "/bin/launchctl",
            project_id="vnx-dev",
        )

    def test_the_job_is_an_expected_launchd_job(self) -> None:
        bts = _lib("build_t0_state")
        assert LABEL in bts._discover_launchd_jobs(LAUNCHD_DIR, project_id="vnx-dev")

    def test_not_installed_reads_as_not_loaded(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, loaded=[])
        assert result["jobs"][LABEL]["state"] == "not_loaded"
        assert result["overall"] == "fail"

    def test_installed_reads_as_loaded(self, tmp_path: Path) -> None:
        result = self._measure(tmp_path, loaded=[LABEL])
        assert result["jobs"][LABEL]["state"] == "loaded"


class TestTheJobRuns:
    """The resolved command, executed for real against a throwaway engine checkout
    that carries a two-entry attestation ledger."""

    @pytest.fixture(scope="class")
    def central(self, engine: Path, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
        """How the job finds its store on the machine: no explicit data dir, an
        existing ``~/.vnx-data/<project id>``, the project id from the plist."""
        root = tmp_path_factory.mktemp("central")
        installed = _install(root, engine)
        home = root / "home"
        data_dir = home / ".vnx-data" / PROJECT
        data_dir.mkdir(parents=True)
        result = _run_job(installed, home)
        return SimpleNamespace(result=result, home=home, data_dir=data_dir, installed=installed)

    @pytest.fixture(scope="class")
    def explicit(self, engine: Path, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
        """The same job pointed at a tmp VNX_DATA_DIR."""
        root = tmp_path_factory.mktemp("explicit")
        installed = _install(root, engine)
        home = root / "home"
        data_dir = root / "data-dir"
        data_dir.mkdir()
        result = _run_job(installed, home, VNX_DATA_DIR=str(data_dir), VNX_DATA_DIR_EXPLICIT="1")
        return SimpleNamespace(result=result, home=home, data_dir=data_dir, installed=installed)

    def test_exits_zero_without_a_traceback(self, central: SimpleNamespace) -> None:
        assert central.result.returncode == 0, central.result.stdout + central.result.stderr
        assert "Traceback" not in central.result.stderr

    @pytest.mark.parametrize("name", BEACONS)
    def test_writes_the_beacon_into_the_data_dir_of_its_project(self, central: SimpleNamespace, name: str) -> None:
        beacon = central.data_dir / "health" / f"{name}.json"
        assert beacon.is_file(), central.result.stdout + central.result.stderr
        payload = json.loads(beacon.read_text(encoding="utf-8"))
        assert payload["component"] == name
        assert payload["status"] == "ok"

    def test_writes_nothing_under_state_health(self, central: SimpleNamespace) -> None:
        """The reader looks in <data_dir>/health. A beacon under state/health would
        be one nobody reads."""
        assert not (central.data_dir / "state").exists()

    def test_writes_nothing_into_the_engine_checkout(self, central: SimpleNamespace, engine: Path) -> None:
        assert not (engine / ".vnx-data").exists()

    def test_the_reader_that_t0_state_uses_calls_both_beacons_ok(self, central: SimpleNamespace) -> None:
        beacons = _lib("health_beacon").all_beacons(central.data_dir)
        assert {name: beacons[name]["health"] for name in BEACONS} == {name: "ok" for name in BEACONS}

    def test_the_written_window_is_wider_than_the_interval_the_job_runs_at(self, central: SimpleNamespace) -> None:
        """The number the job's cadence has to beat, read from a beacon the job
        wrote and not from the code that supposedly writes it."""
        interval = central.installed["StartInterval"]
        for name in BEACONS:
            payload = json.loads((central.data_dir / "health" / f"{name}.json").read_text(encoding="utf-8"))
            assert 3 * interval < payload["expected_interval_seconds"], name

    @pytest.mark.parametrize("name", BEACONS)
    def test_an_explicit_data_dir_wins(self, explicit: SimpleNamespace, name: str) -> None:
        assert explicit.result.returncode == 0, explicit.result.stdout + explicit.result.stderr
        assert (explicit.data_dir / "health" / f"{name}.json").is_file()

    def test_an_explicit_data_dir_leaves_the_home_store_alone(self, explicit: SimpleNamespace) -> None:
        assert not (explicit.home / ".vnx-data").exists()

    def test_a_checkout_without_the_ledger_writes_no_beacon(self, tmp_path: Path) -> None:
        """Why the comment says the job must run from the checkout that owns
        .vnx-attest: a dispatch worktree has none, the probes then report unknown,
        and unknown writes no beacon. The job still exits 0, so the beacons would
        go stale again without a sound."""
        bare = _build_engine(tmp_path / "bare-checkout", with_ledger=False)
        installed = _install(tmp_path / "install", bare)
        home = tmp_path / "install" / "home"
        data_dir = home / ".vnx-data" / PROJECT
        data_dir.mkdir(parents=True)

        result = _run_job(installed, home)

        assert result.returncode == 0, result.stdout + result.stderr
        assert not (data_dir / "health").exists() or not any((data_dir / "health").glob("*.json"))
