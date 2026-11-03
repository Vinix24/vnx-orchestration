"""K-4 (ADR-045 §1/§3/§5): the ``dispatch_started`` line at the door's hand-over.

The door owns a dispatch until it hands the run to a lane. That hand-over is the
one place a start line can honestly be written: before the occupancy slot and
before the spawn. Order is binding — ledger line first, then ``lifecycle_mode``
on the ``dispatches`` row, then the slot and the spawn — and with
``ledger_lifecycle`` on a start line that cannot be written refuses the fire
instead of running it untraced.

``dispatch_start`` is imported INSIDE the tests, never at module scope: on
origin/main the module does not exist, and the red run must fail on behaviour
(no start line is ever written, no spawn hook is ever offered) rather than on a
collection-time ImportError that says nothing about the door.

Every store lives under ``tmp_path``; nothing here reads or writes the real
central ledger, and no test ever spawns a real worker.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import config_registry  # noqa: E402
from append_receipt_internals import payload as append_payload  # noqa: E402
from append_receipt_internals.common import AppendReceiptError, AppendResult  # noqa: E402
from dispatch_cli import run_dispatch  # noqa: E402
from test_dispatch_door_row import (  # noqa: E402
    _make_bundle,
    _make_coordination_db,
    _read_attempts,
    _read_row,
)

PROJECT_ID = "vnx-dev"
START = "dispatch_started"
FINISH = "dispatch_finished"


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Keep the config precedence chain env-only (step 2 is the project DB)."""
    monkeypatch.setattr(config_registry, "_db_resolver", None, raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_VNX_LEDGER_LIFECYCLE", raising=False)


# ---------------------------------------------------------------------------
# Stores, all under tmp_path
# ---------------------------------------------------------------------------

def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path, dispatch_id: str) -> List[Dict[str, Any]]:
    path = _ledger(state_dir)
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and line.get("dispatch_id") == dispatch_id:
            out.append(line)
    return out


def _starts(state_dir: Path, dispatch_id: str) -> List[Dict[str, Any]]:
    return [line for line in _lines(state_dir, dispatch_id) if line.get("event_type") == START]


def _append_raw(state_dir: Path, line: Dict[str, Any]) -> None:
    """Write one raw ledger line — the shape an earlier door left behind."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with _ledger(state_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _spawn_path(state_dir: Path, dispatch_id: str, attempt: int) -> Path:
    return state_dir / "dispatch_end" / PROJECT_ID / f"{dispatch_id}.{attempt}.spawn.json"


def _seed_row(state_dir: Path, dispatch_id: str, *, attempts: int = 1) -> Path:
    """A dispatches row the way the door's own bookkeeping leaves it."""
    db_path = _make_coordination_db(state_dir)
    from coordination_db import get_connection  # noqa: PLC0415
    from runtime_coordination import increment_attempt_count, register_dispatch  # noqa: PLC0415

    with get_connection(state_dir) as conn:
        register_dispatch(
            conn, dispatch_id=dispatch_id, project_id=PROJECT_ID, terminal_id=None,
            track=None, priority="P2", pr_ref=None, gate=None, bundle_path=None,
            metadata={"target_slot": "T0"}, actor="test",
        )
        for _ in range(attempts):
            increment_attempt_count(conn, dispatch_id)
        conn.commit()
    return db_path


