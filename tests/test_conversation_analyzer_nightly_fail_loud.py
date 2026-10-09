"""The nightly conversation-analyzer job must fail for real on an error.

Measured 2026-09-28: `scripts/conversation_analyzer_nightly.sh` called
`ensure_env`, a function that exists in no shell file. `lib/vnx_paths.sh`
snapshots the caller's shell options inside `$(...)` (where bash clears errexit)
and restores that snapshot on exit, so the script's `set -e` was silently gone
after the `source`. The missing function only wrote "command not found" to
stderr and every phase still logged "complete".

Measured 2026-10-08 (OI-2021): Phase 1 sat 9 h 44 min in one blocked read
behind a pipe to `tee`, the log ended on the phase header, the beacon kept the
previous night's `ok` and the lock would have made the next night exit 0. The
tests from `test_phase1_hang_*` on cover the bounded phases, the fail beacon
and the lock branch.

These tests run the REAL script (copied into a tmp tree next to the real
`lib/`) under bash with a tmp HOME and a tmp state dir, in its own session as
launchd starts it. Every Python phase is a stub; no analyzer, ollama or mail is
started. A fail beacon is written by the REAL `scripts/conversation_analyzer.py`
(the analyzer stub execs it for `--write-fail-beacon`), into the tmp store.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "conversation_analyzer_nightly.sh"
_REAL_ANALYZER = _REPO_ROOT / "scripts" / "conversation_analyzer.py"
_PHASES = (
    "quality_db_init",
    "conversation_analyzer",
    "link_sessions_dispatches",
    "generate_t0_session_brief",
    "governance_aggregator",
    "learning_loop_nightly",
    "generate_suggested_edits",
    "send_digest_email",
)
_SOURCE_LINE = 'source "$SCRIPT_DIR/lib/vnx_paths.sh"\n'
_PRELUDE_END = "# ── Interpreter resolution"
COMPLETE = "Nightly analysis pipeline complete"
FAILED = "Nightly analysis pipeline FAILED"

# Limit for the phase under test, and the script's own grace and reap window
# (KILL_GRACE_SECS, REAP_WAIT_SECS). CI runs on shared runners, so a run is only
# required to return within limit + grace + reap + 20 seconds.
LIMIT = 3
GRACE = 5
REAP = 10
WINDOW = LIMIT + GRACE + REAP + 20

# Every phase stub writes "<pid> <ppid> <pgrp>" to $STUB_MARK_DIR/<phase>.pid and
# then behaves as STUB_MODE_<phase> says. The analyzer stub hands
# --write-fail-beacon to the real analyzer CLI, and STUB_BEACON_<phase> makes it
# write its own beacon through the real _write_heartbeat, as the analyzer does.
_STUB = """\
import os, signal, subprocess, sys, time
from pathlib import Path

PHASE = {phase!r}
REAL_ANALYZER = {real!r}

if PHASE == "conversation_analyzer" and "--write-fail-beacon" in sys.argv:
    os.execv(sys.executable, [sys.executable, REAL_ANALYZER] + sys.argv[1:])

marks = Path(os.environ["STUB_MARK_DIR"])
(marks / (PHASE + ".pid")).write_text(f"{{os.getpid()}} {{os.getppid()}} {{os.getpgrp()}}")
mode = os.environ.get("STUB_MODE_" + PHASE, "exit")
if mode == "block":
    time.sleep(300)
elif mode == "ignore_signals":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGABRT, signal.SIG_IGN)
    time.sleep(300)
elif mode == "grandchild":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    (marks / "grandchild.id").write_text(str(child.pid))
    time.sleep(300)
elif mode == "unflushed":
    print("unflushed line from " + PHASE)
    while not (marks / "release").exists():
        time.sleep(0.1)

beacon = os.environ.get("STUB_BEACON_" + PHASE)
if beacon:
    import argparse, importlib.util
    spec = importlib.util.spec_from_file_location("analyzer_cli", REAL_ANALYZER)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    error = None if beacon == "ok" else "stub analyzer error"
    cli._write_heartbeat(argparse.Namespace(max_sessions=50, dry_run=False), None, beacon, error)
sys.exit(int(os.environ.get("STUB_EXIT_" + PHASE, "0")))
"""

_HOLDER = """\
import signal, sys, time
from pathlib import Path

