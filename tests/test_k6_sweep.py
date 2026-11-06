"""ADR-045 audit-spoor-kern K-6: the sweep, its heartbeat, its tick, its count.

Dispatch-ID: dlv-ceb78731c6eb

The sweep is the one tick that closes what no lane can: it recovers an end
intent that never published, and it books ``abandoned`` for a ``v3`` attempt
that is past its deadline plus the 30-minute grace and has no liveness
(ADR-045 §5/§6). One block per acceptance point of the dispatch:

1. a hard-killed lane is abandoned, with the row projected per the table;
2. liveness from the flock, the door pid or the child pid keeps it alive;
3. a reused pid (alive, other start time) counts as dead;
4. an unmeasurable probe is unknown and never abandons;
5. an open intent whose line is already in the ledger completes the row;
6. the sweep racing a lane's ``finalize`` gives exactly one end line;
7. a legacy dispatch is never swept;
8. the heartbeat after a good run and after a raised sweep, and the runner's
   exit code unchanged when the sweep raises;
9. the count command's two numbers;
10. ``StartInterval`` 900 in the runner's plist.

Every store is real (a real ledger, a real ``runtime_coordination.db``, real
files) under ``tmp_path``; only the clock is given. Liveness uses real
short-lived ``sleep`` processes.
"""

from __future__ import annotations

import fcntl
import gzip
import importlib.util as _ilu
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import gate_obligation_runner as runner  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.lifecycle import (  # noqa: E402
    build_end_intent,
    end_intent_path,
    load_end_intent,
    prepare_end_line,
    write_end_intent,
)
from append_receipt_internals.payload import append_receipt_payload  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402
from coordination_db import get_connection  # noqa: E402
from dispatch_spec import DEADLINE_SECONDS_MAX  # noqa: E402
from dispatch_worktree_isolation import claims_dir_for_state_dir  # noqa: E402

PROJECT = "vnx-dev"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k6-hard"
ATTEMPT = 1
ROLE = "backend-developer"
DEADLINE = 300  # dispatch_spec.DEADLINE_SECONDS_MIN: the smallest legal deadline
GRACE = 30 * 60  # the 30-minute decision of 15-10, pinned literally
START = "2026-11-01T10:00:00Z"
START_DT = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k6", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the real ledger layer, never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    # The lifecycle writers (dispatch_start / dispatch_lifecycle) resolve the
    # append through the registered facade, so the same wrapper goes there too.
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """``<tmp>/vnx-data``: the state dir sits inside it, as in production."""
    return tmp_path / "vnx-data"


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = data_dir / "state"
    sd.mkdir(parents=True)
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def lifecycle_off(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")


# ---------------------------------------------------------------------------
# Helpers: real lines, real rows, real processes
# ---------------------------------------------------------------------------

def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _finished(state_dir: Path, dispatch_id: str = DISPATCH, attempt: int = ATTEMPT) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == "dispatch_finished"
            and line.get("dispatch_id") == dispatch_id and line.get("attempt") == attempt]


def _append(state_dir: Path, line: dict):
    return append_receipt_payload(dict(line), receipts_file=str(_ledger(state_dir)),
                                  skip_enrichment=True)


def _spec(state_dir: Path, *, dispatch_id: str = DISPATCH, deadline: int = DEADLINE,
          subdir: str = "completed") -> tuple[Path, str]:
    """A staged ``dispatch-spec.json`` and the sha256 the start line records."""
    import hashlib

    path = state_dir.parent / "dispatches" / subdir / dispatch_id / "dispatch-spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"dispatch_id": dispatch_id, "deadline_seconds": deadline})
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _start_line(state_dir: Path, *, project_id: str = PROJECT, dispatch_id: str = DISPATCH,
                attempt: int = ATTEMPT, mode: str = "v3", door_pid: int = 0,
                door_started_at: str = "", spec_sha: str = "0" * 64,
                timestamp: str = START) -> dict:
    line = {
        "timestamp": timestamp, "event_type": "dispatch_started",
        "project_id": project_id, "dispatch_id": dispatch_id, "attempt": attempt,
        "role": ROLE, "base_sha": "a" * 40, "spec_sha256": spec_sha,
        "lifecycle_mode": mode, "door_pid": door_pid,
        "door_started_at": door_started_at or "Mon Jan  1 00:00:00 2024",
        "requested_by": "terminal:T1",
    }
    _append(state_dir, line)
    return line


def _insert_row(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                state: str = "running", attempt_count: int = ATTEMPT,
                metadata: dict | None = None) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count,
             json.dumps(metadata if metadata is not None else {"lifecycle_mode": "v3"})),
        )
        conn.commit()


