"""tests/test_cost_ledger_tick.py — the spool replay in the dispatcher prelude, the beacon and the known files (K2a-3c).

Track ``kosten-per-model-in-het-grootboek``, plan ``k2a3-spool-een-lock.md``
§ Acceptatie items 12 and 13, ADR-043 §6:

12. a threshold file that cannot be written, or a replay that hangs: the tick
    returns 0 after at most its hard timeout and the dispatcher goes on;
13. the ``cost-ledger`` beacon is degraded while the spool is not empty or not
    readable, ok after.

Plus the rest of the tick's contract (it runs in legacy mode too, logs every
failure, and with an empty spool its subprocess writes nothing), and the
cost ledger's own files as known files in both ``vnx doctor`` entrances and
the footprint probe.

The tick is the real ``dispatcher_supervisor_ticks.sh`` sourced in bash under
``set -euo pipefail``, and the replay it starts is the real
``scripts/cost_ledger_replay.py``. A hang is a real child process holding the
real lock file; nothing mocks the timeout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
TICKS_SH = _LIB / "dispatcher_supervisor_ticks.sh"
DISPATCHER_SH = REPO_ROOT / "scripts" / "dispatcher_minimal.sh"
_SCRIPT = REPO_ROOT / "scripts" / "cost_ledger_replay.py"
for _path in (str(REPO_ROOT), str(_LIB), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger

PROJECT = "vnx-dev"
RUN_A1 = f"{PROJECT}:dispatch:20261021-kp-tick:1"
_ZERO = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}
# A replay that hangs is killed after this many seconds in the tests (the tick allows 1 to 20).
TEST_TIMEOUT_SECONDS = 2
# What the tick may take beyond the replay's timeout: starting bash, python and the kill.
TICK_MARGIN_SECONDS = 3.0


def _record(**overrides) -> dict:
    fields = dict(
        run_id=RUN_A1, project_id=PROJECT, role="builder", provider="deepseek-harness", model="deepseek-v4-pro",
        occurred_from="2026-10-21T07:00:00Z", occurred_to="2026-10-21T07:30:00Z",
        tokens={**_ZERO, "input_uncached": 1000, "output": 200}, usage_completeness="complete",
        billing="metered", cost_usd=0.5, cost_source="provider_reported",
    )
    fields.update(overrides)
    return cost_ledger.build_record(cost_ledger.RunCost(**fields))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    (path / "state").mkdir(parents=True)
    return path


def _ledger(store: Path) -> Path:
    return store / "events" / "provider_costs.ndjson"


def _spool_dir(store: Path) -> Path:
    return store / "events" / "provider_costs.spool"


def _spool(store: Path, record: dict) -> Path:
    """A spool file holding ``record``, as the writer leaves it after its ledger failed."""
    path = cost_ledger.spool_file_for(_spool_dir(store), record["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([record], separators=(",", ":")), encoding="utf-8")
    return path


def _lines(store: Path) -> list:
    ledger = _ledger(store)
    if not ledger.is_file():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def _beacon(store: Path) -> dict:
    return json.loads((store / "health" / "cost-ledger.json").read_text(encoding="utf-8"))


def _tree(root: Path) -> dict:
    """Every path under ``root`` with its bytes, a directory as ``None``."""
    return {str(path.relative_to(root)): (None if path.is_dir() else path.read_bytes())
            for path in sorted(root.rglob("*"))}


def _env(store: Path, tmp_path: Path, **extra: str) -> dict:
    env = {key: value for key, value in os.environ.items() if key != "VNX_SUPERVISOR_MODE"}
    env.update(
        STATE_DIR=str(store / "state"), VNX_STATE_DIR=str(store / "state"), VNX_DATA_DIR=str(store),
        VNX_DATA_DIR_EXPLICIT="1", VNX_LOGS_DIR=str(tmp_path / "logs"), VNX_DIR=str(REPO_ROOT),
        VNX_LOG_CAPTURE=str(tmp_path / "dispatcher.log"), VNX_COST_LEDGER_REPLAY_INTERVAL_SEC="0",
    )
    env.update(extra)
    return env


def _run_bash(body: str, env: dict, timeout: float = 60) -> subprocess.CompletedProcess:
    """Source the real ticks file and run ``body`` under ``set -euo pipefail``; SURVIVED proves the caller goes on."""
    script = textwrap.dedent(f"""\
        set -euo pipefail
        log() {{ printf '[log] %s\\n' "$*" >> "$VNX_LOG_CAPTURE"; }}
        source "{TICKS_SH}"
        {body}
        echo "SURVIVED:$?"
        """)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=timeout)


def _dispatcher_log(tmp_path: Path) -> str:
    path = tmp_path / "dispatcher.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


# Holds the lock file exclusively until killed; argv: the lock path, the marker to write once held.
_LOCK_HOLDER = textwrap.dedent("""
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    open(sys.argv[2], "w").write("1")
    time.sleep(120)