seen = Path(sys.argv[1])
def record(signum, frame):
    with seen.open("a") as fh:
        fh.write(f"{signum}\\n")
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGABRT,
            signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(sig, record)
Path(sys.argv[2]).write_text("ready")
while True:
    time.sleep(0.2)
"""


def _build_tree(tmp: Path, script_text: str) -> tuple[Path, Path]:
    scripts = tmp / "scripts"
    scripts.mkdir()
    (scripts / "lib").symlink_to(_REPO_ROOT / "scripts" / "lib")
    script = scripts / "conversation_analyzer_nightly.sh"
    script.write_text(script_text, encoding="utf-8")
    for phase in _PHASES:
        (scripts / f"{phase}.py").write_text(
            _STUB.format(phase=phase, real=str(_REAL_ANALYZER)), encoding="utf-8")
    # The script prefers <tree>/.venv/bin/python; point it at this interpreter so
    # the rig does not depend on which Pythons the machine has.
    venv_bin = tmp / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    # pgrep reports ollama as running, so the script never starts a real one.
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    pgrep = fakebin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pgrep.chmod(0o755)
    return script, fakebin


class Run:
    """One run of the script in a tmp tree; started in its own session."""

    def __init__(self, tmp_path: Path, script_text: str, env_extra: dict[str, str]):
        self.tmp = tmp_path
        self.script, fakebin = _build_tree(tmp_path, script_text)
        self.data = tmp_path / "data"
        self.state = self.data / "state"
        self.state.mkdir(parents=True)
        self.marks = tmp_path / "marks"
        self.marks.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        self.env = {
            "PATH": f"{fakebin}:{os.environ['PATH']}",
            "HOME": str(home),
            "VNX_DATA_DIR": str(self.data),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "VNX_STATE_DIR": str(self.state),
            "VNX_DATA_DIR_GUARD": "off",
            "STUB_MARK_DIR": str(self.marks),
        }
        self.env.update(env_extra)
        self.proc: subprocess.Popen | None = None
        self.stdout = ""
        self.stderr = ""

    @property
    def log(self) -> str:
        path = self.state / "conversation_analyzer.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @property
    def beacon(self) -> dict | None:
        path = self.data / "health" / "conversation_analyzer.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def start(self) -> "Run":
        self.proc = subprocess.Popen(
            ["/bin/bash", str(self.script)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return self

    def finish(self, window: float) -> int:
        """Wait at most ``window`` seconds. A script still running then is the
        failure under test: its group is killed and the test fails."""
        assert self.proc is not None
        try:
            self.stdout, self.stderr = self.proc.communicate(timeout=window)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.stdout, self.stderr = self.proc.communicate()
            pytest.fail(f"the nightly script was still running {window}s after it started")
        return self.proc.returncode

    def stub_ids(self, phase: str) -> tuple[int, int, int]:
        pid, ppid, pgrp = (self.marks / f"{phase}.pid").read_text().split()
        return int(pid), int(ppid), int(pgrp)

    def kill_leftovers(self) -> None:
        """Stubs lead their own groups, out of reach of the script's group, and
        a grandchild may have left its stub's group: end what is still there."""
        for mark in self.marks.glob("*.pid"):
            pid, _ppid, pgrp = (int(v) for v in mark.read_text().split())
            try:
                if pgrp == pid != os.getpgrp() and os.getpgid(pid) == pgrp:
                    os.killpg(pgrp, signal.SIGKILL)
            except ProcessLookupError:
                pass
        grandchild = self.marks / "grandchild.id"
        if grandchild.exists() and _alive(int(grandchild.read_text())):
            os.kill(int(grandchild.read_text()), signal.SIGKILL)
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.communicate()


@pytest.fixture
def run_script(tmp_path):
    runs: list[Run] = []

    def _start(script_text: str | None = None, **env: str) -> Run:
        run = Run(tmp_path, script_text or _SCRIPT.read_text(encoding="utf-8"), env)
        runs.append(run)
        return run.start()

    yield _start
    for run in runs:
        run.kill_leftovers()


