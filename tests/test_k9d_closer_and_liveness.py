"""ADR-045 audit-spoor-kern K-9d: the closer reads to the record boundary, and an
unmeasurable liveness refuses the refire.

Dispatch-ID: dlv-c21823e24300

Both are codex reproductions on #2384 (K-9c):

1. ``_close_a_torn_tail`` judged only a 64 KiB tail window, so a complete record
   longer than the window (measured: the ``vnx-dev`` ledger holds a 894,997-byte
   line) was sealed as debris when its final newline was missing. The closer now
   reads backwards until the last newline and judges the whole final fragment.
2. ``process_started_at`` returned ``None`` both for "ps ran and the pid is gone"
   and for "ps could not answer". Before the occupancy flock exists an unavailable
   probe is the only signal, and it read as a dead door. The probe now answers
   alive, dead or unknown; the door refuses the refire on unknown; the sweep keeps
   its unknown state instead of abandoning.

Every ledger is written by the real writers and every store is a real
``runtime_coordination.db`` under ``tmp_path``; only ``_now`` is pinned. ``ps`` is
made unavailable through ``PATH`` only — no probe, liveness or plan function is
patched.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import ndjson_io  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402

PROJECT = "proj-alpha"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k9d"
OTHER_DISPATCH = "d-other"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
T0 = "2026-11-03T10:00:00Z"
START_DT = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
DEADLINE = 300
_REAL_PS = shutil.which("ps") or "/bin/ps"
_TORN_FRAGMENT = (b'{"event_type": "dispatch_finished", "dispatch_id": "d-k9d", '
                  b'"pad": "' + b"x" * 70_000)

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_k9d", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = tmp_path / "state"
    sd.mkdir()
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
def spec_file(tmp_path: Path) -> Path:
    """A staged spec with the smallest legal deadline, so the sweep's threshold is short."""
    path = tmp_path / "dispatch-spec.json"
    path.write_text(json.dumps({"deadline_seconds": DEADLINE}), encoding="utf-8")
    return path


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def clock(monkeypatch):
    """Only ``_now`` is pinned; every line is still written by a real writer."""
    monkeypatch.setattr(dispatch_start, "_now", lambda: T0)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _begin(state_dir: Path, spec_file: Path) -> dispatch_start.Handover:
    return dispatch_start.begin(project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE,
                                base_sha=BASE_SHA, spec_file=spec_file, target_slot="T1",
                                state_dir=state_dir)


def _records(state_dir: Path) -> list:
    return ndjson_io.read_ndjson_strict(_ledger(state_dir))


def _finished(state_dir: Path) -> list:
    return [line for line in _records(state_dir)
            if line.get("event_type") == "dispatch_finished"]


def _starts(state_dir: Path) -> list:
    return [line for line in _records(state_dir)
            if line.get("event_type") == "dispatch_started"
            and line.get("dispatch_id") == DISPATCH]


def _next_append(state_dir: Path) -> None:
    """The real append path: the closer runs under the lock, then the record lands."""
    result = payload_module.append_receipt_payload(
        {"timestamp": T0, "event_type": "task_complete", "project_id": OTHER_PROJECT,
         "dispatch_id": OTHER_DISPATCH, "terminal": "T1", "status": "success"},
        receipts_file=str(_ledger(state_dir)))
    assert result.status == "appended", result


def _seed_start(state_dir: Path, *, door_pid: int, door_started_at: str,
                deadline_seconds: int | None = None) -> None:
    """A v3 start line through the real append writer, for a door pid the test chooses."""
    line = {"timestamp": T0, "event_type": "dispatch_started", "project_id": PROJECT,
            "dispatch_id": DISPATCH, "attempt": 1, "role": ROLE, "base_sha": BASE_SHA,
            "spec_sha256": "b" * 64, "lifecycle_mode": "v3", "door_pid": door_pid,
            "door_started_at": door_started_at, "requested_by": "terminal:T1"}
    if deadline_seconds is not None:
        line["deadline_seconds"] = deadline_seconds
    result = payload_module.append_receipt_payload(line, receipts_file=str(_ledger(state_dir)))
    assert result.status == "appended", result


def _insert_row(state_dir: Path) -> None:
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (DISPATCH, PROJECT, "running", 1, json.dumps({"lifecycle_mode": "v3"})))
        conn.commit()