def _row(state_dir: Path, dispatch_id: str = DISPATCH, project_id: str = PROJECT) -> dict:
    with get_connection(state_dir) as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id)).fetchone()
    return dict(row) if row is not None else {}


def _spawn_file(state_dir: Path, *, dispatch_id: str = DISPATCH, attempt: int = ATTEMPT,
                child_pid: int, child_started_at: str) -> Path:
    path = dispatch_start.spawn_file_path(state_dir, PROJECT, dispatch_id, attempt)
    atomic_write_json(path, {"version": 1, "project_id": PROJECT, "dispatch_id": dispatch_id,
                             "attempt": attempt, "child_pid": child_pid,
                             "child_started_at": child_started_at})
    return path


def _dead_process() -> tuple[int, str]:
    """A real process that has exited, with the start time it had while alive."""
    proc = subprocess.Popen(["sleep", "0.3"])
    started = dispatch_start.process_started_at(proc.pid)
    proc.wait(timeout=30)
    assert started, "the kernel must report the child's start time for this fixture"
    return proc.pid, started


def _live_process_start(pid: int) -> str:
    started = dispatch_start.process_started_at(pid)
    assert started, f"pid {pid} must have a readable start time"
    return started


@contextmanager
def _held_occupancy(state_dir: Path, dispatch_id: str = DISPATCH):
    path = claims_dir_for_state_dir(state_dir) / f"{dispatch_id}.occupancy"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now_after(start: datetime, seconds: float) -> datetime:
    return start + timedelta(seconds=seconds)


def _heartbeat(state_dir: Path) -> dict:
    path = state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _freeze_intent(state_dir: Path, line: dict, *, project_id: str = PROJECT,
                   dispatch_id: str = DISPATCH, attempt: int = ATTEMPT) -> Path:
    ledger = _ledger(state_dir)
    prepared = prepare_end_line(line, ledger_path=ledger)
    intent_path = end_intent_path(state_dir, project_id, dispatch_id, attempt)
    offset = ledger.stat().st_size if ledger.exists() else 0
    document = build_end_intent(
        project_id=project_id, dispatch_id=dispatch_id, attempt=attempt,
        end_state=str(prepared["end_state"]), end_line=prepared,
        ledger_path=ledger, ledger_offset=offset)
    assert write_end_intent(intent_path, document) is True
    return intent_path


def _end_line(**overrides) -> dict:
    line = {"timestamp": START, "event_type": "dispatch_finished", "project_id": PROJECT,
            "dispatch_id": DISPATCH, "attempt": ATTEMPT, "end_state": "completed",
            "head_sha": "c" * 40}
    line.update(overrides)
    return line


#: A door that has exited: the pid is reused by this process with another start time.
_STALE_DOOR_START = "Mon Jan  1 00:00:00 2024"


def _begin_v3(state_dir: Path, *, dispatch_id: str = DISPATCH,
              project_id: str = PROJECT) -> dispatch_start.Handover:
    """A real ``dispatch_start.begin`` line, with only the clock and the door's start
    time pinned: the recorded door identity is stale, so the sweep reads it as dead."""
    spec_file, _sha = _spec(state_dir, dispatch_id=dispatch_id)
    with patch.object(dispatch_start, "process_started_at", lambda pid: _STALE_DOOR_START), \
            patch.object(dispatch_start, "_now", lambda: START):
        return dispatch_start.begin(project_id=project_id, dispatch_id=dispatch_id,
                                    role=ROLE, base_sha="a" * 40, spec_file=spec_file,
                                    target_slot="T1", state_dir=state_dir)


def _attempt_stub() -> "lifecycle.InFlightAttempt":
    return lifecycle.InFlightAttempt(PROJECT, DISPATCH, ATTEMPT, {})


def _set_metadata(state_dir: Path, metadata_json, *, project_id: str = PROJECT,
                  dispatch_id: str = DISPATCH) -> None:
    with get_connection(state_dir) as conn:
        conn.execute("UPDATE dispatches SET metadata_json = ? "
                     "WHERE dispatch_id = ? AND project_id = ?",
                     (metadata_json, dispatch_id, project_id))
        conn.commit()


# ---------------------------------------------------------------------------
# 1. a hard-killed lane is abandoned
# ---------------------------------------------------------------------------