def _run(tmp_path: Path, script_text: str, **stub_exits: int) -> tuple[subprocess.CompletedProcess, str]:
    run = Run(tmp_path, script_text, {f"STUB_EXIT_{k}": str(v) for k, v in stub_exits.items()})
    run.start()
    try:
        rc = run.finish(60)
    finally:
        run.kill_leftovers()
    proc = subprocess.CompletedProcess(run.script, rc, run.stdout, run.stderr)
    return proc, run.log


def _script_with_missing_function() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    assert _PRELUDE_END in text
    return text.replace(_PRELUDE_END, "ensure_env_that_does_not_exist\n\n" + _PRELUDE_END, 1)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_missing_function_after_prelude_fails_the_job(tmp_path):
    proc, log = _run(tmp_path, _script_with_missing_function())
    assert proc.returncode != 0
    assert COMPLETE not in log
    assert "command not found" in proc.stderr


def test_normal_run_completes(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"))
    assert proc.returncode == 0, proc.stderr
    assert "ensure_env" not in proc.stderr
    assert COMPLETE in log
    for n in ("Phase 0 complete", "Phase 1 complete", "Phase 2 complete", "Phase 3 complete"):
        assert n in log


def test_script_relies_on_resolver_preserving_errexit():
    """No local `set -e` re-assert after the source.

    That workaround existed only because vnx_paths.sh used to lose errexit in
    the caller. The resolver now restores the caller's strict mode exactly
    (regression-tested in tests/test_vnx_paths_shellopts.py), so a re-assert here
    would mask a regression in the resolver. The behavior it protected is still
    covered by test_missing_function_after_prelude_fails_the_job.
    """
    after_source = _SCRIPT.read_text(encoding="utf-8").split(_SOURCE_LINE, 1)[1]
    prelude = after_source.split("VNX_PYTHON=", 1)[0]
    commands = [ln for ln in prelude.splitlines() if not ln.lstrip().startswith("#")]
    assert all("set -e" not in ln for ln in commands)


def test_phase0_failure_aborts_without_complete_line(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), quality_db_init=1)
    assert proc.returncode != 0
    assert "Phase 0 FAILED" in log
    assert COMPLETE not in log
    assert "Phase 1" not in log
    beacon = json.loads((tmp_path / "data" / "health" / "conversation_analyzer.json").read_text())
    assert beacon["status"] == "fail"
    assert beacon["details"]["error"] == "Phase 0 exited 1"


def test_analyzer_failure_exits_nonzero_without_complete_line(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), conversation_analyzer=3)
    assert proc.returncode == 3
    assert "Phase 1 FAILED (exit=3)" in log
    assert "Phase 1 complete" not in log
    assert COMPLETE not in log
    assert FAILED in log


def test_non_fatal_phase_failure_still_completes(tmp_path):
    proc, log = _run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), generate_suggested_edits=1)
    assert proc.returncode == 0, proc.stderr
    assert "Phase 3 WARNING" in log
    assert COMPLETE in log


def test_every_phase_runs_through_the_bounded_function():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "| tee" not in text
    for label in ("Phase 0", "Phase 1", "Phase 1.5", "Phase 2", "Phase 2.5",
                  "Phase 2.6", "Phase 3", "Phase 4"):
        assert f'run_phase "{label}" ' in text, label


def _assert_later_phases_ran(log: str, after: str) -> None:
    tail = log[log.index(after):]
    for n in ("Phase 1.5 complete", "Phase 2 complete", "Phase 2.5 complete",
              "Phase 2.6 complete", "Phase 3 complete", "Phase 4: Skipped"):
        assert n in tail, n


# 8a
def test_phase1_hang_is_bounded_and_reported(run_script):
    run = run_script(STUB_MODE_conversation_analyzer="block",
                     VNX_ANALYZER_PHASE1_TIMEOUT=str(LIMIT))
    rc = run.finish(WINDOW)
    log = run.log
    timeout_line = f"Phase 1 TIMEOUT: still running after {LIMIT}s"
    assert timeout_line in log
    # SIGABRT first: the fault handler put the stub's traceback in the log.
    assert "Fatal Python error: Aborted" in log
    assert "Phase 1 FAILED (exit=124)" in log
    _assert_later_phases_ran(log, timeout_line)
    assert run.beacon["status"] == "fail"
    assert run.beacon["details"]["error"] == f"Phase 1 timed out after {LIMIT}s"
    assert rc == 124
    assert FAILED in log and COMPLETE not in log