def _git_repo(tmp_path: Path) -> Path:
    """A disposable repo with an ``origin/main`` branch (no remote needed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "vnx-test",
           "GIT_AUTHOR_EMAIL": "vnx-test@example.invalid",
           "GIT_COMMITTER_NAME": "vnx-test", "GIT_COMMITTER_EMAIL": "vnx-test@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "branch", "origin/main"], cwd=repo, check=True, env=env)
    return repo


def _head_sha(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


@contextmanager
def _no_slot():
    yield


# ---------------------------------------------------------------------------
# The door, fired against tmp stores with the lane faked
# ---------------------------------------------------------------------------

class DoorRun:
    """One door fire with the lane faked and the ordering facts recorded."""

    def __init__(self, tmp_path: Path, monkeypatch, *, lifecycle: bool, dispatch_id: str):
        # The start line resolves its base in the CONSUMER root, the checkout the
        # lane builds in (``resolve_consumer_project_root``); ``base_ref: HEAD``
        # keeps the test independent of a fetched origin/main while still
        # exercising the real ``git rev-parse`` resolution.
        from dispatch_worktree_isolation import resolve_consumer_project_root  # noqa: PLC0415
        self.repo = resolve_consumer_project_root()
        self.data_dir, self.spec_file = _make_bundle(
            tmp_path, staging_id=f"staging-{dispatch_id}", dispatch_id=dispatch_id,
        )
        spec = json.loads(self.spec_file.read_text(encoding="utf-8"))
        spec["base_ref"] = "HEAD"
        self.spec_file.write_text(json.dumps(spec), encoding="utf-8")
        self.db_path = _make_coordination_db(self.data_dir / "state",
                                             tracks={"oi-847-track": "active"})
        self.state_dir = self.data_dir / "state"
        self.dispatch_id = dispatch_id
        monkeypatch.setenv("VNX_DATA_DIR", str(self.data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_OVERRIDE_CLAUDE_HEADLESS", "1")
        if lifecycle:
            monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
        else:
            monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
        self.slot_entries = 0
        self.lane_calls = 0

    @contextmanager
    def door(self, *, slot=None, lane=None):
        door = self

        @contextmanager
        def slot_cm(*_args, **_kwargs):
            door.slot_entries += 1
            if slot is not None:
                slot()
            yield

        def lane_fn(plan, permit, *, state_dir, data_dir, role=None, on_spawn=None):
            door.lane_calls += 1
            return lane(on_spawn) if lane is not None else MagicMock(returncode=0)

        with patch("dispatch_cli.serialize_lane", slot_cm), \
                patch("dispatch_cli.run_envelope_headless_plan", side_effect=lane_fn):
            yield

    def fire(self, **kwargs) -> int:
        with self.door(**kwargs):
            return run_dispatch(self.spec_file)

    def row_metadata(self) -> Dict[str, Any]:
        row = _read_row(self.db_path, self.dispatch_id)
        assert row is not None, "the door must leave a dispatches row"
        return json.loads(row["metadata_json"] or "{}")


# ---------------------------------------------------------------------------
# (a) With the setting on: one line, every field, before the slot; spawn file after
# ---------------------------------------------------------------------------

def test_v3_writes_one_complete_start_line_before_the_slot(tmp_path, monkeypatch):
    """(a) exactly one start line, every required field, before the slot opens."""
    run = DoorRun(tmp_path, monkeypatch, lifecycle=True, dispatch_id="20261103-k4-order")
    seen: Dict[str, Any] = {}

    def asserting_slot():
        seen["starts"] = _starts(run.state_dir, run.dispatch_id)

    with run.door(slot=asserting_slot):
        assert run_dispatch(run.spec_file) == 0

    starts = seen["starts"]
    assert len(starts) == 1, f"exactly one dispatch_started per hand-over, got {starts}"
    line = starts[0]
    assert line["event_type"] == START
    assert line["project_id"] == PROJECT_ID
    assert line["dispatch_id"] == "20261103-k4-order"
    assert line["attempt"] == 1
    assert line["role"] == "backend-developer"
    assert line["base_sha"] == _head_sha(run.repo)
    assert line["spec_sha256"] == hashlib.sha256(run.spec_file.read_bytes()).hexdigest()
    assert line["requested_by"], "requested_by must name who asked for the dispatch"
    assert line["lifecycle_mode"] == "v3"
    assert line["door_pid"] == os.getpid()
    assert line["door_started_at"], "the door's start time is what makes its pid evidence"
    # schema_version and writer_sha come from the append layer's one stamping
    # place (ADR-045 §8); the caller must not have set a second one.
    assert line["schema_version"] == 3
    assert line["writer_sha"]


def test_v3_writes_the_spawn_file_directly_after_the_spawn(tmp_path, monkeypatch):
    """(a) child_pid/child_started_at land in the spawn file, right after the spawn."""
    run = DoorRun(tmp_path, monkeypatch, lifecycle=True, dispatch_id="20261103-k4-spawn")

    def lane(on_spawn):
        assert on_spawn is not None, "a v3 hand-over must offer the spawn hook"
        assert not _spawn_path(run.state_dir, run.dispatch_id, 1).exists(), (
            "the spawn file must not exist before the child is spawned")
        on_spawn(os.getpid())
        return MagicMock(returncode=0)

    assert run.fire(lane=lane) == 0
    assert run.lane_calls == 1

    spawn_file = _spawn_path(run.state_dir, run.dispatch_id, 1)
    assert spawn_file.exists(), "the child must be recorded directly after the spawn"
    payload = json.loads(spawn_file.read_text(encoding="utf-8"))
    assert payload["child_pid"] == os.getpid()
    assert payload["child_started_at"], "a pid without its start time is no evidence"
    assert payload["attempt"] == 1
    assert payload["dispatch_id"] == run.dispatch_id


def test_v3_records_lifecycle_mode_on_the_row(tmp_path, monkeypatch):
    """Step 2 after the line: lifecycle_mode=v3 reaches dispatches.metadata_json."""
    run = DoorRun(tmp_path, monkeypatch, lifecycle=True, dispatch_id="20261103-k4-row")
    assert run.fire() == 0
    assert run.row_metadata()["lifecycle_mode"] == "v3"
    assert _read_row(run.db_path, run.dispatch_id)["attempt_count"] == 1


# ---------------------------------------------------------------------------
# (b) Fail-closed: a start line that cannot be written refuses the fire
# ---------------------------------------------------------------------------

def test_v3_refuses_the_fire_when_the_start_line_cannot_be_written(tmp_path, monkeypatch, capsys):
    run = DoorRun(tmp_path, monkeypatch, lifecycle=True, dispatch_id="20261103-k4-refuse")
    real_append = append_payload.append_receipt_payload

    def appends_or_refuses(receipt, **kwargs):
        if str(receipt.get("event_type")) == START:
            raise AppendReceiptError("ledger_start_unwritable", 4, "simulated append failure")
        return real_append(receipt, **kwargs)

    with patch.object(append_payload, "append_receipt_payload", appends_or_refuses):
        rc = run.fire()

    assert rc == 1, "a start line that cannot be written must refuse the fire"
    err = capsys.readouterr().err
    assert "REJECT [start-line-refused]" in err, err
    assert "ledger_start_unwritable" in err, "the refusal must be named"
    assert run.slot_entries == 0, "no slot may be taken when the line is missing"
    assert run.lane_calls == 0, "nothing may spawn when the line is missing"
    assert "lifecycle_mode" not in run.row_metadata(), (
        "the row must not learn a mode from a hand-over whose line never landed")
    assert not _spawn_path(run.state_dir, run.dispatch_id, 1).exists()
    assert _starts(run.state_dir, run.dispatch_id) == []


# ---------------------------------------------------------------------------
# (c) Off by default: nothing new, and the decision is main's decision
# ---------------------------------------------------------------------------

def test_legacy_writes_no_line_no_spawn_file_and_marks_the_row_legacy(tmp_path, monkeypatch):
    run = DoorRun(tmp_path, monkeypatch, lifecycle=False, dispatch_id="20261103-k4-legacy")
    assert run.fire() == 0

    assert _starts(run.state_dir, run.dispatch_id) == [], (
        "with ledger_lifecycle off no start line may reach the ledger")
    assert not _spawn_path(run.state_dir, run.dispatch_id, 1).exists()
    assert run.row_metadata()["lifecycle_mode"] == "legacy"


def test_legacy_decision_is_main_behaviour(tmp_path, monkeypatch):
    """The off path decides exactly what main decides for the same input.

    Main has no hand-over step at all, so the baseline is the same door fire with
    ``dispatch_start.begin`` stubbed to a side-effect-free legacy hand-over: same
    plan, same permit, same lane choice, no write anywhere.
    """
    run = DoorRun(tmp_path, monkeypatch, lifecycle=False, dispatch_id="20261103-k4-same")
    decisions: List[Dict[str, Any]] = []

    def capture(plan, permit, *, state_dir, data_dir, role=None, handover=None):
        decisions.append({
            "dispatch_id": plan.dispatch_id, "lane": plan.lane, "model": plan.model,
            "provider": getattr(plan.provider, "value", plan.provider),
            "target_id": plan.target_id, "serialization_class": plan.serialization_class,
            "instruction_sha256": plan.instruction_sha256, "base_ref": plan.base_ref,
        })
        return 0

    with patch("dispatch_cli._execute_claude_headless", capture), \
            patch("dispatch_cli.serialize_lane", lambda *a, **k: _no_slot()):
        assert run_dispatch(run.spec_file) == 0

        dispatch_start = importlib.import_module("dispatch_start")

        def main_begin(**kwargs):
            return dispatch_start.Handover(
                project_id=kwargs["project_id"], dispatch_id=kwargs["dispatch_id"],
                attempt=1, lifecycle_mode="legacy", door_pid=os.getpid(),
                door_started_at="main-has-no-hand-over", started_at="main")

        with patch.object(dispatch_start, "begin", main_begin):
            assert run_dispatch(run.spec_file, refire_reason="k4: main baseline") == 0

    assert len(decisions) == 2
    assert decisions[0] == decisions[1], (
        "the shipped (off) path must not change what the door decides")


def test_legacy_hand_over_end_to_end_takes_no_lock_and_writes_no_line(tmp_path, monkeypatch):
    """With the setting off the hand-over is main's: no lock, no line, no spawn
    file, the row marked legacy and the lane still reached."""
    run = DoorRun(tmp_path, monkeypatch, lifecycle=False,
                  dispatch_id="20261103-k4-legacy-pin")
    assert run.fire() == 0

    assert run.lane_calls == 1, "the lane must still be reached"
    assert _starts(run.state_dir, run.dispatch_id) == []
    assert not _spawn_path(run.state_dir, run.dispatch_id, 1).exists()
    assert not (run.state_dir / "dispatch_end" / PROJECT_ID
                / f"{run.dispatch_id}.handover.lock").exists(), (
        "a legacy hand-over must take no per-dispatch lock")
    assert run.row_metadata()["lifecycle_mode"] == "legacy"
    assert _read_row(run.db_path, run.dispatch_id)["state"] == "completed"


# ---------------------------------------------------------------------------
# (d) Two attempts on one id: attempt 1 and attempt 2
# ---------------------------------------------------------------------------

def _begin(state_dir: Path, repo: Path, dispatch_id: str):
    import dispatch_start  # noqa: PLC0415

    return dispatch_start.begin(
        project_id=PROJECT_ID, dispatch_id=dispatch_id, role="backend-developer",
        base_ref="HEAD", spec_file=repo / "seed.txt", target_slot="T0",
        state_dir=state_dir, project_root=repo,
    )


def test_second_fire_writes_attempt_two(tmp_path, monkeypatch):
    """(d) a refire whose predecessor is gone gets the next attempt number."""
    dispatch_start = importlib.import_module("dispatch_start")

    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-two-attempts"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)

    first = _begin(state_dir, repo, dispatch_id)
    assert first.attempt == 1

    # The first door is gone: its pid now reports a start time that is not the
    # one the ledger recorded, which is exactly what a reused pid looks like.
    monkeypatch.setattr(dispatch_start, "process_started_at",
                        lambda pid: "Thu Jan  1 00:00:00 1970")

    assert _begin(state_dir, repo, dispatch_id).attempt == 2

    starts = _starts(state_dir, dispatch_id)
    assert [line["attempt"] for line in starts] == [1, 2], starts
    assert all(line["lifecycle_mode"] == "v3" for line in starts)


# ---------------------------------------------------------------------------
# (e) Refire: refused while the previous attempt is live, allowed otherwise
# ---------------------------------------------------------------------------

def _start_line(dispatch_id: str, attempt: int, *, door_pid: int,
                door_started_at: str) -> Dict[str, Any]:
    return {
        "timestamp": "2026-11-03T10:00:00Z", "event_type": START, "project_id": PROJECT_ID,
        "dispatch_id": dispatch_id, "attempt": attempt, "role": "backend-developer",
        "base_sha": "a" * 40, "spec_sha256": "b" * 64, "requested_by": "terminal:T0",
        "lifecycle_mode": "v3", "door_pid": door_pid, "door_started_at": door_started_at,
        "schema_version": 3, "writer_sha": "c" * 40,
    }


def test_refire_is_refused_while_the_previous_attempt_is_live(tmp_path, monkeypatch):
    """(e) a live door_pid with the same start time still owns the worktree."""
    dispatch_start = importlib.import_module("dispatch_start")

    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-live"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    started = dispatch_start.process_started_at(os.getpid())
    assert started, "the test process must have a readable start time"
    _append_raw(state_dir, _start_line(dispatch_id, 1, door_pid=os.getpid(),
                                       door_started_at=started))

    with pytest.raises(dispatch_start.StartRefused) as refused:
        _begin(state_dir, repo, dispatch_id)

    assert refused.value.code == "refire_attempt_live"
    assert "door_pid" in refused.value.detail
    assert len(_starts(state_dir, dispatch_id)) == 1, "a refused refire writes nothing"


def test_refire_is_allowed_after_an_end_line(tmp_path, monkeypatch):
    """(e) an attempt that has an end line no longer blocks the next one."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-ended"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    _append_raw(state_dir, _start_line(dispatch_id, 1, door_pid=os.getpid(),
                                       door_started_at="Thu Jan  1 00:00:00 1970"))
    _append_raw(state_dir, {"event_type": FINISH, "project_id": PROJECT_ID,
                            "dispatch_id": dispatch_id, "attempt": 1,
                            "end_state": "completed"})

    assert _begin(state_dir, repo, dispatch_id).attempt == 2
    assert [line["attempt"] for line in _starts(state_dir, dispatch_id)] == [1, 2]