def test_a_hard_killed_lane_is_abandoned_after_deadline_plus_grace(
        state_dir, lifecycle_on):
    _spec_path, spec_sha = _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, spec_sha=spec_sha)
    child_pid, child_started = _dead_process()
    _spawn_file(state_dir, child_pid=child_pid, child_started_at=child_started)

    now = _now_after(START_DT, DEADLINE + GRACE + 60)
    result = lifecycle.sweep(state_dir=state_dir, now=now)

    assert result.abandoned == 1, result
    assert result.errors == ()
    finished = _finished(state_dir)
    assert len(finished) == 1
    assert finished[0]["end_state"] == "abandoned"
    assert "head_sha" not in finished[0]
    assert finished[0]["no_push"] == "no_push:deadline_passed_no_liveness"
    row = _row(state_dir)
    assert row["state"] == "timed_out"
    assert json.loads(row["metadata_json"])["end_state"] == "abandoned"
    assert result.heartbeat_path.endswith(lifecycle.SWEEP_HEARTBEAT_FILENAME)
    heartbeat = _heartbeat(state_dir)
    assert heartbeat["counts"]["abandoned"] == 1
    assert heartbeat["deadline"]["fallback_seconds"] == lifecycle.FALLBACK_DEADLINE_SECONDS
    assert heartbeat["last_error"] is None


def test_the_attempt_is_not_abandoned_inside_the_grace(state_dir, lifecycle_on):
    _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir)

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE - 1))

    assert result.abandoned == 0
    assert result.in_flight == 1
    assert _finished(state_dir) == []


# ---------------------------------------------------------------------------
# 2. liveness: the flock, the door, the child
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("signal", ["occupancy_flock", "door_pid", "child_pid"])
def test_a_live_signal_keeps_the_attempt_open_also_well_past_the_threshold(
        state_dir, lifecycle_on, signal):
    _spec(state_dir)
    _insert_row(state_dir)
    door_pid, door_started, child_pid, child_started = 0, "", 0, ""
    context = _held_occupancy(state_dir) if signal == "occupancy_flock" else _null_context()
    if signal == "door_pid":
        door_pid, door_started = os.getpid(), _live_process_start(os.getpid())
    if signal == "child_pid":
        live = subprocess.Popen(["sleep", "60"])
        try:
            child_pid, child_started = live.pid, _live_process_start(live.pid)
            _start_line(state_dir, door_pid=door_pid, door_started_at=door_started)
            _spawn_file(state_dir, child_pid=child_pid, child_started_at=child_started)
            with context:
                result = lifecycle.sweep(
                    state_dir=state_dir, now=_now_after(START_DT, 10 * 3600))
            assert result.abandoned == 0
            assert result.live == 1, result
            assert _finished(state_dir) == []
            return
        finally:
            live.kill()
            live.wait(timeout=30)
    _start_line(state_dir, door_pid=door_pid, door_started_at=door_started)
    with context:
        result = lifecycle.sweep(state_dir=state_dir, now=_now_after(START_DT, 10 * 3600))
    assert result.abandoned == 0
    assert result.live == 1, result
    assert _finished(state_dir) == []


@contextmanager
def _null_context():
    yield None


def test_a_live_door_with_the_same_start_time_survives_before_the_flock(state_dir, lifecycle_on):
    """The window between the start line and the occupancy flock (gap 2)."""
    _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, door_pid=os.getpid(),
                door_started_at=_live_process_start(os.getpid()))

    result = lifecycle.sweep(state_dir=state_dir, now=_now_after(START_DT, 10 * 3600))

    assert result.abandoned == 0
    assert result.live == 1
    assert _finished(state_dir) == []


# ---------------------------------------------------------------------------
# 3. a reused pid counts as dead
# ---------------------------------------------------------------------------

def test_a_reused_pid_with_another_start_time_counts_as_dead(state_dir, lifecycle_on):
    _spec_path, spec_sha = _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, door_pid=os.getpid(), spec_sha=spec_sha,
                door_started_at="Thu Jan  1 00:00:00 1970")

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 1, result
    assert result.live == 0
    assert len(_finished(state_dir)) == 1


# ---------------------------------------------------------------------------
# 4. an unmeasurable probe is unknown, never dead
# ---------------------------------------------------------------------------

def test_an_unmeasurable_liveness_probe_never_abandons(state_dir, lifecycle_on, monkeypatch):
    _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, door_pid=os.getpid(), door_started_at="some start time")

    def _boom(pid):
        raise RuntimeError("ps is not readable")

    monkeypatch.setattr(dispatch_start, "probe_process_started_at", _boom)

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, 10 * 3600))

    assert result.abandoned == 0
    assert result.unknown_liveness == 1, result
    assert _finished(state_dir) == []
    assert _heartbeat(state_dir)["counts"]["unknown_liveness"] == 1


# ---------------------------------------------------------------------------
# 5. an open intent whose line is already in the ledger
# ---------------------------------------------------------------------------

