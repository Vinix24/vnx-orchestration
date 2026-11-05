"""ADR-045 audit-spoor-kern K-4a: the core of the hand-over in ``dispatch_start``.

Dispatch-ID: dlv-01e8d854e98c

The module is not wired into the door yet (K-4b/K-4c); every test drives it
directly against real stores in ``tmp_path``. One block per OI-2510 point:

1. the frozen ``Handover`` and the named ``StartRefused`` codes;
2. ``begin`` takes an already resolved ``base_sha`` and resolves nothing itself;
3. only the exact ``(project_id, dispatch_id)`` key counts, never a substring;
4. one locked operation: two concurrent hand-overs never pick the same attempt,
   and the append layer's ``duplicate`` verdict is ``start_line_duplicate``;
5. legacy never takes the lock, writes no line and needs no door identity,
   while v3 with an unreadable door start time refuses;
6. a stored count survives a failed read; "no row yet" is attempt 1;
7. a failed ``lifecycle_mode`` write is a loud WARNING and the hand-over goes on;
8. ``record_child`` writes the spawn file for v3 only.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util as _ilu
import json
import logging
import os
import sqlite3
import sys
import threading
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from append_receipt_internals.lifecycle import writer_sha  # noqa: E402
from coordination_db import get_connection  # noqa: E402
from dispatch_worktree_isolation import claims_dir_for_state_dir  # noqa: E402

PROJECT = "proj-alpha"
OTHER_PROJECT = "proj-beta"
DISPATCH = "d-k4a"
BASE_SHA = "a" * 40
ROLE = "backend-developer"

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k4a", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


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
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def legacy(monkeypatch):
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


def _begin(state_dir: Path, spec_file: Path, **overrides) -> dispatch_start.Handover:
    kwargs = dict(project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
                  spec_file=spec_file, target_slot="T1", state_dir=state_dir)
    kwargs.update(overrides)
    return dispatch_start.begin(**kwargs)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _starts(state_dir: Path, dispatch_id: str = DISPATCH, project_id: str = PROJECT) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == "dispatch_started"
            and line.get("dispatch_id") == dispatch_id
            and line.get("project_id") == project_id]


def _seed_line(state_dir: Path, line: dict) -> None:
    with _ledger(state_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


def _start_line(**overrides) -> dict:
    line = {"timestamp": "2026-11-01T10:00:00Z", "event_type": "dispatch_started",
            "project_id": PROJECT, "dispatch_id": DISPATCH, "attempt": 1, "role": ROLE,
            "base_sha": BASE_SHA, "spec_sha256": "b" * 64, "lifecycle_mode": "v3",
            "door_pid": os.getpid(), "door_started_at": dispatch_start.process_started_at(os.getpid()),
            "requested_by": "terminal:T1", "writer_sha": "c" * 40, "schema_version": 3}
    line.update(overrides)
    return line


def _insert_row(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                attempt_count: int = 1, metadata: str = "{}") -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, "running", attempt_count, metadata))
        conn.commit()


def _row(state_dir: Path, dispatch_id: str = DISPATCH, project_id: str = PROJECT) -> dict:
    with get_connection(state_dir) as conn:
        row = conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (dispatch_id, project_id)).fetchone()
    return dict(row) if row is not None else {}


def _metadata(row: dict) -> dict:
    return json.loads(row.get("metadata_json") or "{}")


# --- OI-2510 points 1 and 2 -------------------------------------------------

def test_handover_is_frozen_and_refusal_names_its_code():
    handover = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=DISPATCH, attempt=1, lifecycle_mode="v3",
        door_pid=os.getpid(), door_started_at="Mon Nov  3 10:00:00 2026", started_at="2026-11-03T10:00:00Z")
    assert handover.is_v3 is True and handover.mode_write == "ok"
    with pytest.raises(FrozenInstanceError):
        handover.attempt = 2
    refused = dispatch_start.StartRefused("refire_attempt_live", "attempt 1 is live")
    assert refused.code == "refire_attempt_live"
    assert "attempt 1 is live" in str(refused)


def test_begin_writes_the_given_base_sha_and_resolves_no_ref(
        state_dir, spec_file, v3, monkeypatch):
    writer_sha()  # warm the append layer's cache: only the door may call out now
    calls = []
    real_run = dispatch_start.subprocess.run

    def _spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(dispatch_start.subprocess, "run", _spy)
    handover = _begin(state_dir, spec_file, base_sha="refs/heads/not-resolved")
    assert handover.attempt == 1 and handover.is_v3 and handover.mode_write == "ok"
    line = _starts(state_dir)[0]
    assert line["base_sha"] == "refs/heads/not-resolved"
    assert not [cmd for cmd in calls if "git" in cmd], calls


def test_happy_path_records_the_line_then_the_projection(state_dir, spec_file, v3):
    _insert_row(state_dir, attempt_count=1)
    handover = _begin(state_dir, spec_file)
    assert handover.door_pid == os.getpid()
    assert handover.door_started_at == dispatch_start.process_started_at(os.getpid())
    lines = _starts(state_dir)
    assert len(lines) == 1
    line = lines[0]
    assert line["project_id"] == PROJECT and line["dispatch_id"] == DISPATCH
    assert line["attempt"] == 1 and line["role"] == ROLE and line["base_sha"] == BASE_SHA
    assert line["spec_sha256"] == hashlib.sha256(spec_file.read_bytes()).hexdigest()
    assert line["lifecycle_mode"] == "v3" and line["door_started_at"] == handover.door_started_at
    assert line["requested_by"] == "terminal:T1"
    assert line["schema_version"] == 3 and line["writer_sha"]
    row = _row(state_dir)
    assert row["attempt_count"] == 1 and _metadata(row)["lifecycle_mode"] == "v3"


# --- OI-2510 point 3: the exact key, never a substring ----------------------

def test_job_extra_start_does_not_block_job(state_dir, spec_file, v3):
    live = dispatch_start.process_started_at(os.getpid())
    _seed_line(state_dir, _start_line(dispatch_id="job-extra", door_started_at=live))
    _seed_line(state_dir, _start_line(dispatch_id=DISPATCH, project_id=OTHER_PROJECT))
    handover = _begin(state_dir, spec_file)
    assert handover.attempt == 1
    assert len(_starts(state_dir)) == 1
    assert len(_starts(state_dir, dispatch_id="job-extra")) == 1


def test_live_job_start_still_refuses_after_job_extra_finishes(state_dir, spec_file, v3):
    live = dispatch_start.process_started_at(os.getpid())
    _seed_line(state_dir, _start_line(attempt=1, door_started_at=live))
    _seed_line(state_dir, {"timestamp": "2026-11-01T10:05:00Z", "event_type": "dispatch_finished",
                           "project_id": PROJECT, "dispatch_id": "job-extra", "attempt": 1,
                           "end_state": "completed", "writer_sha": "c" * 40, "schema_version": 3})
    _seed_line(state_dir, {"timestamp": "2026-11-01T10:05:01Z", "event_type": "dispatch_finished",
                           "project_id": PROJECT, "dispatch_id": DISPATCH, "attempt": 1,
                           "end_state": "completed", "writer_sha": "c" * 40, "schema_version": 3})
    # The finish above is attempt 1's own end, so the refire is attempt 2, not a refusal.
    handover = _begin(state_dir, spec_file)
    assert handover.attempt == 2
    assert len(_starts(state_dir)) == 2


def test_live_job_start_refuses_the_refire(state_dir, spec_file, v3):
    live = dispatch_start.process_started_at(os.getpid())
    _seed_line(state_dir, _start_line(attempt=1, door_started_at=live))
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "refire_attempt_live"
    assert len(_starts(state_dir)) == 1


def test_liveness_from_the_spawn_file(state_dir, spec_file, v3):
    _seed_line(state_dir, _start_line(attempt=1, door_pid=999999, door_started_at="long gone"))
    spawn = dispatch_start.spawn_file_path(state_dir, PROJECT, DISPATCH, 1)
    spawn.parent.mkdir(parents=True, exist_ok=True)
    spawn.write_text(json.dumps({"child_pid": os.getpid(),
                                 "child_started_at": dispatch_start.process_started_at(os.getpid())}),
                     encoding="utf-8")
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "refire_attempt_live"


def test_liveness_from_the_occupancy_flock(state_dir, spec_file, v3):
    _seed_line(state_dir, _start_line(attempt=1, door_pid=999999, door_started_at="long gone"))
    claims = claims_dir_for_state_dir(state_dir)
    claims.mkdir(parents=True, exist_ok=True)
    with (claims / f"{DISPATCH}.occupancy").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dispatch_start.StartRefused) as excinfo:
            _begin(state_dir, spec_file)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert excinfo.value.code == "refire_attempt_live"


# --- OI-2510 point 4: one locked operation, never two hand-overs ------------

def _fire(barrier, out, index, state_dir, spec_file, dispatch_id):
    barrier.wait(timeout=30)
    try:
        out[index] = _begin(state_dir, spec_file, dispatch_id=dispatch_id)
    except dispatch_start.StartRefused as exc:
        out[index] = exc


def test_concurrent_handovers_pick_exactly_one_attempt(state_dir, spec_file, v3):
    for round_index in range(20):
        dispatch_id = f"d-race-{round_index}"
        barrier = threading.Barrier(2)
        out = [None, None]
        threads = [threading.Thread(target=_fire, args=(barrier, out, i, state_dir, spec_file, dispatch_id))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        handovers = [item for item in out if isinstance(item, dispatch_start.Handover)]
        refusals = [item for item in out if isinstance(item, dispatch_start.StartRefused)]
        assert len(handovers) == 1, (round_index, out)
        assert len(refusals) == 1, (round_index, out)
        assert refusals[0].code in {"refire_attempt_live", "start_line_duplicate"}
        assert len(_starts(state_dir, dispatch_id=dispatch_id)) == 1
    assert len({(item.dispatch_id, item.attempt) for item in handovers}) == 1


def test_append_duplicate_is_a_named_refusal(state_dir, spec_file, v3, monkeypatch):
    monkeypatch.setattr(payload_module, "append_receipt_payload",
                        lambda receipt, **kwargs: types.SimpleNamespace(status="duplicate"))
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "start_line_duplicate"
    assert not _lines(state_dir)


def test_unconfirmed_append_is_a_named_refusal(state_dir, spec_file, v3, monkeypatch):
    monkeypatch.setattr(payload_module, "append_receipt_payload",
                        lambda receipt, **kwargs: types.SimpleNamespace(status="queued"))
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "start_line_unconfirmed"


def test_the_lock_is_bounded(state_dir, spec_file, v3, monkeypatch):
    monkeypatch.setattr(dispatch_start, "HANDOVER_LOCK_TIMEOUT_SECONDS", 0.2)
    lock_path = dispatch_start.handover_lock_path(state_dir, PROJECT, DISPATCH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dispatch_start.StartRefused) as excinfo:
            _begin(state_dir, spec_file)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert excinfo.value.code == "handover_lock_timeout"
    assert not _lines(state_dir)


# --- OI-2510 point 5: legacy and the door identity --------------------------

def test_legacy_without_ps_writes_no_lock_and_no_line(state_dir, spec_file, legacy, monkeypatch):
    monkeypatch.setattr(dispatch_start, "process_started_at", lambda pid: None)
    _insert_row(state_dir, attempt_count=2)
    handover = _begin(state_dir, spec_file)
    assert handover.lifecycle_mode == "legacy" and handover.is_v3 is False
    assert handover.attempt == 2 and handover.door_started_at == ""
    assert handover.mode_write == "ok"
    assert not _ledger(state_dir).exists()
    assert not dispatch_start.handover_lock_path(state_dir, PROJECT, DISPATCH).exists()
    row = _row(state_dir)
    assert row["attempt_count"] == 2 and _metadata(row)["lifecycle_mode"] == "legacy"


def test_legacy_handover_never_probes_ps(state_dir, spec_file, legacy, monkeypatch):
    """Legacy needs no door identity, so it must not pay for the probe at all (OI-2510 p. 3):
    zero ``process_started_at`` calls, an empty ``door_started_at`` and the door's own pid."""
    calls: list[object] = []

    def _spy(pid):
        calls.append(pid)
        return "Mon Nov  3 10:00:00 2026"

    monkeypatch.setattr(dispatch_start, "process_started_at", _spy)
    _insert_row(state_dir, attempt_count=2)
    handover = _begin(state_dir, spec_file)
    assert calls == [], f"the legacy hand-over probed ps {len(calls)} time(s)"
    assert handover.lifecycle_mode == "legacy" and handover.is_v3 is False
    assert handover.door_started_at == ""
    assert handover.door_pid == os.getpid()
    assert handover.attempt == 2 and handover.mode_write == "ok"
    assert not _ledger(state_dir).exists()
    assert _metadata(_row(state_dir))["lifecycle_mode"] == "legacy"