""")


@pytest.fixture
def hold_the_lock_file(tmp_path):
    """``hold(store)`` starts a child holding ``provider_costs.lock`` and returns it once it holds the lock."""
    holders: list = []

    def hold(store: Path) -> subprocess.Popen:
        marker = tmp_path / f"holds-{len(holders)}"
        (store / "events").mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen([sys.executable, "-c", _LOCK_HOLDER, str(store / "events" / "provider_costs.lock"),
                                  str(marker)], stderr=subprocess.DEVNULL)
        holders.append(child)
        deadline = time.monotonic() + 30
        while not marker.exists():
            assert child.poll() is None, "the holder died before it took the lock file"
            assert time.monotonic() < deadline, "the holder never took the lock file"
            time.sleep(0.01)
        return child

    yield hold
    for child in holders:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)


# ---------------------------------------------------------------------------
# Item 12: the tick
# ---------------------------------------------------------------------------


class TestTheTick:
    def test_a_spooled_run_is_replayed_by_the_tick_in_legacy_mode(self, store, tmp_path):
        """ADR-043 §6: the spool is the ledger's recovery path, not supervision; no VNX_SUPERVISOR_MODE needed."""
        record = _record()
        spooled = _spool(store, record)

        done = _run_bash("_maybe_cost_ledger_replay", _env(store, tmp_path, VNX_COST_LEDGER_TICK_TIMEOUT_SEC="17"))

        assert done.returncode == 0 and "SURVIVED:0" in done.stdout, done.stderr
        left = subprocess.run(["pgrep", "-fx", "sleep 17"], capture_output=True, text=True).stdout.split()
        assert left == [], "the watchdog's timer is stopped once the replay ends"
        assert _lines(store) == [record] and not spooled.exists()
        assert (store / "state" / ".last_cost_ledger_replay_ts").read_text(encoding="utf-8").isdigit()
        assert "1 spool files replayed" in (tmp_path / "logs" / "cost_ledger_replay.log").read_text(encoding="utf-8")

    def test_with_an_empty_spool_the_replay_writes_nothing(self, store, tmp_path):
        """The tick's subprocess creates no lock file, no ledger, no beacon: only the tick's own threshold file."""
        (store / "events").mkdir()
        before = _tree(store)

        done = _run_bash("_maybe_cost_ledger_replay", _env(store, tmp_path))

        assert "SURVIVED:0" in done.stdout, done.stderr
        after = _tree(store)
        after.pop("state/.last_cost_ledger_replay_ts")
        assert after == before

    def test_a_hanging_replay_is_killed_after_the_timeout_and_the_tick_returns_0(
        self, store, tmp_path, hold_the_lock_file,
    ):
        """Acceptance 12: the real replay waits on a real held lock; the tick kills it and the dispatcher goes on."""
        spooled = _spool(store, _record())
        holder = hold_the_lock_file(store)
        env = _env(store, tmp_path, VNX_COST_LEDGER_TICK_TIMEOUT_SEC=str(TEST_TIMEOUT_SECONDS),
                   VNX_COST_LEDGER_LOCK_WAIT_SEC="60", VNX_COST_LEDGER_REPLAY_BUDGET_SEC="60")

        started = time.monotonic()
        done = _run_bash("_maybe_cost_ledger_replay", env)
        waited = time.monotonic() - started

        assert done.returncode == 0 and "SURVIVED:0" in done.stdout, done.stderr
        assert waited < TEST_TIMEOUT_SECONDS + TICK_MARGIN_SECONDS, f"the tick took {waited:.1f}s"
        assert f"killed after its hard timeout of {TEST_TIMEOUT_SECONDS}s" in _dispatcher_log(tmp_path)
        holder.kill()
        holder.wait(timeout=30)
        time.sleep(1.0)
        assert spooled.exists() and _lines(store) == [], "the replay is gone: it did not go on once the lock was free"

    @pytest.mark.parametrize("value, limit", [
        (None, "20"), ("20", "20"), ("5", "5"), ("1", "1"), ("60", "20"), ("0", "20"), ("-3", "20"), ("abc", "20"),
    ])
    def test_the_hard_timeout_is_20_seconds_and_can_only_be_lowered(self, store, tmp_path, value, limit):
        env = _env(store, tmp_path)
        env.pop("VNX_COST_LEDGER_TICK_TIMEOUT_SEC", None)
        if value is not None:
            env["VNX_COST_LEDGER_TICK_TIMEOUT_SEC"] = value

        done = _run_bash('printf "LIMIT:%s\\n" "$(_cost_ledger_tick_timeout)"', env)

        assert f"LIMIT:{limit}" in done.stdout, done.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
    def test_a_threshold_file_that_cannot_be_written_is_logged_and_the_tick_returns_0(self, store, tmp_path):
        """Acceptance 12: the replay still runs, and the dispatcher process throttles the tick in memory."""
        first = _spool(store, _record())
        later = _record(run_id=f"{PROJECT}:dispatch:20261021-kp-tick:2")
        staged = tmp_path / "later.json"
        staged.write_text(json.dumps([later], separators=(",", ":")), encoding="utf-8")
        later_file = cost_ledger.spool_file_for(_spool_dir(store), later["run_id"])
        (store / "state").chmod(0o555)
        try:
            done = _run_bash(
                f'_maybe_cost_ledger_replay\ncp "{staged}" "{later_file}"\n_maybe_cost_ledger_replay',
                _env(store, tmp_path, VNX_COST_LEDGER_REPLAY_INTERVAL_SEC="3600"),
            )
        finally:
            (store / "state").chmod(0o755)

        assert done.returncode == 0 and "SURVIVED:0" in done.stdout, done.stderr
        assert "threshold file" in _dispatcher_log(tmp_path) and "cannot be written" in _dispatcher_log(tmp_path)
        assert not first.exists() and [line["run_id"] for line in _lines(store)] == [RUN_A1]
        assert later_file.exists(), "the second call was throttled in memory"
        assert not (store / "state" / ".last_cost_ledger_replay_ts").exists()

    def test_a_log_file_that_cannot_be_written_is_logged_and_the_replay_still_runs(self, store, tmp_path):
        spooled = _spool(store, _record())
        not_a_directory = tmp_path / "logs-is-a-file"
        not_a_directory.write_text("", encoding="utf-8")

        done = _run_bash("_maybe_cost_ledger_replay", _env(store, tmp_path, VNX_LOGS_DIR=str(not_a_directory)))

        assert done.returncode == 0 and "SURVIVED:0" in done.stdout, done.stderr
        assert "cannot be written; the replay runs without it" in _dispatcher_log(tmp_path)
        assert not spooled.exists() and len(_lines(store)) == 1

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without read permission")
    def test_a_replay_that_fails_is_logged_with_its_exit_code(self, store, tmp_path):
        _spool_dir(store).mkdir(parents=True)
        _spool_dir(store).chmod(0)
        try:
            done = _run_bash("_maybe_cost_ledger_replay", _env(store, tmp_path))
        finally:
            _spool_dir(store).chmod(0o755)

        assert done.returncode == 0 and "SURVIVED:0" in done.stdout, done.stderr
        assert "the spool replay exited 1" in _dispatcher_log(tmp_path)

    def test_the_interval_throttles_the_tick(self, store, tmp_path):
        (store / "state" / ".last_cost_ledger_replay_ts").write_text(str(int(time.time())), encoding="utf-8")
        spooled = _spool(store, _record())

        done = _run_bash("_maybe_cost_ledger_replay", _env(store, tmp_path, VNX_COST_LEDGER_REPLAY_INTERVAL_SEC="3600"))

        assert "SURVIVED:0" in done.stdout, done.stderr
        assert spooled.exists() and _lines(store) == []

    def test_process_dispatches_runs_the_tick_outside_the_supervisor_mode_gate(self):
        """The prelude calls it; its body never reads VNX_SUPERVISOR_MODE (the legacy test above runs it)."""
        dispatcher = DISPATCHER_SH.read_text(encoding="utf-8")
        start = dispatcher.index("process_dispatches() {")
        prelude = dispatcher[start:dispatcher.index('for dispatch in "$PENDING_DIR"/*.md', start)]
        assert "_maybe_cost_ledger_replay" in prelude
        ticks = TICKS_SH.read_text(encoding="utf-8")
        body = ticks[ticks.index("_maybe_cost_ledger_replay() {"):]
        assert "VNX_SUPERVISOR_MODE" not in body

    def test_the_ticks_file_passes_bash_syntax_check(self):
        assert subprocess.run(["bash", "-n", str(TICKS_SH)], capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Item 13: the beacon
# ---------------------------------------------------------------------------


def _replay_cli(store: Path, **extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, VNX_DATA_DIR=str(store), VNX_DATA_DIR_EXPLICIT="1", VNX_STATE_DIR=str(store / "state"),
               **extra)
    return subprocess.run([sys.executable, str(_SCRIPT), "--json"], env=env, capture_output=True, text=True,
                          timeout=60)


class TestTheBeacon:
    def test_degraded_while_a_file_waits_and_ok_once_the_spool_is_empty(self, store):
        """Acceptance 13: the batch bound leaves a file and the beacon degraded; the next replay empties it: ok."""
        _spool(store, _record())
        _spool(store, _record(run_id=f"{PROJECT}:dispatch:20261021-kp-tick:2"))

        bounded = _replay_cli(store, VNX_COST_LEDGER_REPLAY_BATCH="1")

        assert bounded.returncode == 0 and json.loads(bounded.stdout)["files_remaining"] == 1, bounded.stderr
        beacon = _beacon(store)
        assert (beacon["component"], beacon["status"], beacon["expected_interval_seconds"]) == (
            "cost-ledger", "stale", None)
        assert beacon["details"] == {
            "state": "degraded", "spool_files": 1, "spool_pending": 1, "spool_unreadable": False,
            "spool_quarantined": 0, "spool_conflicts": 0, "reason": beacon["details"]["reason"],
        }
        assert "holds 1 files (1 runs waiting" in beacon["details"]["reason"]

        done = _replay_cli(store)

        assert done.returncode == 0, done.stderr
        assert not list(_spool_dir(store).glob("*.json")) and len(_lines(store)) == 2
        beacon = _beacon(store)
        assert beacon["status"] == "ok"
        assert beacon["details"] == {
            "state": "ok", "spool_files": 0, "spool_pending": 0, "spool_unreadable": False,
            "spool_quarantined": 0, "spool_conflicts": 0,
        }

    def test_a_held_lock_file_never_makes_the_beacon_hang(self, store, hold_the_lock_file):
        """The replay is lock_busy within its wait, and the beacon's shared lock is bounded by the same wait."""
        spooled = _spool(store, _record())
        hold_the_lock_file(store)

        started = time.monotonic()
        busy = _replay_cli(store, VNX_COST_LEDGER_LOCK_WAIT_SEC="0.3")
        waited = time.monotonic() - started

        assert busy.returncode == 3, busy.stderr
        assert waited < 0.3 * 2 + TICK_MARGIN_SECONDS, f"it took {waited:.1f}s"
        assert "the cost-ledger beacon was not written this time" in busy.stderr
        assert spooled.exists() and not (store / "health" / "cost-ledger.json").exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without read permission")
    def test_degraded_while_the_spool_cannot_be_read(self, store):
        _spool(store, _record())
        _spool_dir(store).chmod(0)
        try:
            refused = _replay_cli(store)
        finally:
            _spool_dir(store).chmod(0o755)

        assert refused.returncode == 1, refused.stderr
        assert (_beacon(store)["status"], _beacon(store)["details"]["spool_unreadable"]) == ("stale", True)
        assert _beacon(store)["details"]["state"] == "degraded"

        assert _replay_cli(store).returncode == 0
        assert _beacon(store)["details"]["state"] == "ok"

    def test_degraded_while_a_file_is_in_quarantine_and_ok_once_a_person_removed_it(self, store):
        corrupt = cost_ledger.spool_file_for(_spool_dir(store), RUN_A1)
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("not json", encoding="utf-8")

        for _call in range(2):
            assert _replay_cli(store).returncode == 0
            details = _beacon(store)["details"]
            assert (details["state"], details["spool_quarantined"], details["spool_files"]) == ("degraded", 1, 0)

        (_spool_dir(store) / "quarantine" / corrupt.name).unlink()
        assert _replay_cli(store).returncode == 0
        assert _beacon(store)["details"]["state"] == "ok"

    def test_an_empty_spool_and_an_ok_beacon_write_nothing(self, store):
        """Event-driven: no beacon is owed while nothing happens, and an ok one is left as it is."""
        assert cost_ledger.report_spool_health(_ledger(store)) is None
        assert not (store / "health").exists() and not (store / "events").exists()
        _spool(store, _record())
        assert _replay_cli(store).returncode == 0
        assert not (store / "health" / "cost-ledger.json").exists(), "drained in one call: nothing to report"

        (store / "health").mkdir()
        (store / "health" / "cost-ledger.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        before = (store / "health" / "cost-ledger.json").read_bytes()
        assert cost_ledger.report_spool_health(_ledger(store)) is None
        assert (store / "health" / "cost-ledger.json").read_bytes() == before

    @pytest.mark.parametrize("on_disk", ['{"status": "stale"}', "not json"])
    def test_a_beacon_that_is_not_ok_is_set_to_ok_on_an_empty_spool(self, store, on_disk):
        (store / "health").mkdir()
        (store / "health" / "cost-ledger.json").write_text(on_disk, encoding="utf-8")

        assert cost_ledger.report_spool_health(_ledger(store)) == "ok"

        assert (_beacon(store)["status"], _beacon(store)["details"]["state"]) == ("ok", "ok")

    def test_a_beacon_that_cannot_be_written_is_logged_and_keeps_the_exit_code(self, store):
        _spool(store, _record())
        _spool(store, _record(run_id=f"{PROJECT}:dispatch:20261021-kp-tick:2"))
        (store / "health").write_text("a file where the health directory goes", encoding="utf-8")

        bounded = _replay_cli(store, VNX_COST_LEDGER_REPLAY_BATCH="1")

        assert bounded.returncode == 0, bounded.stderr
        assert "the cost-ledger beacon could not be written" in bounded.stderr
        assert json.loads(bounded.stdout)["stopped"] == "batch"

    def test_the_beacon_is_written_under_the_shared_lock_file(self, store, hold_the_lock_file):
        """It counts and writes in one scope under LOCK_SH: a writer holding the lock file keeps it waiting."""
        _spool(store, _record())
        holder = hold_the_lock_file(store)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import cost_ledger; "
             "from pathlib import Path; print(cost_ledger.report_spool_health(Path(sys.argv[2])))",
             str(_LIB), str(_ledger(store))], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, VNX_COST_LEDGER_LOCK_WAIT_SEC="20"))
        time.sleep(1.0)
        assert child.poll() is None, "it wrote the beacon while an exclusive holder had the lock file"
        assert not (store / "health" / "cost-ledger.json").exists()
        holder.kill()
        holder.wait(timeout=30)
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        assert out.strip() == "degraded" and _beacon(store)["details"]["spool_pending"] == 1