def test_refire_is_allowed_when_the_pid_was_reused(tmp_path, monkeypatch):
    """(e) a reused pid carries a different start time, so it reads as dead."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-reused"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    _append_raw(state_dir, _start_line(dispatch_id, 1, door_pid=os.getpid(),
                                       door_started_at="Thu Jan  1 00:00:00 1970"))

    assert _begin(state_dir, repo, dispatch_id).attempt == 2, (
        "pid reuse must never read as a live predecessor")
    assert [line["attempt"] for line in _starts(state_dir, dispatch_id)] == [1, 2]


# ---------------------------------------------------------------------------
# (e2) The ledger key is exact: a longer id that CONTAINS this one is not this one
# ---------------------------------------------------------------------------

def test_a_finish_of_a_longer_id_never_ends_this_attempt(tmp_path, monkeypatch):
    """Codex's reproduction: a ``job-extra`` finish must not close ``job`` attempt 1.

    A substring prefilter admits ``job-extra``'s records; only an exact
    ``dispatch_id`` match may let them count as this dispatch's start or end.
    """
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-job"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    started = dispatch_start.process_started_at(os.getpid())
    assert started, "the test process must have a readable start time"
    _append_raw(state_dir, _start_line(dispatch_id, 1, door_pid=os.getpid(),
                                       door_started_at=started))
    _append_raw(state_dir, {"event_type": FINISH, "project_id": PROJECT_ID,
                            "dispatch_id": f"{dispatch_id}-extra", "attempt": 1,
                            "end_state": "completed"})

    with pytest.raises(dispatch_start.StartRefused) as refused:
        _begin(state_dir, repo, dispatch_id)

    assert refused.value.code == "refire_attempt_live", (
        "the foreign finish must play no part; the live attempt 1 still owns the worktree")
    assert [line["attempt"] for line in _starts(state_dir, dispatch_id)] == [1], (
        "a refused refire writes nothing")


def test_a_start_of_a_longer_id_does_not_block_this_dispatch(tmp_path, monkeypatch):
    """The mirror: ``job-extra``'s live start is not ``job``'s start."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-mirror"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    started = dispatch_start.process_started_at(os.getpid())
    assert started, "the test process must have a readable start time"
    _append_raw(state_dir, _start_line(f"{dispatch_id}-extra", 1, door_pid=os.getpid(),
                                       door_started_at=started))

    assert _begin(state_dir, repo, dispatch_id).attempt == 1, (
        "another dispatch's live start must not refuse this one")
    assert [line["attempt"] for line in _starts(state_dir, dispatch_id)] == [1]