def test_an_open_intent_with_its_line_in_the_ledger_completes_the_row(
        state_dir, lifecycle_on):
    _insert_row(state_dir)
    _freeze_intent(state_dir, _end_line())
    _append(state_dir, _end_line())  # the crash between step 3 and step 5

    result = lifecycle.sweep(state_dir=state_dir, now=START_DT)

    assert result.recovered == 1, result
    assert len(_finished(state_dir)) == 1  # never a second line
    assert _row(state_dir)["state"] == "completed"
    intent = load_end_intent(end_intent_path(state_dir, PROJECT, DISPATCH, ATTEMPT))
    assert intent["published_at"]


# ---------------------------------------------------------------------------
# 6. the sweep racing a lane's finalize
# ---------------------------------------------------------------------------

def test_the_sweep_and_a_lane_finalize_race_gives_one_end_line(state_dir, lifecycle_on):
    _spec_path, spec_sha = _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, spec_sha=spec_sha)
    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def _sweep():
        barrier.wait(timeout=30)
        try:
            outcomes["sweep"] = lifecycle.sweep(
                state_dir=state_dir, now=_now_after(START_DT, DEADLINE + GRACE + 60))
        except Exception as exc:  # surface a child failure instead of hanging the test
            outcomes["sweep"] = f"{type(exc).__name__}: {exc}"

    def _lane():
        barrier.wait(timeout=30)
        try:
            outcomes["lane"] = lifecycle.finalize(
                state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                attempt=ATTEMPT, end_state="completed", end_line=_end_line())
        except Exception as exc:
            outcomes["lane"] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=_sweep), threading.Thread(target=_lane)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    finished = _finished(state_dir)
    assert len(finished) == 1, finished
    assert finished[0]["end_state"] in {"abandoned", "completed"}
    assert isinstance(outcomes.get("lane"), lifecycle.LifecycleResult), outcomes
    assert isinstance(outcomes.get("sweep"), lifecycle.SweepResult), outcomes


# ---------------------------------------------------------------------------
# 7. a legacy dispatch is never swept
# ---------------------------------------------------------------------------

def test_a_legacy_dispatch_past_its_deadline_is_untouched(state_dir, lifecycle_on):
    _spec(state_dir)
    _insert_row(state_dir, metadata={"lifecycle_mode": "legacy"})
    _start_line(state_dir, mode="legacy")

    result = lifecycle.sweep(state_dir=state_dir, now=_now_after(START_DT, 10 * 3600))

    assert result.abandoned == 0
    assert result.in_flight == 0
    assert _finished(state_dir) == []
    assert _row(state_dir)["state"] == "running"


# ---------------------------------------------------------------------------
# 8. deadline sources and the named fallback
# ---------------------------------------------------------------------------

def test_the_staged_spec_is_the_deadline_source(state_dir, lifecycle_on):
    _spec_path, spec_sha = _spec(state_dir, deadline=DEADLINE)
    _insert_row(state_dir)
    _start_line(state_dir, spec_sha=spec_sha)

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 1
    assert _heartbeat(state_dir)["deadline"]["sources"][0].endswith(":spec:" + str(_spec_path))


def test_an_unreadable_deadline_falls_back_to_the_named_constant(state_dir, lifecycle_on):
    _insert_row(state_dir, metadata={"lifecycle_mode": "v3"})
    _start_line(state_dir, spec_sha="f" * 64)  # no staged spec matches this sha

    inside = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))
    assert inside.abandoned == 0  # 300 s would be past: the fallback is not it
    assert tuple(_heartbeat(state_dir)["deadline"]["sources"]) == (
        "%s#%d:fallback:FALLBACK_DEADLINE_SECONDS" % (DISPATCH, ATTEMPT),)

    past = lifecycle.sweep(
        state_dir=state_dir,
        now=_now_after(START_DT, lifecycle.FALLBACK_DEADLINE_SECONDS + GRACE + 60))
    assert past.abandoned == 1
    assert _heartbeat(state_dir)["deadline"]["fallback_seconds"] == (
        lifecycle.FALLBACK_DEADLINE_SECONDS)


def test_a_spec_with_a_different_sha_is_not_the_deadline_source(state_dir, lifecycle_on):
    _spec(state_dir, deadline=DEADLINE)
    _insert_row(state_dir)
    _start_line(state_dir, spec_sha="e" * 64)  # the file on disk is not this dispatch's

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 0
    assert tuple(_heartbeat(state_dir)["deadline"]["sources"]) == (
        "%s#%d:fallback:FALLBACK_DEADLINE_SECONDS" % (DISPATCH, ATTEMPT),)


# ---------------------------------------------------------------------------
# 9. the brake: the lifecycle off while a v3 attempt is in flight
# ---------------------------------------------------------------------------

