#!/usr/bin/env python3
"""The central-store guard recognises every test runner, not only pytest.

Track absence-is-loud, point 1. ``tests/conftest.py`` pins a tmp store, but it
loads only under pytest, and the #1333 guard only recognised pytest. A test
file run with ``python -m unittest`` therefore resolved to the operator's real
central store and wrote there: a ``cleanup_worker_exit`` beacon for
``dispatch-fwd-01`` on ``fail``, ``worker_exited`` / ``dispatch_completed``
register events, 132 receipt lines carrying ``dispatch-fwd-0x`` ids.

Every check here runs the write in a CHILD process whose ``HOME`` is a tmp dir,
so the "real central store" (``~/.vnx-data/vnx-dev``) is a tmp dir too and
nothing here can touch the operator's ledger. The child environment is built
from an allowlist: no ``VNX_*`` pin (the pin is exactly what a unittest run
lacks), no ``PYTEST_CURRENT_TEST`` (a child of pytest would otherwise inherit
the pytest marker and every "plain script" case would be refused for the wrong
reason), and no ``claude`` on ``PATH``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "scripts" / "lib"
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(LIB_DIR))

import state_writer
import vnx_paths
from health_beacon import HealthBeacon

# Aliased: imported into a test module, the class would otherwise be
# collected as a test class (name starts with "Test").
IsolationGuardError = vnx_paths.TestIsolationGuardError

PROJECT_ID = "vnx-dev"
CHILD_TIMEOUT_SECONDS = 180


def _child_env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.pathsep.join(
            [str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        ),
        "HOME": str(home),
        "VNX_PROJECT_ID": PROJECT_ID,
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    env.update(extra)
    return env


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake HOME holding an existing central install, so an unpinned
    resolution lands on ``<home>/.vnx-data/vnx-dev`` (resolution branch 3)."""
    fake_home = tmp_path / "home"
    (fake_home / ".vnx-data" / PROJECT_ID).mkdir(parents=True)
    return fake_home


@pytest.fixture
def work(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def _store_file(home: Path, relative: str) -> Path:
    return home / ".vnx-data" / PROJECT_ID / relative


def _run_unittest(work: Path, home: Path, source: str) -> subprocess.CompletedProcess:
    (work / "test_probe.py").write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", "test_probe"],
        cwd=work, env=_child_env(home), capture_output=True, text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


def _run_script(work: Path, home: Path, source: str, **extra_env: str) -> subprocess.CompletedProcess:
    (work / "probe.py").write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "probe.py"],
        cwd=work, env=_child_env(home, **extra_env), capture_output=True, text=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


_PREAMBLE = textwrap.dedent(
    '''\
    import os, sys, unittest, unittest.mock
    from pathlib import Path
    sys.path.insert(0, "__LIB__")
    sys.path.insert(0, "__SCRIPTS__")
    from vnx_paths import TestIsolationGuardError

    STORE = Path(os.path.expanduser("~")) / ".vnx-data" / "vnx-dev"
    STATE = STORE / "state"
    RECEIPT = dict(
        timestamp="2026-09-23T06:00:00Z",
        event_type="subprocess_completion",
        dispatch_id="guard-probe-001",
        terminal="T1",
        status="failed",
        source="subprocess",
        project_id="vnx-dev",
        model="sonnet",
    )
    '''
).replace("__LIB__", str(LIB_DIR)).replace("__SCRIPTS__", str(SCRIPTS_DIR))


@dataclass(frozen=True)
class Surface:
    """One write surface: the code that writes, and what must land in the
    store when the write is allowed. Under a test runner the write must raise
    ``TestIsolationGuardError`` and land nothing."""

    write: str
    landed: tuple[str, ...]