# ---------------------------------------------------------------------------
# (e3) One locked attempt choice: concurrent hand-overs cannot share an attempt
# ---------------------------------------------------------------------------

def test_concurrent_handovers_choose_exactly_one_attempt(tmp_path, monkeypatch):
    """Two hand-overs of one dispatch, released together: exactly one start line,
    exactly one Handover, the other refused; never two hand-overs on one attempt.

    ``_base_sha`` is slowed just enough that both hand-overs have chosen their
    attempt before either appends — the race the per-dispatch lock must close.
    Repeated 20 times so a lucky schedule cannot pass it.
    """
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    repo = _git_repo(tmp_path)
    real_base_sha = dispatch_start._base_sha

    def slow_base_sha(root, ref):
        time.sleep(0.1)
        return real_base_sha(root, ref)

    monkeypatch.setattr(dispatch_start, "_base_sha", slow_base_sha)

    for round_no in range(20):
        dispatch_id = f"20261103-k4-race-{round_no}"
        state_dir = tmp_path / f"race-{round_no}" / "state"
        _seed_row(state_dir, dispatch_id, attempts=1)
        barrier = threading.Barrier(2)
        outcomes: List[tuple] = []

        def hand_over():
            try:
                barrier.wait(timeout=10)
                outcomes.append(("handover", _begin(state_dir, repo, dispatch_id)))
            except dispatch_start.StartRefused as exc:
                outcomes.append(("refused", exc))
            except BaseException as exc:  # recorded and asserted below, never swallowed
                outcomes.append(("error", exc))

        threads = [threading.Thread(target=hand_over) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), (
            f"round {round_no}: a hand-over hung on the lock")

        errors = [value for kind, value in outcomes if kind == "error"]
        assert errors == [], f"round {round_no}: unexpected hand-over errors: {errors}"
        handovers = [value for kind, value in outcomes if kind == "handover"]
        refusals = [value for kind, value in outcomes if kind == "refused"]
        starts = _starts(state_dir, dispatch_id)

        assert len(starts) == 1, f"round {round_no}: exactly one start line, got {starts}"
        assert len(handovers) == 1, (
            f"round {round_no}: exactly one hand-over may win an attempt, got {len(handovers)}")
        assert len(refusals) == 1, (
            f"round {round_no}: the loser must be refused, got {[r.code for r in refusals]}")
        assert refusals[0].code in ("refire_attempt_live", "handover_lock_timeout"), (
            f"round {round_no}: the refusal must be named, got {refusals[0].code!r}")
        assert starts[0]["attempt"] == handovers[0].attempt, (
            f"round {round_no}: the returned attempt must own the only start line")