def test_with_the_lifecycle_off_the_sweep_counts_and_writes_nothing(
        state_dir, lifecycle_on, monkeypatch):
    _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir)
    before = _lines(state_dir)
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, 10 * 3600))

    assert result.lifecycle_off is True
    assert result.lifecycle_off_in_flight == 1
    assert result.abandoned == 0
    assert _lines(state_dir) == before  # no end line, no refusal loop
    assert _heartbeat(state_dir)["counts"]["lifecycle_off_in_flight"] == 1
    assert list((state_dir / "dispatch_end" / PROJECT).glob("*.json")) == []


# ---------------------------------------------------------------------------
# 10. the heartbeat, also when the sweep raised
# ---------------------------------------------------------------------------

def test_a_raised_sweep_still_writes_its_heartbeat(state_dir, lifecycle_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("the intent tree is unreadable")

    monkeypatch.setattr(lifecycle, "open_intents", _boom)

    with pytest.raises(RuntimeError):
        lifecycle.sweep(state_dir=state_dir, now=START_DT)

    heartbeat = _heartbeat(state_dir)
    assert "the intent tree is unreadable" in heartbeat["error"]
    assert heartbeat["counts"]["errors"] == 1


def test_the_runner_keeps_its_exit_code_when_the_sweep_raises(
        state_dir, lifecycle_on, monkeypatch):
    monkeypatch.setattr(runner, "_tick_github_context", lambda state_dir: (None, None))
    monkeypatch.setattr(runner, "_open_pr_lookup",
                        lambda owner_repo: {"prs": [], "suspect": False})

    def _boom(**kwargs):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(lifecycle, "sweep", _boom)

    assert runner.main(["--state-dir", str(state_dir), "--json"]) == 0
    summary = runner.run(state_dir)
    assert summary["lifecycle_sweep"]["ran"] is True
    assert "sweep exploded" in summary["lifecycle_sweep"]["error"]
    assert summary["pending_after"] == 0
    assert not (state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME).exists()


def test_the_runner_runs_the_sweep_once_per_run(state_dir, lifecycle_on, monkeypatch):
    monkeypatch.setattr(runner, "_tick_github_context", lambda state_dir: (None, None))
    monkeypatch.setattr(runner, "_open_pr_lookup",
                        lambda owner_repo: {"prs": [], "suspect": False})
    _spec(state_dir)
    _insert_row(state_dir)
    _start_line(state_dir, timestamp="2099-01-01T00:00:00Z")
    seen: list = []
    real = lifecycle.sweep

    def _spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(lifecycle, "sweep", _spy)  # the runner imports it lazily

    summary = runner.run(state_dir)

    assert len(seen) == 1, seen
    assert summary["lifecycle_sweep"]["abandoned"] == 0  # inside the default deadline
    assert (state_dir / "dispatch_end" / lifecycle.SWEEP_HEARTBEAT_FILENAME).is_file()
    assert runner.run(state_dir, write=False)["lifecycle_sweep"] == {
        "ran": False, "reason": "dry_run"}


def test_the_runner_plist_pins_start_interval_900():
    plist = (VNX_ROOT / "scripts" / "launchd"
             / "com.vnx.gate-obligation-runner.plist").read_text(encoding="utf-8")
    match = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", plist)
    assert match is not None, "the runner plist must declare StartInterval"
    assert int(match.group(1)) == 900


# ---------------------------------------------------------------------------
# 11. the count command
# ---------------------------------------------------------------------------

def _count(state_dir: Path) -> "tuple[int, dict]":
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "dispatch_lifecycle_count.py"),
         "--state-dir", str(state_dir), "--json"],
        capture_output=True, text=True, timeout=120)
    return proc.returncode, json.loads(proc.stdout or "{}")


def test_the_count_command_counts_two_in_flight_and_one_open_intent(
        state_dir, lifecycle_on, tmp_path):
    _insert_row(state_dir, dispatch_id="d-k6-a")
    _insert_row(state_dir, dispatch_id="d-k6-b")
    _start_line(state_dir, dispatch_id="d-k6-a")
    _start_line(state_dir, dispatch_id="d-k6-b")
    _freeze_intent(state_dir, _end_line(dispatch_id="d-k6-c"), dispatch_id="d-k6-c")

    code, report = _count(state_dir)

    assert code == 0
    assert report["v3_in_flight"] == 2
    assert report["open_intents"] == 1
    assert report["v3_in_flight_lifecycle_off"] == 0