# 8b
def test_phase1_ignoring_term_and_abort_is_killed(run_script):
    run = run_script(STUB_MODE_conversation_analyzer="ignore_signals",
                     VNX_ANALYZER_PHASE1_TIMEOUT=str(LIMIT))
    rc = run.finish(WINDOW)
    log = run.log
    timeout_line = f"Phase 1 TIMEOUT: still running after {LIMIT}s"
    assert timeout_line in log
    assert "(exit status 137)" in log  # ended by SIGKILL, not by SIGABRT
    _assert_later_phases_ran(log, timeout_line)
    assert run.beacon["status"] == "fail"
    assert run.beacon["details"]["error"] == f"Phase 1 timed out after {LIMIT}s"
    assert rc == 124
    stub_pid = run.stub_ids("conversation_analyzer")[0]
    assert not _alive(stub_pid)


# 8c
def test_grandchild_holding_the_output_open_does_not_hold_the_run(run_script):
    run = run_script(STUB_MODE_conversation_analyzer="grandchild",
                     VNX_ANALYZER_PHASE1_TIMEOUT=str(LIMIT))
    rc = run.finish(WINDOW)
    grandchild = int((run.marks / "grandchild.id").read_text())
    # It left the stub's group, so the kill missed it, and it still has the
    # phase's output open: the run returned without waiting for it. The
    # fixture ends it afterwards.
    assert _alive(grandchild)
    assert f"Phase 1 TIMEOUT: still running after {LIMIT}s" in run.log
    assert rc == 124


# 8d
def test_unflushed_line_reaches_the_log_before_the_limit(run_script):
    run = run_script(STUB_MODE_conversation_analyzer="unflushed",
                     VNX_ANALYZER_PHASE1_TIMEOUT="60")
    line = "unflushed line from conversation_analyzer"
    seen_while_running = False
    give_up = time.monotonic() + 45
    while time.monotonic() < give_up and run.proc.poll() is None:
        log = run.log
        if line in log:
            seen_while_running = "TIMEOUT" not in log
            break
        time.sleep(0.1)
    (run.marks / "release").write_text("go")
    rc = run.finish(WINDOW)
    assert seen_while_running, "the stub's line was not in the log while the phase still ran"
    assert rc == 0
    assert COMPLETE in run.log


# 8e
def test_quick_phase1_exits_zero_and_keeps_the_ok_beacon(run_script):
    run = run_script(STUB_BEACON_conversation_analyzer="ok")
    rc = run.finish(WINDOW)
    assert rc == 0, run.stderr
    assert run.beacon["status"] == "ok"
    assert "error" not in run.beacon["details"]
    assert "fail beacon" not in run.log
    assert COMPLETE in run.log


# 8f
def test_later_phase_hang_is_bounded_and_the_run_continues(run_script):
    run = run_script(STUB_MODE_learning_loop_nightly="block",
                     STUB_BEACON_conversation_analyzer="ok",
                     VNX_ANALYZER_PHASE_TIMEOUT=str(LIMIT))
    rc = run.finish(WINDOW)
    log = run.log
    timeout_line = f"Phase 2.6 TIMEOUT: still running after {LIMIT}s"
    assert timeout_line in log
    assert "Phase 3 complete" in log[log.index(timeout_line):]
    assert "Phase 1 complete (exit=0)" in log
    assert run.beacon["status"] == "fail"
    assert run.beacon["details"]["error"] == f"Phase 2.6 timed out after {LIMIT}s"
    assert rc == 124
    assert FAILED in log and COMPLETE not in log