def test_a_duplicate_start_line_is_refused(tmp_path, monkeypatch):
    """The append layer calling the line a duplicate means this hand-over does not
    own the identity: ``begin`` refuses with ``start_line_duplicate``."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-dup"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)

    def duplicate(receipt, **kwargs):
        return AppendResult(status="duplicate",
                            receipts_file=Path(kwargs["receipts_file"]), idempotency_key="dup")

    with patch.object(append_payload, "append_receipt_payload", duplicate):
        with pytest.raises(dispatch_start.StartRefused) as refused:
            _begin(state_dir, repo, dispatch_id)

    assert refused.value.code == "start_line_duplicate"
    assert _starts(state_dir, dispatch_id) == [], (
        "a hand-over the append layer deduplicated may not report a line of its own")


def test_an_unconfirmed_start_line_is_refused(tmp_path, monkeypatch):
    """Any other non-``appended`` verdict is not a written line: fail closed."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-unconfirmed"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)

    def strange(receipt, **kwargs):
        return AppendResult(status="queued_elsewhere",
                            receipts_file=Path(kwargs["receipts_file"]), idempotency_key="x")

    with patch.object(append_payload, "append_receipt_payload", strange):
        with pytest.raises(dispatch_start.StartRefused) as refused:
            _begin(state_dir, repo, dispatch_id)

    assert refused.value.code == "start_line_unconfirmed"
    assert _starts(state_dir, dispatch_id) == []


