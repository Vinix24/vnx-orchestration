"""tests/test_daemon_register.py — D2 (absence-is-loud).

Covers scripts/lib/daemon_register.py:
- read_daemon_register() parses start_all() in vnx_supervisor_simple.sh: the
  chosen source of truth (three other candidates were rejected, see the
  module docstring). Comments, the two shell-variable receipt_processor
  entries, and the VNX_QUEUE_POPUP_ENABLED conditional branch must all
  resolve correctly.
- measure_daemon_liveness() reports a tri-state per daemon (running/absent/
  unknown) -- "could not measure" must never collapse into "absent".
- Regression: matching must use exact argv-token basename equality, not a
  substring search over the joined command line. A `claude -p <prompt>`
  worker's own argv carries its full instruction text as one element, and
  that prose can literally contain a daemon's script filename -- a naive
  substring match self-matches every daemon onto the wrong PID (reproduced
  live while building this module: all nine daemons matched the running
  worker's own PID before the basename fix).
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Iterator

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import daemon_register as dr  # noqa: E402


# ---------------------------------------------------------------------------
# read_daemon_register — synthetic fixture scripts
# ---------------------------------------------------------------------------

_FIXTURE_SUPERVISOR = textwrap.dedent(
    """\
    #!/bin/bash
    RECEIPT_SERVICE_NAME="receipt_processor"
    RECEIPT_SCRIPT="receipt_processor.sh"

    start_all() {
        log "Starting all VNX processes..."

        start_process "dispatcher" "dispatcher_minimal.sh"
        # - dispatcher_v7_compilation.sh (Phase 1B: replaced by V8 native skills)
        start_process "smart_tap" "smart_tap_json_translator.sh"
        start_process "$RECEIPT_SERVICE_NAME" "$RECEIPT_SCRIPT"
        # start_process "ack_dispatcher" "ack_dispatcher_v2.sh"
        start_process "heartbeat_ack_monitor" "heartbeat_ack_monitor.py"
        if [ "${VNX_QUEUE_POPUP_ENABLED:-1}" != "0" ]; then
          start_process "queue_watcher" "queue_popup_watcher.sh"
        else
          log "Queue popup disabled — starting auto-accept watcher instead"
          start_process "queue_watcher" "queue_auto_accept.sh"
        fi
        start_process "dashboard" "generate_valid_dashboard.sh"
        start_process "state_manager" "unified_state_manager.py"
        start_process "intelligence_daemon" "intelligence_daemon.py"
        start_process "recommendations_engine" "recommendations_engine_daemon.sh"

        log "All processes started"
    }

    stop_all() {
        stop_process "dispatcher" "dispatcher_minimal.sh"
    }
    """
)


@pytest.fixture()
def fixture_supervisor(tmp_path: Path) -> Path:
    p = tmp_path / "vnx_supervisor_simple.sh"
    p.write_text(_FIXTURE_SUPERVISOR, encoding="utf-8")
    return p


# D2b reproduction: the exact real-world defect. A purely cosmetic edit —
# switching start_process's opening quote from double to single — leaves
# start_all() perfectly findable but silently breaks every _START_PROCESS_RE
# match (it anchors on a literal `"` right after `start_process`), so the
# parse goes from nine entries to zero without raising anything on its own.
# Measured live against the real supervisor shell before this fix:
# read_daemon_register() returned () with no exception, and
# measure_daemon_liveness(()) reported {"overall": "ok", "daemons": {}}.
_FIXTURE_SUPERVISOR_BROKEN_QUOTES = _FIXTURE_SUPERVISOR.replace(
    'start_process "', "start_process '"
)


@pytest.fixture()
def fixture_supervisor_broken_quotes(tmp_path: Path) -> Path:
    p = tmp_path / "vnx_supervisor_simple.sh"
    p.write_text(_FIXTURE_SUPERVISOR_BROKEN_QUOTES, encoding="utf-8")
    return p


class TestReadDaemonRegister:
    def test_nine_daemons_from_fixture(self, fixture_supervisor: Path) -> None:
        reg = dr.read_daemon_register(fixture_supervisor)
        assert len(reg) == 9

    def test_commented_out_entry_excluded(self, fixture_supervisor: Path) -> None:
        reg = dr.read_daemon_register(fixture_supervisor)
        names = {spec.name for spec in reg}
        assert "ack_dispatcher" not in names

    def test_shell_variable_resolved(self, fixture_supervisor: Path) -> None:
        reg = dr.read_daemon_register(fixture_supervisor)
        by_name = {spec.name: spec for spec in reg}
        assert "receipt_processor" in by_name
        assert by_name["receipt_processor"].scripts == ("receipt_processor.sh",)

    def test_conditional_branch_merges_both_scripts(self, fixture_supervisor: Path) -> None:
        reg = dr.read_daemon_register(fixture_supervisor)
        by_name = {spec.name: spec for spec in reg}
        qw = by_name["queue_watcher"]
        assert set(qw.scripts) == {"queue_popup_watcher.sh", "queue_auto_accept.sh"}
        assert qw.conditional is True

    def test_unconditional_entries_not_flagged_conditional(self, fixture_supervisor: Path) -> None:
        reg = dr.read_daemon_register(fixture_supervisor)
        by_name = {spec.name: spec for spec in reg}
        assert by_name["dispatcher"].conditional is False

    def test_stop_all_not_included(self, fixture_supervisor: Path) -> None:
        # stop_all() repeats "dispatcher" -- must not be double-counted or
        # picked up as a second register.
        reg = dr.read_daemon_register(fixture_supervisor)
        assert len([s for s in reg if s.name == "dispatcher"]) == 1

    def test_missing_start_all_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "no_start_all.sh"
        p.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
        with pytest.raises(ValueError):
            dr.read_daemon_register(p)

    def test_broken_quote_style_raises_not_empty_tuple(
        self, fixture_supervisor_broken_quotes: Path
    ) -> None:
        """D2b: start_all() is found, but the quote-style change means zero
        start_process lines match. This must raise -- NOT return () as if
        the project legitimately runs zero daemons (the exact real-world
        defect: nine daemons silently became zero and nothing said so)."""
        with pytest.raises(ValueError, match="zero start_process"):
            dr.read_daemon_register(fixture_supervisor_broken_quotes)

    def test_real_supervisor_script_gives_nine(self) -> None:
        """Integration: the actual vnx_supervisor_simple.sh in this repo."""
        reg = dr.read_daemon_register()
        assert len(reg) == 9
        names = {spec.name for spec in reg}
        assert names == {
            "dispatcher", "smart_tap", "receipt_processor",
            "heartbeat_ack_monitor", "queue_watcher", "dashboard",
            "state_manager", "intelligence_daemon", "recommendations_engine",
        }


# ---------------------------------------------------------------------------
# measure_daemon_liveness — tri-state
# ---------------------------------------------------------------------------

class TestMeasureDaemonLivenessAbsence:
    def test_all_absent_when_nothing_matches(self) -> None:
        register = (dr.DaemonSpec(name="nope", scripts=("definitely_not_a_real_script_xyz.sh",)),)
        result = dr.measure_daemon_liveness(register)
        assert result["overall"] == "fail"
        assert result["daemons"]["nope"]["state"] == "absent"
        assert result["daemons"]["nope"]["expected"] is True
        assert result["daemons"]["nope"]["pid"] is None
        assert result["daemons"]["nope"]["since"] is None

    def test_all_ok_when_register_empty(self) -> None:
        # An EXPLICIT empty register, handed in by the caller, is a
        # legitimate "zero daemons expected" -- distinct from D2b's failed-
        # internal-parse case below, which must NOT reach this outcome.
        result = dr.measure_daemon_liveness(())
        assert result["overall"] == "ok"
        assert result["daemons"] == {}


class TestMeasureDaemonLivenessFailedParse:
    def test_broken_quote_style_register_is_not_ok(
        self, fixture_supervisor_broken_quotes: Path
    ) -> None:
        """D2b reproduction end-to-end: register=None (the real call shape
        used by build_t0_state.py) against a supervisor script whose
        start_process quoting broke. Before this fix, read_daemon_register()
        silently returned () and this call reported {"overall": "ok"} --
        nine daemons vanishing with a clean bill of health. It must now
        surface as a failure, never "ok"."""
        result = dr.measure_daemon_liveness(supervisor_script=fixture_supervisor_broken_quotes)
        assert result["overall"] != "ok"
        assert result["overall"] == "unknown"
        assert "reason" in result
        assert "zero start_process" in result["reason"]


class TestMeasureDaemonLivenessRunning:
    @pytest.fixture()
    def running_child(self, tmp_path: Path) -> Iterator[subprocess.Popen]:
        script = tmp_path / "fake_daemon_xyz123.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script)])
        try:
            time.sleep(0.3)  # let psutil see a stable cmdline
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_running_process_detected_by_basename(self, running_child: subprocess.Popen) -> None:
        register = (dr.DaemonSpec(name="fake_daemon", scripts=("fake_daemon_xyz123.py",)),)
        result = dr.measure_daemon_liveness(register)
        entry = result["daemons"]["fake_daemon"]
        assert entry["state"] == "running"
        assert entry["pid"] == running_child.pid
        assert entry["since"] is not None
        assert result["overall"] == "ok"

    def test_substring_in_unrelated_process_does_not_false_match(
        self, running_child: subprocess.Popen
    ) -> None:
        """Regression: a script name that is merely a SUBSTRING of some other
        process's argv (e.g. this test file's own long tmp_path) must not
        register as a match -- only an exact argv-token basename counts."""
        register = (dr.DaemonSpec(name="fake_daemon", scripts=("xyz123",)),)
        result = dr.measure_daemon_liveness(register)
        # "xyz123" is a substring of fake_daemon_xyz123.py but not a full
        # basename of any argv token -- must NOT match.
        assert result["daemons"]["fake_daemon"]["state"] == "absent"


class TestMeasureDaemonLivenessUnknown:
    def test_psutil_import_failure_yields_unknown_not_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("psutil not installed (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)

        register = (dr.DaemonSpec(name="whatever", scripts=("whatever.sh",)),)
        result = dr.measure_daemon_liveness(register)
        assert result["overall"] == "unknown"
        assert result["daemons"]["whatever"]["state"] == "unknown"
        assert result["daemons"]["whatever"]["state"] not in ("absent", "running")

    def test_process_iter_failure_yields_unknown_not_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psutil

        def _boom(*_a, **_k):
            raise RuntimeError("enumeration exploded (simulated)")

        monkeypatch.setattr(psutil, "process_iter", _boom)

        register = (dr.DaemonSpec(name="whatever", scripts=("whatever.sh",)),)
        result = dr.measure_daemon_liveness(register)
        assert result["overall"] == "unknown"
        assert result["daemons"]["whatever"]["state"] == "unknown"

    def test_unreadable_register_yields_unknown_empty_daemons(self, tmp_path: Path) -> None:
        result = dr.measure_daemon_liveness(supervisor_script=tmp_path / "does_not_exist.sh")
        assert result["overall"] == "unknown"
        assert result["daemons"] == {}
        assert "reason" in result