def test_v3_without_ps_refuses_without_writing(state_dir, spec_file, v3, monkeypatch):
    monkeypatch.setattr(dispatch_start, "process_started_at", lambda pid: None)
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "door_identity_unresolved"
    assert not _ledger(state_dir).exists()
    assert not dispatch_start.handover_lock_path(state_dir, PROJECT, DISPATCH).exists()


# --- OI-2510 point 6: stored count, no row yet, read failure ----------------

def test_stored_count_survives_a_failing_read_legacy(state_dir, spec_file, legacy, monkeypatch):
    _insert_row(state_dir, attempt_count=5)

    def _broken(state_dir_arg):
        raise sqlite3.OperationalError("count read failed")

    monkeypatch.setattr(dispatch_start, "get_connection", _broken)
    handover = _begin(state_dir, spec_file)
    assert handover.attempt == 0
    assert handover.mode_write == "write_failed"
    row = _row(state_dir)
    assert row["attempt_count"] == 5 and row["metadata_json"] == "{}"


def test_stored_count_survives_a_failing_read_v3(state_dir, spec_file, v3, monkeypatch):
    _insert_row(state_dir, attempt_count=5)

    def _broken(state_dir_arg):
        raise sqlite3.OperationalError("count read failed")

    monkeypatch.setattr(dispatch_start, "get_connection", _broken)
    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)
    assert excinfo.value.code == "start_attempt_unreadable"
    assert not _ledger(state_dir).exists()