SURFACES = {
    "receipt": Surface(
        write="""
        import append_receipt
        append_receipt.append_receipt_payload(dict(RECEIPT), skip_enrichment=True)
        """,
        landed=("state/t0_receipts.ndjson",),
    ),
    "register": Surface(
        write="""
        import dispatch_register
        assert dispatch_register.append_event(
            "dispatch_created", dispatch_id="guard-probe-002", project_id="vnx-dev",
        )
        """,
        landed=("state/dispatch_register.ndjson",),
    ),
    "health_beacon": Surface(
        write="""
        from health_beacon import HealthBeacon
        HealthBeacon(STORE, "guard_probe").heartbeat()
        """,
        landed=("health/guard_probe.json",),
    ),
    # The measured leak. cleanup_worker_exit wraps every step best-effort, so a
    # per-step refusal would be swallowed into a WARN: it refuses up front
    # instead, before the register event, the beacon or the coordination DB.
    "worker_exit_cleanup": Surface(
        write="""
        from cleanup_worker_exit import cleanup_worker_exit
        cleanup_worker_exit(
            terminal_id="T1", dispatch_id="guard-probe-003", exit_status="success",
        )
        """,
        landed=(
            "state/dispatch_register.ndjson",
            "health/cleanup_worker_exit.json",
            "state/runtime_coordination.db",
        ),
    ),
    "intelligence_offer": Surface(
        write="""
        from gather_intelligence import T0IntelligenceGatherer
        T0IntelligenceGatherer().record_pattern_offer("guard-pattern", "T1", "guard-probe-004")
        """,
        landed=("state/intelligence_usage.ndjson",),
    ),
    "intelligence_archival": Surface(
        write="""
        import learning_loop
        learning_loop._emit_archival_decision_event(
            STATE, pattern_id="guard-pattern", decision="archive", approved_by="op",
            approval_id="a-1", reason="probe", action="archive", source_table="t",
            db_applied=False,
        )
        """,
        landed=("state/intelligence_usage.ndjson",),
    ),
    "intelligence_queries": Surface(
        write="""
        from intelligence_queries import IntelligenceQueryAPI
        IntelligenceQueryAPI()._log_usage("guard-probe")
        """,
        landed=("state/intelligence_usage.ndjson",),
    ),
}


def _module_source(surface: Surface, *, as_test: bool) -> str:
    body = textwrap.indent(textwrap.dedent(surface.write).strip("\n"), "    ")
    source = f"{_PREAMBLE}\n\ndef write():\n{body}\n"
    if not as_test:
        return source + "\nwrite()\nprint('WRITE_DONE')\n"
    return (
        source
        + "\n\nclass ProbeTest(unittest.TestCase):\n    def test_write(self):\n"
        + "        with self.assertRaises(TestIsolationGuardError):\n            write()\n"
    )


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

class TestDetector:
    def test_pytest_is_a_test_runner(self):
        assert vnx_paths.running_under_test_runner() is True

    def test_pytest_only_names_are_gone(self):
        # The guard was generalised, not aliased: no caller keeps a
        # pytest-only spelling that would suggest unittest is not covered.
        assert not hasattr(vnx_paths, "refuse_real_central_store_write_under_pytest")
        assert not hasattr(vnx_paths, "refuse_real_launch_agents_write_under_pytest")

    def test_python_m_unittest_is_a_test_runner(self, home, work):
        source = _PREAMBLE + textwrap.dedent(
            '''
            import threading
            import vnx_paths

            class ProbeTest(unittest.TestCase):
                def test_main_thread(self):
                    self.assertTrue(vnx_paths.running_under_test_runner())

                def test_thread_started_by_a_test(self):
                    seen = []
                    worker = threading.Thread(
                        target=lambda: seen.append(vnx_paths.running_under_test_runner())
                    )
                    worker.start()
                    worker.join()
                    self.assertEqual(seen, [True])
            '''
        )
        result = _run_unittest(work, home, source)
        assert result.returncode == 0, result.stderr

    def test_unittest_main_in_a_script_is_a_test_runner(self, home, work):
        source = _PREAMBLE + textwrap.dedent(
            '''
            import vnx_paths

            class ProbeTest(unittest.TestCase):
                def test_runner(self):
                    self.assertTrue(vnx_paths.running_under_test_runner())

            if __name__ == "__main__":
                unittest.main()
            '''
        )
        result = _run_script(work, home, source)
        assert result.returncode == 0, result.stderr

    def test_plain_script_that_uses_unittest_is_not_a_test_runner(self, home, work):
        # A production process may import unittest and use unittest.mock or a
        # TestCase's assertions. None of that is a test run.
        source = _PREAMBLE + textwrap.dedent(
            '''
            import vnx_paths
            case = unittest.TestCase()
            case.assertEqual(1, 1)
            with unittest.mock.patch("os.getpid", return_value=1):
                inside_mock = vnx_paths.running_under_test_runner()
            print("RUNNER", inside_mock, vnx_paths.running_under_test_runner())
            '''
        )
        result = _run_script(work, home, source)
        assert result.returncode == 0, result.stderr
        assert "RUNNER False False" in result.stdout

    def test_pytest_marker_in_the_environment_is_still_detected(self, home, work):
        source = _PREAMBLE + "import vnx_paths\nprint('RUNNER', vnx_paths.running_under_test_runner())\n"
        result = _run_script(work, home, source, PYTEST_CURRENT_TEST="probe::test (call)")
        assert result.returncode == 0, result.stderr
        assert "RUNNER True" in result.stdout