def _write_ps(directory: Path, *, fail_for: int | None = None) -> str:
    """A ``ps`` first on PATH: exit 1 with an error on stderr for *fail_for* (or all pids),
    and otherwise exec the real ps. A genuine failed probe — no Python seam is patched."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "ps"
    guard = ('echo "ps: unavailable" >&2; exit 1' if fail_for is None
             else f'[ "$2" = "{fail_for}" ] && {{ echo "ps: cannot read $2" >&2; exit 1; }}')
    shim.write_text(f'#!/bin/sh\n{guard}\nexec "{_REAL_PS}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return f"{directory}{os.pathsep}{os.environ['PATH']}"


def _dead_pid() -> tuple[int, str]:
    """A real process that has exited, with the kernel start time it had while alive."""
    proc = subprocess.Popen(["sleep", "0.3"])
    started = dispatch_start.process_started_at(proc.pid)
    proc.wait(timeout=30)
    assert started, "the kernel must report the child's start time for this fixture"
    return proc.pid, started


# ---------------------------------------------------------------------------
# rule 1: the whole final fragment decides, not a 64 KiB window
# ---------------------------------------------------------------------------

def test_a_complete_record_over_70k_without_its_newline_still_stands(state_dir):
    """Codex's reproduction: one record before the closer, zero after, at 70,142 bytes."""
    big = {"timestamp": T0, "event_type": "task_complete", "project_id": PROJECT,
           "dispatch_id": DISPATCH, "attempt": 1, "status": "success", "pad": "x" * 70_000}
    raw = json.dumps(big).encode("utf-8")
    assert len(raw) > 70_000
    _ledger(state_dir).write_bytes(raw)  # the crash state: complete JSON, no newline

    _next_append(state_dir)

    data = _ledger(state_dir).read_bytes()
    assert data.startswith(raw + b"\n"), "the complete record did not stand"
    assert ndjson_io.TORN_TAIL_CLOSED not in data
    assert [record.get("dispatch_id") for record in _records(state_dir)] == [
        DISPATCH, OTHER_DISPATCH]
    assert data.split(b"\n")[0] == raw, "the next record must start on its own line"


def test_a_torn_fragment_over_64k_is_sealed_and_skipped(state_dir):
    """A fragment cut mid-JSON is debris whatever its length: sealed, never parsed."""
    _ledger(state_dir).write_bytes(_TORN_FRAGMENT)

    _next_append(state_dir)

    data = _ledger(state_dir).read_bytes()
    assert data.startswith(_TORN_FRAGMENT + ndjson_io.TORN_TAIL_CLOSED + b"\n")
    assert data.count(ndjson_io.TORN_TAIL_CLOSED) == 1
    assert [record.get("dispatch_id") for record in _records(state_dir)] == [OTHER_DISPATCH]


# ---------------------------------------------------------------------------
# rule 2: the probe answers alive, dead or unknown
# ---------------------------------------------------------------------------

def test_the_probe_separates_alive_dead_and_unknown(tmp_path, monkeypatch):
    state, started = dispatch_start.probe_process_started_at(os.getpid())
    assert state == dispatch_start.PROBE_ALIVE
    assert started == dispatch_start.process_started_at(os.getpid())

    pid, dead_start = _dead_pid()
    assert dispatch_start.probe_process_started_at(pid) == (dispatch_start.PROBE_DEAD, None)
    assert dispatch_start.process_started_at(pid) is None

    # ps fails on stderr: an unmeasurable probe, never a dead door.
    monkeypatch.setenv("PATH", _write_ps(tmp_path / "erroring", fail_for=os.getpid()))
    assert dispatch_start.probe_process_started_at(os.getpid()) == (
        dispatch_start.PROBE_UNKNOWN, None)

    # ps is not on PATH at all: unknown too, and ``process_started_at`` keeps its None.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert dispatch_start.probe_process_started_at(os.getpid())[0] == dispatch_start.PROBE_UNKNOWN
    assert dispatch_start.process_started_at(os.getpid()) is None
    assert dead_start  # the recorded dead start stays the value the test recorded


# ---------------------------------------------------------------------------
# rule 3: an unknown probe for the previous door refuses the refire
# ---------------------------------------------------------------------------

def test_a_refire_with_an_unmeasurable_previous_door_is_refused(
        state_dir, spec_file, v3, clock, tmp_path, monkeypatch):
    previous = subprocess.Popen(["sleep", "60"])
    try:
        started = dispatch_start.process_started_at(previous.pid)
        assert started, "the kernel must report the previous door's start time"
        _seed_start(state_dir, door_pid=previous.pid, door_started_at=started)
        # ps can no longer answer for the previous pid; every other pid still resolves,
        # so this door can record its own identity and reach the refire decision.
        monkeypatch.setenv("PATH", _write_ps(tmp_path / "partial", fail_for=previous.pid))
        with pytest.raises(dispatch_start.StartRefused) as excinfo:
            _begin(state_dir, spec_file)
    finally:
        previous.kill()
        previous.wait(timeout=30)

    assert excinfo.value.code == "refire_liveness_unknown"
    assert len(_starts(state_dir)) == 1, "an attempt-2 line was written on an unknown probe"


def test_a_refire_over_a_real_dead_door_goes_ahead(state_dir, spec_file, v3, clock):
    pid, started = _dead_pid()
    _seed_start(state_dir, door_pid=pid, door_started_at=started)

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 2
    assert [line["attempt"] for line in _starts(state_dir)] == [1, 2]


# ---------------------------------------------------------------------------
# rule 4: the sweep keeps its unknown state with the same unavailable ps
# ---------------------------------------------------------------------------

def test_the_sweep_never_abandons_on_an_unavailable_ps(
        state_dir, v3, clock, tmp_path, monkeypatch):
    pid, started = _dead_pid()
    _insert_row(state_dir)
    _seed_start(state_dir, door_pid=pid, door_started_at=started,
                deadline_seconds=DEADLINE)
    monkeypatch.setenv("PATH", _write_ps(tmp_path / "unavailable"))

    result = lifecycle.sweep(state_dir=state_dir, now=START_DT + timedelta(hours=1))

    assert result.abandoned == 0, result
    assert result.unknown_liveness == 1, result
    assert _finished(state_dir) == []