def test_a_taken_handover_lock_refuses_with_a_named_timeout(tmp_path, monkeypatch):
    """A lock another hand-over still holds past the bound is a named refusal, not a wait."""
    import fcntl  # noqa: PLC0415

    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.setattr(dispatch_start, "HANDOVER_LOCK_TIMEOUT_SECONDS", 0.2)
    dispatch_id = "20261103-k4-lock-timeout"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    lock_path = dispatch_start.handover_lock_path(state_dir, PROJECT_ID, dispatch_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(dispatch_start.StartRefused) as refused:
            _begin(state_dir, repo, dispatch_id)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert refused.value.code == "handover_lock_timeout"
    assert _starts(state_dir, dispatch_id) == [], "a timed-out hand-over writes nothing"


# ---------------------------------------------------------------------------
# (e4) The base SHA is the consumer repo's, the one the lane builds in
# ---------------------------------------------------------------------------

def test_hand_over_resolves_the_base_in_the_consumer_repo(tmp_path, monkeypatch):
    """A branch that exists only in the consumer repo must resolve, and its sha
    must be what the start line carries — not the engine checkout's."""
    import dispatch_worktree_isolation  # noqa: PLC0415

    run = DoorRun(tmp_path, monkeypatch, lifecycle=True,
                  dispatch_id="20261103-k4-consumer-base")
    consumer = _git_repo(tmp_path)
    branch = f"consumer-only-{run.dispatch_id}"
    subprocess.run(["git", "-C", str(consumer), "branch", branch], check=True)
    consumer_sha = _head_sha(consumer)
    spec = json.loads(run.spec_file.read_text(encoding="utf-8"))
    spec["base_ref"] = branch
    run.spec_file.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(dispatch_worktree_isolation, "resolve_consumer_project_root",
                        lambda: consumer)

    assert run.fire() == 0, "the door must resolve the consumer-only branch"

    starts = _starts(run.state_dir, run.dispatch_id)
    assert len(starts) == 1
    assert starts[0]["base_sha"] == consumer_sha, (
        "the start line must record the consumer repo's base, not the engine checkout's")


# ---------------------------------------------------------------------------
# (e5) A refused start leaves no dispatch in delivering
# ---------------------------------------------------------------------------

def _seed_live_attempt(db_path: Path, dispatch_id: str) -> str:
    """The door's fixture: a claimable row plus one already-live attempt."""
    from coordination_db import get_connection  # noqa: PLC0415
    from runtime_coordination import (  # noqa: PLC0415
        create_attempt, increment_attempt_count, register_dispatch, update_attempt,
    )

    with get_connection(db_path.parent) as conn:
        register_dispatch(
            conn, dispatch_id=dispatch_id, project_id=PROJECT_ID, terminal_id=None,
            track=None, priority="P2", pr_ref=None, gate=None, bundle_path=None,
            metadata={"target_slot": "T0"}, actor="test",
        )
        increment_attempt_count(conn, dispatch_id)
        attempt = create_attempt(
            conn, dispatch_id=dispatch_id, project_id=PROJECT_ID, terminal_id="T0",
            attempt_number=1, actor="test",
        )
        update_attempt(conn, attempt_id=attempt["attempt_id"], state="running", actor="test")
        conn.commit()
    return attempt["attempt_id"]


def test_a_refused_start_closes_only_the_door_owned_attempt(tmp_path, monkeypatch, capsys):
    """A refused begin must not leave the door-owned row in ``delivering`` with no
    worker: its own attempt is closed with the refusal as reason, and an attempt
    belonging to someone else is untouched."""
    run = DoorRun(tmp_path, monkeypatch, lifecycle=True,
                  dispatch_id="20261103-k4-refused-close")
    _seed_live_attempt(run.db_path, run.dispatch_id)
    real_append = append_payload.append_receipt_payload

    def refuse(receipt, **kwargs):
        if str(receipt.get("event_type")) == START:
            raise AppendReceiptError("ledger_start_unwritable", 4, "simulated append failure")
        return real_append(receipt, **kwargs)

    with patch.object(append_payload, "append_receipt_payload", refuse):
        assert run.fire() == 1

    assert "REJECT [start-line-refused]" in capsys.readouterr().err
    row = _read_row(run.db_path, run.dispatch_id)
    assert row["state"] == "failed_delivery", (
        f"a refused start must close the door-owned row, got {row['state']!r}")

    attempts = {attempt["attempt_number"]: attempt
                for attempt in _read_attempts(run.db_path, run.dispatch_id)}
    assert sorted(attempts) == [1, 2], f"the fixture must leave two attempts: {attempts}"
    assert attempts[1]["state"] == "running", (
        "another attempt of the same dispatch must be left alone")
    assert attempts[1]["failure_reason"] is None
    assert attempts[2]["state"] == "failed", (
        "the door-owned attempt must be closed on the refusal")
    assert "ledger_start_unwritable" in (attempts[2]["failure_reason"] or "")


# ---------------------------------------------------------------------------
# (f) A failed row projection is loud and named, never silent
# ---------------------------------------------------------------------------

def test_a_failed_lifecycle_mode_write_is_named_and_carried(tmp_path, monkeypatch, caplog):
    """The ledger is the source, so the fire goes on — but the failed projection is
    logged with the key and the exception, and the Handover carries it."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-mode-write"
    state_dir = tmp_path / "state"
    _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("coordination db gone")

    # The attempt read succeeded before the store died; the mode write is what fails.
    with patch.object(dispatch_start, "read_attempt_count", lambda *a, **k: 1), \
            patch.object(dispatch_start, "get_connection", boom), \
            caplog.at_level(logging.WARNING, logger="dispatch_start"):
        handover = _begin(state_dir, repo, dispatch_id)

    assert handover.mode_write == "write_failed", (
        "the door must be able to name the failed projection")
    assert handover.attempt == 1
    assert [line["attempt"] for line in _starts(state_dir, dispatch_id)] == [1], (
        "the ledger line landed, so the fire proceeds")
    warnings = [rec.getMessage() for rec in caplog.records
                if rec.levelno == logging.WARNING and rec.name == "dispatch_start"]
    assert warnings, "a failed mode write must not be swallowed silently"
    text = "\n".join(warnings)
    assert dispatch_id in text, f"the warning must name the dispatch: {text}"
    assert "attempt" in text and "1" in text, f"the warning must name the attempt: {text}"
    assert "coordination db gone" in text, f"the warning must carry the exception: {text}"


# ---------------------------------------------------------------------------
# (g) Legacy needs no door identity; v3 does
# ---------------------------------------------------------------------------

def _row_metadata(db_path: Path, dispatch_id: str) -> Dict[str, Any]:
    row = _read_row(db_path, dispatch_id)
    assert row is not None, "the seeded row must exist"
    return json.loads(row["metadata_json"] or "{}")


def test_legacy_without_a_readable_door_start_time_still_hands_over(tmp_path, monkeypatch):
    """With the setting off nothing new may refuse: no ``ps``, no refusal."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    dispatch_id = "20261103-k4-legacy-no-ps"
    state_dir = tmp_path / "state"
    db_path = _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(dispatch_start, "process_started_at", lambda pid: None)

    handover = _begin(state_dir, repo, dispatch_id)

    assert handover.lifecycle_mode == "legacy"
    assert handover.door_pid == os.getpid(), "a legacy hand-over records what it can"
    assert not handover.door_started_at, "it records no start time it could not read"
    assert handover.mode_write == "ok"
    assert _starts(state_dir, dispatch_id) == [], "a legacy hand-over writes no line"
    assert _row_metadata(db_path, dispatch_id)["lifecycle_mode"] == "legacy", (
        "the row still learns the mode")


def test_v3_without_a_readable_door_start_time_is_refused(tmp_path, monkeypatch):
    """A v3 start line cannot record liveness without it, so the fire is refused."""
    dispatch_start = importlib.import_module("dispatch_start")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k4-v3-no-ps"
    state_dir = tmp_path / "state"
    db_path = _seed_row(state_dir, dispatch_id, attempts=1)
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(dispatch_start, "process_started_at", lambda pid: None)

    with pytest.raises(dispatch_start.StartRefused) as refused:
        _begin(state_dir, repo, dispatch_id)

    assert refused.value.code == "door_identity_unresolved"
    assert _starts(state_dir, dispatch_id) == [], "nothing may be written"
    assert "lifecycle_mode" not in _row_metadata(db_path, dispatch_id)


# ---------------------------------------------------------------------------
# The hook itself: deliver() hands over the child's pid right after Popen
# ---------------------------------------------------------------------------

def _deliver_with_spawn_hook(on_spawn):
    from subprocess_adapter import SubprocessAdapter  # noqa: PLC0415

    adapter = SubprocessAdapter()
    adapter.spawn("T1", {})

    def mock_popen(cmd, **kwargs):
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 4242
        process.returncode = None
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        return process

    with patch("subprocess_adapter.subprocess.Popen", side_effect=mock_popen):
        return adapter.deliver("T1", "dispatch-k4", instruction="do work", model="sonnet",
                               on_spawn=on_spawn)


def test_deliver_calls_on_spawn_with_the_child_pid():
    seen: List[int] = []
    assert _deliver_with_spawn_hook(seen.append).success
    assert seen == [4242], "the hook must receive the pid the spawn just returned"


def test_a_failing_on_spawn_hook_does_not_fail_the_delivery():
    def boom(_pid: int) -> None:
        raise OSError("spawn file unwritable")

    assert _deliver_with_spawn_hook(boom).success, (
        "spawn-file evidence must never un-spawn a running child")