# 8g
def test_live_lock_holder_is_reported_as_failure_and_left_alone(tmp_path):
    seen = tmp_path / "holder_signals"
    ready = tmp_path / "holder_ready"
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(seen), str(ready)])
    try:
        give_up = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < give_up:
            time.sleep(0.05)
        assert ready.exists()
        run = Run(tmp_path, _SCRIPT.read_text(encoding="utf-8"), {})
        lock = run.state / "conversation_analyzer.lock"
        lock.write_text(f"{holder.pid}\n")
        two_hours_ago = time.time() - 2 * 3600
        os.utime(lock, (two_hours_ago, two_hours_ago))
        rc = run.start().finish(WINDOW)
        log = run.log
        assert rc != 0
        assert f"Already running: another run still holds the lock (PID {holder.pid}, lock age 2h0" in log
        assert "Phase 0" not in log
        assert run.beacon["status"] == "fail"
        error = run.beacon["details"]["error"]
        assert f"PID {holder.pid}" in error and "lock age 2h0" in error
        assert holder.poll() is None
        assert not seen.exists() or seen.read_text() == ""
        assert lock.read_text().strip() == str(holder.pid)
    finally:
        holder.kill()
        holder.wait()


# 8h
def test_group_kill_leaves_the_script_that_started_the_stub_alive(run_script):
    run = run_script(STUB_MODE_conversation_analyzer="ignore_signals",
                     VNX_ANALYZER_PHASE1_TIMEOUT=str(LIMIT))
    rc = run.finish(WINDOW)
    pid, ppid, pgrp = run.stub_ids("conversation_analyzer")
    assert ppid == run.proc.pid  # the script started the stub
    assert pgrp == pid  # the stub led its own group
    assert pgrp != run.proc.pid  # which is not the script's group
    # The script outlived both signals to the stub's group: it ran on and ended
    # with its own exit status, not by a signal.
    assert rc == 124
    log = run.log
    assert FAILED in log[log.index("Phase 1 TIMEOUT"):]


def test_nonzero_phase1_without_its_own_beacon_writes_a_fail_beacon(run_script):
    run = run_script(STUB_EXIT_conversation_analyzer="3")
    rc = run.finish(WINDOW)
    assert rc == 3
    assert run.beacon["status"] == "fail"
    assert run.beacon["details"]["error"] == "Phase 1 exited 3"


def test_nonzero_phase1_keeps_the_beacon_it_wrote_itself(run_script):
    run = run_script(STUB_EXIT_conversation_analyzer="3", STUB_BEACON_conversation_analyzer="fail")
    rc = run.finish(WINDOW)
    assert rc == 3
    assert "fail beacon not written: this run's beacon is present (status=fail)" in run.log
    assert run.beacon["details"]["error"] == "stub analyzer error"


def test_invalid_limit_falls_back_to_the_default(run_script):
    run = run_script(VNX_ANALYZER_PHASE_TIMEOUT="soon", VNX_ANALYZER_PHASE1_TIMEOUT="0")
    rc = run.finish(WINDOW)
    assert rc == 0, run.stderr
    assert "WARNING: VNX_ANALYZER_PHASE_TIMEOUT='soon' is not a whole number of seconds; using 900" in run.log
    assert "WARNING: VNX_ANALYZER_PHASE1_TIMEOUT=0 would end every phase at once; using 1800" in run.log