def test_no_row_yet_is_attempt_one(state_dir, spec_file, v3):
    handover = _begin(state_dir, spec_file)
    assert handover.attempt == 1 and handover.mode_write == "ok"
    assert _starts(state_dir)[0]["attempt"] == 1


def test_no_row_yet_is_attempt_one_legacy(state_dir, spec_file, legacy):
    handover = _begin(state_dir, spec_file)
    assert handover.attempt == 1 and handover.mode_write == "ok"
    assert not _ledger(state_dir).exists()


# --- OI-2510 point 7: a failed projection is loud, the hand-over continues --

def test_failed_lifecycle_mode_write_warns_and_continues(
        state_dir, spec_file, legacy, monkeypatch, caplog):
    _insert_row(state_dir, attempt_count=3)
    real_get_connection = dispatch_start.get_connection
    calls = {"n": 0}

    def _flaky(sd):
        calls["n"] += 1
        if calls["n"] > 1:
            raise sqlite3.OperationalError("projection write failed")
        return real_get_connection(sd)

    monkeypatch.setattr(dispatch_start, "get_connection", _flaky)
    with caplog.at_level(logging.WARNING, logger="dispatch_start"):
        handover = _begin(state_dir, spec_file)
    assert handover.attempt == 3 and handover.mode_write == "write_failed"
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(DISPATCH in message and "attempt=3" in message and "projection write failed" in message
               for message in messages), messages
    row = _row(state_dir)
    assert row["attempt_count"] == 3 and row["metadata_json"] == "{}"