def test_the_count_command_is_non_zero_when_a_source_is_unreadable(
        state_dir, lifecycle_on):
    broken = state_dir / "dispatch_end" / PROJECT / "d-k6-broken.1.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")

    code, report = _count(state_dir)

    assert code == 2
    assert report["unreadable"]
    assert report["v3_in_flight"] == 0
    assert report["open_intents"] == 0


def test_the_count_command_reports_the_lifecycle_off_in_flight(
        state_dir, lifecycle_on, monkeypatch):
    _insert_row(state_dir)
    _start_line(state_dir)
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")

    code, report = _count(state_dir)

    assert code == 0
    assert report["v3_in_flight"] == 1
    assert report["lifecycle_off"] is True
    assert report["v3_in_flight_lifecycle_off"] == 1


# ---------------------------------------------------------------------------
# 12. K-6 round 2: only a real end ends a v3 attempt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("with_attempt", [False, True], ids=["no-attempt", "attempt-1"])
def test_a_refusal_receipt_does_not_end_a_v3_attempt(
        state_dir, lifecycle_on, with_attempt):
    """Codex's reproduction: start, then a refused delivery, and no dispatch_finished."""
    _insert_row(state_dir)
    _begin_v3(state_dir)
    refusal = {"timestamp": START, "event_type": "task_complete", "project_id": PROJECT,
               "dispatch_id": DISPATCH, "status": "blocked",
               "delivery_state": "delivery_refused"}
    if with_attempt:
        refusal["attempt"] = ATTEMPT
    _append(state_dir, refusal)

    assert [attempt.attempt for attempt in lifecycle.v3_in_flight(state_dir)[0]] == [ATTEMPT]
    code, report = _count(state_dir)
    assert code == 0, report
    assert report["v3_in_flight"] == 1, report

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 1, result
    assert result.in_flight == 1, result
    assert [line["end_state"] for line in _finished(state_dir)] == ["abandoned"]
    assert _count(state_dir)[1]["v3_in_flight"] == 0


@pytest.mark.parametrize("event_type", ["task_complete", "task_failed", "task_timeout"])
def test_no_legacy_completion_ends_a_v3_attempt(state_dir, lifecycle_on, event_type):
    _insert_row(state_dir)
    _begin_v3(state_dir)
    _append(state_dir, {"timestamp": START, "event_type": event_type,
                        "project_id": PROJECT, "dispatch_id": DISPATCH, "attempt": ATTEMPT})

    assert [attempt.attempt for attempt in lifecycle.v3_in_flight(state_dir)[0]] == [ATTEMPT]


def test_a_dispatch_finished_without_an_attempt_never_ends_the_attempt(
        state_dir, lifecycle_on):
    """The append layer refuses such a line, so a legacy writer's line is written raw."""
    _insert_row(state_dir)
    _begin_v3(state_dir)
    with _ledger(state_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": START, "event_type": "dispatch_finished",
                                 "project_id": PROJECT, "dispatch_id": DISPATCH,
                                 "end_state": "completed"}) + "\n")

    assert [attempt.attempt for attempt in lifecycle.v3_in_flight(state_dir)[0]] == [ATTEMPT]


def test_a_finished_for_attempt_one_does_not_end_attempt_two(state_dir, lifecycle_on):
    _insert_row(state_dir, attempt_count=1)
    _begin_v3(state_dir)  # attempt 1
    lifecycle.finalize(state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH,
                       attempt=1, end_state="completed", end_line=_end_line(attempt=1))
    _begin_v3(state_dir)  # attempt 2

    flights, errors = lifecycle.v3_in_flight(state_dir)

    assert [(attempt.dispatch_id, attempt.attempt) for attempt in flights] == [(DISPATCH, 2)]
    assert errors == ()


def test_a_finished_of_another_project_does_not_end_the_attempt(state_dir, lifecycle_on):
    _insert_row(state_dir)
    _begin_v3(state_dir)
    lifecycle.finalize(state_dir=state_dir, project_id=OTHER_PROJECT, dispatch_id=DISPATCH,
                       attempt=ATTEMPT, end_state="completed",
                       end_line=_end_line(project_id=OTHER_PROJECT))

    flights, errors = lifecycle.v3_in_flight(state_dir)

    assert [(attempt.project_id, attempt.attempt) for attempt in flights] == [
        (PROJECT, ATTEMPT)]
    assert errors == ()


# ---------------------------------------------------------------------------
# 13. K-6 round 2: the mode stamp is one atomic statement
# ---------------------------------------------------------------------------