# ---------------------------------------------------------------------------
# The write surfaces
# ---------------------------------------------------------------------------

class TestWriteSurfaces:
    @pytest.mark.parametrize("name", sorted(SURFACES))
    def test_write_is_refused_under_python_m_unittest(self, name, home, work):
        surface = SURFACES[name]

        result = _run_unittest(work, home, _module_source(surface, as_test=True))

        assert result.returncode == 0, result.stderr
        for relative in surface.landed:
            assert not _store_file(home, relative).exists(), (
                f"{relative} landed in the real central store from a unittest run"
            )

    @pytest.mark.parametrize("name", sorted(SURFACES))
    def test_same_write_lands_from_a_plain_script(self, name, home, work):
        surface = SURFACES[name]

        result = _run_script(work, home, _module_source(surface, as_test=False))

        assert result.returncode == 0, result.stderr
        assert "WRITE_DONE" in result.stdout
        for relative in surface.landed:
            landed = _store_file(home, relative)
            assert landed.exists() and landed.stat().st_size > 0, (
                f"{relative} did not land from a plain script: a production run "
                "must not be refused"
            )

    def test_write_is_refused_under_unittest_main_in_a_script(self, home, work):
        surface = SURFACES["receipt"]
        source = _module_source(surface, as_test=True) + (
            '\n\nif __name__ == "__main__":\n    unittest.main()\n'
        )

        result = _run_script(work, home, source)

        assert result.returncode == 0, result.stderr
        assert not _store_file(home, "state/t0_receipts.ndjson").exists()

    def test_refusal_names_the_runner_and_the_fix(self, home, work):
        source = _PREAMBLE + textwrap.dedent(
            '''
            import vnx_paths

            class ProbeTest(unittest.TestCase):
                def test_message(self):
                    with self.assertRaises(TestIsolationGuardError) as caught:
                        vnx_paths.refuse_real_central_store_write_under_test_runner(STATE)
                    print("MESSAGE", caught.exception)
            '''
        )
        result = _run_unittest(work, home, source)

        assert result.returncode == 0, result.stderr
        assert "TEST ISOLATION GUARD" in result.stdout
        assert "unittest" in result.stdout
        assert "VNX_DATA_DIR" in result.stdout


class TestSurfaceGuardsInProcess:
    """Wiring that does not need a child process: pytest is itself a test
    runner, so the guard is live here, and HOME points at a tmp dir."""

    def test_state_writer_refuses_append_and_rewrite(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(fake_home))
        target = fake_home / ".vnx-data" / PROJECT_ID / "state" / "dispatch_register.ndjson"

        with pytest.raises(IsolationGuardError):
            state_writer.append_locked(target, {"event": "worker_exited"})
        with pytest.raises(IsolationGuardError):
            state_writer.rewrite_locked(target, b"{}\n")

        assert not (fake_home / ".vnx-data").exists(), "the guard must refuse before any mkdir"

    def test_state_writer_still_writes_outside_the_real_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        target = tmp_path / "pinned" / "state" / "dispatch_register.ndjson"

        assert state_writer.append_locked(target, {"event": "worker_exited"}) is True

        assert target.read_text(encoding="utf-8").count("worker_exited") == 1

    def test_heartbeat_refuses_on_its_own_not_only_the_constructor(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        beacon = HealthBeacon(fake_home / ".vnx-data" / PROJECT_ID, "guard_probe")
        monkeypatch.setenv("HOME", str(fake_home))

        with pytest.raises(IsolationGuardError):
            beacon.heartbeat()

        assert not beacon.path.exists()

    def test_heartbeat_outside_the_real_store_still_writes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        beacon = HealthBeacon(tmp_path / "pinned", "guard_probe")

        beacon.heartbeat()

        assert beacon.path.exists()


# ---------------------------------------------------------------------------
# The test that hung for 16 minutes
# ---------------------------------------------------------------------------

class TestAutoCommitIsolationRunsHermetically:
    def test_runs_under_unittest_without_touching_the_central_store(self, home):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-q", "test_auto_commit_stash_isolation"],
            cwd=REPO_ROOT / "tests", env=_child_env(home), capture_output=True, text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
        )

        assert result.returncode == 0, result.stderr[-2000:]
        leaked = sorted(
            str(path.relative_to(home)) for path in home.rglob("*") if path.is_file()
        )
        assert leaked == [], f"the run wrote into the real central store: {leaked}"