def _analyzer_cli(data: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    """The real analyzer CLI with a tmp HOME and the store at ``data``."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "VNX_DATA_DIR": str(data),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(data / "state"),
        "VNX_DATA_DIR_GUARD": "off",
    }
    return subprocess.run([sys.executable, str(_REAL_ANALYZER), *args],
                          env=env, capture_output=True, text=True, timeout=60)


def test_fail_beacon_flag_writes_and_keeps(tmp_path):
    data = tmp_path / "data"

    def cli(*args: str) -> subprocess.CompletedProcess:
        return _analyzer_cli(data, tmp_path, *args)

    beacon_path = data / "health" / "conversation_analyzer.json"
    assert cli("--write-fail-beacon", "first").returncode == 0
    assert json.loads(beacon_path.read_text())["details"]["error"] == "first"
    kept = cli("--write-fail-beacon", "second", "--unless-beacon-since", "0")
    assert kept.returncode == 0, kept.stderr
    assert "this run's beacon is present (status=fail)" in kept.stdout
    assert json.loads(beacon_path.read_text())["details"]["error"] == "first"
    future = str(int(time.time()) + 3600)
    assert cli("--write-fail-beacon", "third", "--unless-beacon-since", future).returncode == 0
    assert json.loads(beacon_path.read_text())["details"]["error"] == "third"
    lone = cli("--unless-beacon-since", "0")
    assert lone.returncode == 2
    assert "--unless-beacon-since requires --write-fail-beacon" in lone.stderr


def _seed_old_ok_beacon(health: Path) -> dict:
    """Last night's `ok` beacon, as the analyzer left it a day ago."""
    health.mkdir(parents=True, exist_ok=True)
    yesterday = int(time.time()) - 86400
    beacon = {
        "component": "conversation_analyzer",
        "last_run_ts": yesterday,
        "last_run_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(yesterday)),
        "status": "ok",
        "details": {"max_sessions": 50, "dry_run": False},
        "expected_interval_seconds": 86400,
    }
    (health / "conversation_analyzer.json").write_text(json.dumps(beacon), encoding="utf-8")
    return beacon


def _health_is_a_file(health: Path) -> dict | None:
    # The beacon's directory cannot be created: _write_heartbeat catches and logs.
    health.parent.mkdir(parents=True, exist_ok=True)
    health.write_text("not a directory", encoding="utf-8")
    return None


def _tmp_sibling_is_a_directory(health: Path) -> dict | None:
    # The atomic write's tmp file cannot be opened: HealthBeacon.heartbeat
    # swallows that without a word, and last night's `ok` stays in place.
    old = _seed_old_ok_beacon(health)
    (health / "conversation_analyzer.json.tmp").mkdir()
    return old


# OI-2021 round 2: the CLI used to print "fail beacon written" and exit 0 here.
@pytest.mark.parametrize("make_unwritable", [_health_is_a_file, _tmp_sibling_is_a_directory])
def test_fail_beacon_flag_exits_nonzero_when_the_beacon_does_not_land(tmp_path, make_unwritable):
    data = tmp_path / "data"
    health = data / "health"
    old = make_unwritable(health)

    plain = _analyzer_cli(data, tmp_path, "--write-fail-beacon", "Phase 1 timed out after 3s")
    assert plain.returncode != 0
    assert "fail beacon NOT written" in plain.stderr
    assert "Phase 1 timed out after 3s" in plain.stderr
    assert "fail beacon written" not in plain.stdout

    # No beacon of this run to keep, so the write is attempted and fails too.
    since = _analyzer_cli(data, tmp_path, "--write-fail-beacon", "Phase 1 exited 3",
                          "--unless-beacon-since", str(int(time.time())))
    assert since.returncode != 0
    assert "fail beacon NOT written" in since.stderr

    if old is not None:
        on_disk = json.loads((health / "conversation_analyzer.json").read_text())
        assert on_disk == old


def test_fail_beacon_flag_refuses_a_same_second_beacon_it_did_not_write(tmp_path):
    """A `fail` beacon with the same reason, stamped this second by an earlier
    call, is not proof that this call wrote one."""
    data = tmp_path / "data"
    health = data / "health"
    assert _analyzer_cli(data, tmp_path, "--write-fail-beacon", "same reason").returncode == 0
    (health / "conversation_analyzer.json.tmp").mkdir()
    again = _analyzer_cli(data, tmp_path, "--write-fail-beacon", "same reason")
    assert again.returncode != 0
    assert "fail beacon NOT written" in again.stderr


def test_overrun_with_an_unwritable_beacon_location_is_logged_and_fails(tmp_path):
    run = Run(tmp_path, _SCRIPT.read_text(encoding="utf-8"),
              {"STUB_MODE_conversation_analyzer": "block",
               "VNX_ANALYZER_PHASE1_TIMEOUT": str(LIMIT)})
    old = _tmp_sibling_is_a_directory(run.data / "health")
    try:
        rc = run.start().finish(WINDOW)
    finally:
        run.kill_leftovers()
    log = run.log
    reason = f"Phase 1 timed out after {LIMIT}s"
    assert f"Phase 1 TIMEOUT: still running after {LIMIT}s" in log
    assert f"ERROR: fail beacon NOT written (exit 1), reason it carried: {reason}" in log
    assert f"{reason}; fail beacon not written (exit 1)" in log[log.index(FAILED):]
    assert COMPLETE not in log
    assert rc != 0
    # The write could not land, so last night's `ok` is still on disk: the log
    # line and the exit status are what report this night.
    assert run.beacon == old


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch):
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