def test_the_stamp_merges_under_a_key_committed_after_the_read(state_dir):
    _insert_row(state_dir, metadata={"a": 1})
    row = lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)  # the sweep's read
    _set_metadata(state_dir, json.dumps({"a": 1, "b": 2}))        # a later writer, before the stamp

    lifecycle._stamp_lifecycle_mode(state_dir, row, _attempt_stub())

    assert json.loads(_row(state_dir)["metadata_json"]) == {
        "a": 1, "b": 2, "lifecycle_mode": "v3"}


def test_the_stamp_gives_null_metadata_the_mode(state_dir):
    _insert_row(state_dir)
    _set_metadata(state_dir, None)
    row = lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)

    lifecycle._stamp_lifecycle_mode(state_dir, row, _attempt_stub())

    assert json.loads(_row(state_dir)["metadata_json"]) == {"lifecycle_mode": "v3"}


@pytest.mark.parametrize("raw", ["[1]", "not json"])
def test_the_stamp_leaves_metadata_that_is_not_an_object_untouched(state_dir, raw):
    _insert_row(state_dir)
    _set_metadata(state_dir, raw)
    row = lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)

    lifecycle._stamp_lifecycle_mode(state_dir, row, _attempt_stub())

    assert _row(state_dir)["metadata_json"] == raw


def test_the_stamp_leaves_another_projects_row_untouched(state_dir):
    _insert_row(state_dir, metadata={"a": 1})
    _insert_row(state_dir, project_id=OTHER_PROJECT, metadata={"a": 1})
    row = lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)

    lifecycle._stamp_lifecycle_mode(state_dir, row, _attempt_stub())

    assert json.loads(_row(state_dir)["metadata_json"]) == {"a": 1, "lifecycle_mode": "v3"}
    assert json.loads(_row(state_dir, project_id=OTHER_PROJECT)["metadata_json"]) == {"a": 1}


# ---------------------------------------------------------------------------
# 14. K-6 round 3: a store that cannot be read is reported, never "no row"
# ---------------------------------------------------------------------------

def _store_path(state_dir: Path) -> Path:
    return coordination_db.db_path_from_state_dir(state_dir)


def _corrupt_store(state_dir: Path) -> bytes:
    """Check the readable store's bytes out, then leave a file that is no database."""
    db = _store_path(state_dir)
    with get_connection(state_dir) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    kept = db.read_bytes()
    db.write_bytes(b"this file is not a sqlite database\n")
    return kept


def _restore_store(state_dir: Path, kept: bytes) -> None:
    """Put the readable store back, without the write-ahead files a failed read leaves."""
    db = _store_path(state_dir)
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    db.write_bytes(kept)


def _remove_store(state_dir: Path) -> None:
    """No store at all: not a row, and never "unreadable" (ADR-045 §6)."""
    db = _store_path(state_dir)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)


def test_an_unreadable_store_books_nothing_and_is_reported(state_dir, lifecycle_on):
    """Codex's reproduction: past the threshold with no liveness, and a store
    that is not a database. The row read fails, and the sweep says so."""
    _insert_row(state_dir)
    _begin_v3(state_dir)
    _corrupt_store(state_dir)

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 30 * 60))

    assert result.abandoned == 0, result
    assert result.in_flight == 1, result
    assert _finished(state_dir) == []  # nothing booked: no end line, no projection, no stamp
    assert len(result.errors) == 1, result.errors
    assert f"{DISPATCH}#{ATTEMPT}" in result.errors[0], result.errors
    assert "is not a database" in result.errors[0], result.errors
    heartbeat = _heartbeat(state_dir)
    assert heartbeat["counts"]["errors"] == 1, heartbeat
    assert heartbeat["last_error"] == result.errors[0], heartbeat
    code, report = _count(state_dir)
    assert code == 2, report
    assert len(report["unreadable"]) == 1, report


def test_an_absent_store_is_no_row_and_the_attempt_is_abandoned(state_dir, lifecycle_on):
    _insert_row(state_dir)
    _begin_v3(state_dir)
    _remove_store(state_dir)

    result = lifecycle.sweep(state_dir=state_dir,
                             now=_now_after(START_DT, DEADLINE + GRACE + 60))

    assert result.abandoned == 1, result
    assert result.errors == (), result.errors
    assert [line["end_state"] for line in _finished(state_dir)] == ["abandoned"]
    assert _count(state_dir)[0] == 0  # absent is not unreadable, also for the count


def test_after_the_store_is_repaired_the_next_sweep_abandons(state_dir, lifecycle_on):
    _insert_row(state_dir)
    _begin_v3(state_dir)
    kept = _corrupt_store(state_dir)
    now = _now_after(START_DT, DEADLINE + GRACE + 60)

    first = lifecycle.sweep(state_dir=state_dir, now=now)

    assert first.abandoned == 0, first
    assert len(first.errors) == 1, first.errors
    assert _finished(state_dir) == []

    _restore_store(state_dir, kept)
    second = lifecycle.sweep(state_dir=state_dir, now=now)

    assert second.abandoned == 1, second
    assert second.errors == (), second.errors
    assert [line["end_state"] for line in _finished(state_dir)] == ["abandoned"]