# ---------------------------------------------------------------------------
# The cost ledger's own files: vnx doctor (both entrances) and the footprint probe
# ---------------------------------------------------------------------------


def _a_store_with_every_known_file(store: Path) -> Path:
    """The ledger, the lock file, a spool file, an interrupted spool write and a quarantined file."""
    events = store / "events"
    events.mkdir(parents=True, exist_ok=True)
    _ledger(store).write_bytes(b"{}\n")
    (events / "provider_costs.lock").touch()
    _spool(store, _record())
    (_spool_dir(store) / "tmpab12cd.tmp").write_text("[", encoding="utf-8")
    (_spool_dir(store) / "quarantine").mkdir()
    (_spool_dir(store) / "quarantine" / "x~0000000000000000.json").write_text("not json", encoding="utf-8")
    return events


def _doctor_paths(store: Path) -> dict:
    return {"VNX_DATA_DIR": str(store), "VNX_STATE_DIR": str(store / "state")}


class TestTheKnownFiles:
    def test_ledger_files_names_every_known_file_and_no_stray(self, store):
        events = _a_store_with_every_known_file(store)

        files = cost_ledger.ledger_files(_ledger(store))

        assert (files.ledger, files.lock_file) == (True, True)
        assert (files.spool_files, files.spool_temp_files, files.quarantined) == (1, 1, 1)
        assert (files.strays, files.unlistable) == ((), ())
        sizes = [_ledger(store).stat().st_size, (events / "provider_costs.lock").stat().st_size]
        sizes += [path.stat().st_size for path in _spool_dir(store).rglob("*") if path.is_file()]
        assert files.bytes == sum(sizes)

    def test_an_entry_of_the_spool_that_is_none_of_its_own_is_a_stray(self, store):
        _a_store_with_every_known_file(store)
        (_spool_dir(store) / "notes.txt").write_text("hand-made", encoding="utf-8")
        (_spool_dir(store) / "subdir").mkdir()

        files = cost_ledger.ledger_files(_ledger(store))

        assert files.strays == ("notes.txt", "subdir")
        assert any("notes.txt" in finding for finding in files.findings())

    def test_reading_the_files_creates_nothing(self, store):
        """No lock file is created for a look: a store the doctor may not write stays as it was."""
        before = _tree(store)
        files = cost_ledger.ledger_files(_ledger(store))
        assert _tree(store) == before
        assert (files.ledger, files.lock_file, files.findings()) == (False, False, [])
        assert files.summary().startswith("no ledger yet")

    def test_the_fabric_doctor_knows_the_lock_file_and_warns_on_what_waits(self, store):
        from vnx_doctor import PASS, WARN, check_cost_ledger_files

        (store / "events").mkdir()
        _ledger(store).write_bytes(b"")
        (store / "events" / "provider_costs.lock").touch()
        (_spool_dir(store) / "quarantine").mkdir(parents=True)
        (passed,) = check_cost_ledger_files(_doctor_paths(store))
        assert (passed.name, passed.status) == ("cost_ledger", PASS)
        assert "lock file" in passed.message

        _spool(store, _record())
        (_spool_dir(store) / "notes.txt").write_text("", encoding="utf-8")
        (warned,) = check_cost_ledger_files(_doctor_paths(store))
        assert warned.status == WARN
        assert "1 spool files wait for the replay" in warned.message and "notes.txt" in warned.message
        assert "vnx costs replay" in warned.remediation

    def test_the_pip_doctor_tells_the_same_story(self, store):
        from vnx_cli.commands.doctor import PASS, WARN, _check_cost_ledger_files

        (store / "events").mkdir()
        (store / "events" / "provider_costs.lock").touch()
        passed = _check_cost_ledger_files(store)
        assert (passed.name, passed.status) == ("cost-ledger", PASS)
        assert "lock file" in passed.detail

        _a_store_with_every_known_file(store)
        warned = _check_cost_ledger_files(store)
        assert warned.status == WARN
        assert "1 spool files wait for the replay" in warned.detail
        assert "1 spool files are in quarantine" in warned.detail

    def test_the_footprint_probe_counts_them_as_known_and_keeps_its_verdict(self, store):
        import subsystem_health

        archive = store / "events" / "archive" / "T1" / "recent.ndjson"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"x" * 100)
        without = subsystem_health.aggregate(state_dir=store, subsystems=["vnx-data-footprint"])["vnx-data-footprint"]
        _a_store_with_every_known_file(store)

        result = subsystem_health.aggregate(state_dir=store, subsystems=["vnx-data-footprint"])["vnx-data-footprint"]

        assert result["status"] == without["status"]
        known = result["detail"]["cost_ledger"]
        assert (known["ledger"], known["lock_file"], known["strays"]) == (True, True, [])
        assert (known["spool_files"], known["spool_temp_files"], known["quarantined"]) == (1, 1, 1)
        assert known == cost_ledger.ledger_files(_ledger(store)).as_dict()