# --- OI-2510 point 8: record_child -----------------------------------------

def test_record_child_writes_the_spawn_file_for_v3(state_dir, spec_file, v3):
    handover = _begin(state_dir, spec_file)
    path = dispatch_start.record_child(handover, state_dir=state_dir, child_pid=os.getpid())
    assert path == dispatch_start.spawn_file_path(state_dir, PROJECT, DISPATCH, 1)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["child_pid"] == os.getpid()
    assert document["child_started_at"] == dispatch_start.process_started_at(os.getpid())
    assert document["dispatch_id"] == DISPATCH and document["attempt"] == 1
    assert not [item.name for item in path.parent.iterdir() if ".tmp" in item.name]


def test_record_child_writes_nothing_for_legacy(state_dir, spec_file, legacy):
    handover = _begin(state_dir, spec_file)
    assert dispatch_start.record_child(handover, state_dir=state_dir, child_pid=os.getpid()) is None
    assert not dispatch_start.spawn_file_path(state_dir, PROJECT, DISPATCH, 1).exists()


def test_record_child_without_a_child_start_time_writes_nothing(
        state_dir, spec_file, v3, monkeypatch):
    handover = _begin(state_dir, spec_file)
    monkeypatch.setattr(dispatch_start, "process_started_at", lambda pid: None)
    assert dispatch_start.record_child(handover, state_dir=state_dir, child_pid=os.getpid()) is None
    assert not dispatch_start.spawn_file_path(state_dir, PROJECT, DISPATCH, 1).exists()


