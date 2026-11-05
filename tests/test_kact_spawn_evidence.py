"""ADR-045 K-act rule 5 (OI-2532 part 1): the spawn evidence survives a failed probe,
and a spawn file that cannot be read is unknown, never absent.

Dispatch-ID: dlv-21cffd460098

Two codex reproductions on K-act #2386 (``47aea86d``, 04-11):

1. ``record_child`` returned before writing when ``process_started_at(child_pid)``
   came back ``None``. Once the door is gone and its occupancy flock is free, an
   attempt with no spawn file read as dead — so the refire went ahead and the sweep
   abandoned a child that was still running. The writer now records the child with
   ``"child_started_at": null``; ``pid_verdict`` already answers unknown for a live
   pid with no recorded start (K-9d rule 3).
2. ``_liveness`` folded every failure to read the spawn file into ``spawn = None``,
   which is indistinguishable from "no spawn file". A directory, bad JSON or a
   malformed ``child_pid`` at the spawn path therefore let the refire pick attempt 2
   over a child that might still run. Only ``FileNotFoundError`` means "no spawn
   file"; anything else is unknown.

Every child is a real process and every store is a real ``runtime_coordination.db``
under ``tmp_path``; only ``_now`` is pinned. A genuinely failing ``ps`` is put first
on ``PATH`` — no probe, liveness or plan function is patched.
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
DISPATCH = "d-kact"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
T0 = "2026-11-03T10:00:00Z"
START_DT = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
DEADLINE = 300
_REAL_PS = shutil.which("ps") or "/bin/ps"

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_kact", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
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


def _handover(door_pid: int, door_started_at: str) -> dispatch_start.Handover:
    """The attempt-1 hand-over the seeded start line describes (attempt 1, v3)."""
    return dispatch_start.Handover(project_id=PROJECT, dispatch_id=DISPATCH, attempt=1,
                                   lifecycle_mode="v3", door_pid=door_pid,
                                   door_started_at=door_started_at, started_at=T0)


def _spawn_path(state_dir: Path) -> Path:
    return dispatch_start.spawn_file_path(state_dir, PROJECT, DISPATCH, 1)


# ---------------------------------------------------------------------------
# 1. writer: a failed probe still records the child; the refire then refuses
# ---------------------------------------------------------------------------

def test_a_child_recorded_under_a_failed_probe_still_refuses_the_refire(
        state_dir, spec_file, v3, clock, tmp_path, monkeypatch):
    """Codex's reproduction: no spawn file meant "dead" and attempt 2 was written."""
    child = subprocess.Popen(["sleep", "60"])
    door_pid, door_started = _dead_pid()
    try:
        _seed_start(state_dir, door_pid=door_pid, door_started_at=door_started)
        # ps cannot answer for the child, but every other pid (the new door included) still
        # resolves, so the hand-over reaches its refire decision.
        monkeypatch.setenv("PATH", _write_ps(tmp_path / "partial", fail_for=child.pid))
        path = dispatch_start.record_child(_handover(door_pid, door_started),
                                           state_dir=state_dir, child_pid=child.pid)
        assert path is not None, "the spawn evidence was dropped"
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["child_pid"] == child.pid
        assert document["child_started_at"] is None

        with pytest.raises(dispatch_start.StartRefused) as excinfo:
            _begin(state_dir, spec_file)
    finally:
        child.kill()
        child.wait(timeout=30)

    assert excinfo.value.code == "refire_liveness_unknown"
    assert len(_starts(state_dir)) == 1, "an attempt-2 line was written over an unknown child"


# ---------------------------------------------------------------------------
# 2. the sweep keeps the same unknown instead of abandoning
# ---------------------------------------------------------------------------

def test_the_sweep_never_abandons_a_child_behind_a_failed_probe(
        state_dir, v3, clock, tmp_path, monkeypatch):
    child = subprocess.Popen(["sleep", "60"])
    door_pid, door_started = _dead_pid()
    try:
        _insert_row(state_dir)
        _seed_start(state_dir, door_pid=door_pid, door_started_at=door_started,
                    deadline_seconds=DEADLINE)
        monkeypatch.setenv("PATH", _write_ps(tmp_path / "partial", fail_for=child.pid))
        assert dispatch_start.record_child(_handover(door_pid, door_started),
                                           state_dir=state_dir, child_pid=child.pid) is not None

        result = lifecycle.sweep(state_dir=state_dir, now=START_DT + timedelta(hours=1))
    finally:
        child.kill()
        child.wait(timeout=30)

    assert result.abandoned == 0, result
    assert result.unknown_liveness == 1, result
    assert _finished(state_dir) == []


# ---------------------------------------------------------------------------
# 3. a child that has exited still ends, even with an unknown start
# ---------------------------------------------------------------------------

def test_a_dead_child_with_an_unknown_start_still_ends(state_dir, spec_file, v3, clock):
    child = subprocess.Popen(["sleep", "0.3"])
    child.wait(timeout=30)
    door_pid, door_started = _dead_pid()
    _seed_start(state_dir, door_pid=door_pid, door_started_at=door_started)
    path = dispatch_start.record_child(_handover(door_pid, door_started),
                                       state_dir=state_dir, child_pid=child.pid)
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["child_started_at"] is None

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 2
    assert [line["attempt"] for line in _starts(state_dir)] == [1, 2]


# ---------------------------------------------------------------------------
# 4. unreadable child evidence is unknown, not absent
# ---------------------------------------------------------------------------

def _place_evidence(state_dir: Path, case: str) -> None:
    path = _spawn_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if case == "directory":
        path.mkdir()
    elif case == "invalid_json":
        path.write_text("{not json", encoding="utf-8")
    elif case == "missing_pid":
        path.write_text(json.dumps({"version": 1, "child_started_at": None}), encoding="utf-8")
    elif case == "bool_pid":
        path.write_text(json.dumps({"version": 1, "child_pid": True,
                                    "child_started_at": None}), encoding="utf-8")
    else:  # pragma: no cover - the parameter list is closed
        raise AssertionError(case)


@pytest.mark.parametrize("case", ["directory", "invalid_json", "missing_pid", "bool_pid"])
def test_unreadable_child_evidence_refuses_the_refire(
        state_dir, spec_file, v3, clock, case):
    door_pid, door_started = _dead_pid()
    _seed_start(state_dir, door_pid=door_pid, door_started_at=door_started)
    _place_evidence(state_dir, case)

    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)

    assert excinfo.value.code == "refire_liveness_unknown"
    assert len(_starts(state_dir)) == 1, "an attempt-2 line was written over unreadable evidence"