def test_the_dispatch_row_read_raises_on_an_unreadable_store(state_dir):
    """Three outcomes, not two: no store is ``None``, an unreadable one raises."""
    _insert_row(state_dir)
    assert lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)["bundle_path"] is None

    _corrupt_store(state_dir)

    with pytest.raises(AppendReceiptError) as raised:
        lifecycle._dispatch_row(state_dir, PROJECT, DISPATCH)
    assert "is not a database" in str(raised.value), raised.value


# ---------------------------------------------------------------------------
# 13. K-6 follow-up (OI-2537): compaction keeps an unfinished v3 start live
# ---------------------------------------------------------------------------

def _append_plain_records(state_dir: Path, count: int) -> None:
    with _ledger(state_dir).open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps({"seq": i, "timestamp": i}) + "\n"
                             for i in range(count)))


def test_compaction_keeps_an_unfinished_v3_start_in_the_live_ledger(state_dir, lifecycle_on):
    """OI-2537: one unfinished start before 10,000 records must count 1 before AND after."""
    import compact_state

    _start_line(state_dir)
    _append_plain_records(state_dir, 10_000)
    assert len(_lines(state_dir)) == 10_001

    before, before_errors = lifecycle.v3_in_flight(state_dir)
    assert len(before) == 1 and before_errors == ()
    assert lifecycle.in_flight_counts(state_dir)["v3_in_flight"] == 1

    assert compact_state.compact_receipts(state_dir) == 0

    after, after_errors = lifecycle.v3_in_flight(state_dir)
    assert len(after) == 1, "the unfinished start must survive compaction"
    assert after_errors == ()
    assert lifecycle.in_flight_counts(state_dir)["v3_in_flight"] == 1
    assert lifecycle.in_flight_counts(state_dir)["unreadable"] == []

    archive = next((state_dir / "archive").glob("t0_receipts_*.ndjson.gz"))
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        archived = [json.loads(raw) for raw in handle.read().splitlines() if raw.strip()]
    assert not [line for line in archived if line.get("event_type") == "dispatch_started"], \
        "the unfinished start must not be archived"


# ---------------------------------------------------------------------------
# 14. K-6 follow-up (OI-2538): an unreadable intent directory is not zero
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0o000 directory")
def test_an_unreadable_intent_project_dir_fails_the_count(state_dir, lifecycle_on):
    """OI-2538: a valid intent in a mode-0000 project dir must not read as zero intents."""
    _freeze_intent(state_dir, _end_line(dispatch_id="d-k6-hidden"), dispatch_id="d-k6-hidden")
    project_dir = state_dir / "dispatch_end" / PROJECT
    assert list(project_dir.glob("*.json")), "the fixture must hold a real intent"
    os.chmod(project_dir, 0o000)
    try:
        intents, errors = lifecycle.open_intents(state_dir)
        code, report = _count(state_dir)
    finally:
        os.chmod(project_dir, 0o755)

    assert errors, "an unreadable intent dir must be reported"
    assert any(str(project_dir) in problem for problem in errors), errors
    assert intents == ()
    assert code != 0
    assert report["unreadable"]
    assert report["open_intents"] == 0


def test_a_missing_intent_root_and_project_dir_are_no_intents_and_no_error(
        state_dir, lifecycle_on):
    assert not (state_dir / "dispatch_end").exists()

    assert lifecycle.open_intents(state_dir) == ((), ())
    code, report = _count(state_dir)
    assert code == 0
    assert report["open_intents"] == 0
    assert report["unreadable"] == []

    (state_dir / "dispatch_end").mkdir()
    assert lifecycle.open_intents(state_dir, PROJECT) == ((), ())
    code, report = _count(state_dir)
    assert code == 0 and report["unreadable"] == []


# ---------------------------------------------------------------------------
# 15. K-6 follow-up (OI-2539): the start line's own deadline is the first source
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [DEADLINE_SECONDS_MAX + 1, 299, True, "1800"])
def test_an_invalid_start_line_deadline_falls_through_to_the_fallback(
        state_dir, lifecycle_on, value):
    start = _start_line(state_dir, spec_sha="f" * 64)
    start["deadline_seconds"] = value

    assert lifecycle.deadline_for(state_dir, start, None) == (
        lifecycle.FALLBACK_DEADLINE_SECONDS, "fallback:FALLBACK_DEADLINE_SECONDS")