# --- OI-2528 (K-8a r5): the door refuses only on its OWN broken evidence -------

def _raw_lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    return ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []


def _replace_line(state_dir: Path, index: int, fragment: str) -> None:
    lines = _raw_lines(state_dir)
    lines[index] = fragment
    _ledger(state_dir).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_corrupt_unfinished_start_refuses_the_next_handover(state_dir, spec_file, v3):
    """Codex's reproduction: with the unfinished start unparseable, the refire must not pass."""
    _begin(state_dir, spec_file)  # attempt 1 starts, door pid = this live process
    _replace_line(state_dir, 0, f'{{"event_type": "dispatch_started", "dispatch_id": "{DISPATCH}", '
                                f'"attempt": 1, "proj')

    with pytest.raises(dispatch_start.StartRefused) as excinfo:
        _begin(state_dir, spec_file)

    assert excinfo.value.code == "ledger_corrupt"
    assert "line 1" in excinfo.value.detail
    assert len(_raw_lines(state_dir)) == 1  # no attempt-2 line was written


def test_a_corrupt_line_of_another_dispatch_is_skipped_at_the_door(state_dir, spec_file, v3):
    _ledger(state_dir).write_text(
        '{"event_type": "dispatch_started", "dispatch_id": "d-other", "att\n', encoding="utf-8")

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 1
    own = json.loads(_raw_lines(state_dir)[-1])
    assert own["dispatch_id"] == DISPATCH and own["attempt"] == 1


def test_a_torn_final_fragment_is_tolerated_at_the_door(state_dir, spec_file, v3):
    """An unterminated final fragment is a crash mid-append: the records before it count."""
    _seed_line(state_dir, _start_line(attempt=1, door_pid=999999, door_started_at="long gone"))
    with _ledger(state_dir).open("a", encoding="utf-8") as handle:
        handle.write(f'{{"event_type": "dispatch_started", "dispatch_id": "{DISPATCH}", '
                     f'"attempt": 1, "proj')

    handover = _begin(state_dir, spec_file)

    assert handover.attempt == 2
    appended = json.loads(_raw_lines(state_dir)[-1])
    assert appended["dispatch_id"] == DISPATCH and appended["attempt"] == 2


# --- K-6 follow-up (OI-2539): the start line carries its own deadline ---------

def _staged_json_spec(state_dir: Path, *, deadline: int | None = 1800,
                      subdir: str = "pending") -> Path:
    path = state_dir.parent / "dispatches" / subdir / DISPATCH / "dispatch-spec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = {"dispatch_id": DISPATCH}
    if deadline is not None:
        spec["deadline_seconds"] = deadline
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_the_start_line_carries_the_staged_spec_deadline_and_deadline_for_uses_it(
        state_dir, v3):
    """OI-2539: the door rewrites the spec after the start, so the line must keep it."""
    import dispatch_lifecycle
    from final_prompt_integrity import persist_final_prompt

    spec = _staged_json_spec(state_dir, deadline=1800)
    _begin(state_dir, spec)

    line = _starts(state_dir)[0]
    assert line["deadline_seconds"] == 1800

    # The real door then persists the final prompt, rewriting the spec's bytes so
    # ``spec_sha256`` no longer matches (the K-act lane's normal path).
    persist_final_prompt(spec.parent, "the assembled final prompt")
    assert json.loads(spec.read_text(encoding="utf-8"))["final_prompt_sha256"]

    assert dispatch_lifecycle.deadline_for(state_dir, line, None) == (1800, "start_line")


def test_a_spec_without_a_legal_deadline_writes_no_field_on_the_start_line(state_dir, v3):
    spec = _staged_json_spec(state_dir, deadline=None)
    _begin(state_dir, spec)

    assert "deadline_seconds" not in _starts(state_dir)[0]


@pytest.mark.parametrize("deadline", [299, 14_401, True, "1800"])
def test_an_illegal_spec_deadline_writes_no_field_on_the_start_line(
        state_dir, v3, deadline):
    spec = _staged_json_spec(state_dir, deadline=deadline)
    _begin(state_dir, spec)

    assert "deadline_seconds" not in _starts(state_dir)[0]
