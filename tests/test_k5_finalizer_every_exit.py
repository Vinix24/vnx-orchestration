"""ADR-045 audit-spoor-kern K-5: one end line at every lane's exit.

Dispatch-ID: dlv-99391e46c683

ADR-045 §1/§2/§5: every dispatch that opened a ``dispatch_started`` for an
attempt ends it with exactly one ``dispatch_finished`` for the same key
``(project_id, dispatch_id, attempt)`` — from the success path, from every
failure, and from the slot/lock timeout that never reaches a spawn. The line is
published by ``dispatch_lifecycle.finalize`` BEFORE the lane releases its
occupancy flock, so a lane stays provably alive until its end line is on disk.

``scripts/lib/dispatch_exit.py`` is the one call-site shape (K-5): it maps a
lane's own outcome onto the closed end-state set, reads the worktree head as the
``head_sha`` evidence, refuses to write anything for a legacy hand-over, and
swallows (logs, never re-raises) a finalizer failure so a successful dispatch is
never booked failed.

Every case here runs the REAL lane / REAL door with the child (or the provider
adapter) replaced and the governed receipt writer (``_govern``) stubbed — the
forge stand-in, exactly as K-4c's lane tests do. Every store is under
``tmp_path``; nothing touches the real ledger.

Red on the K-4c head: no exit calls the finalizer, so every lifecycle-on case
below finds an empty ledger.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import coordination_db  # noqa: E402
import dispatch_cli  # noqa: E402
import dispatch_lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import dispatch_worktree_isolation as isolation  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_plan import ExecutionPlan, ModelPin, RuntimeSnapshot  # noqa: E402
from dispatch_spec import Isolation, Provider  # noqa: E402
from envelope_types import _AdapterResult  # noqa: E402
from paid_lane_budget import PaidLaneBudgetExceededError  # noqa: E402
from provider_spawns.claude_spawn import ClaudeSpawnResult  # noqa: E402

PROJECT = "vnx-dev"
MODEL = "sonnet"
SESSION = "s-k5"


# ---------------------------------------------------------------------------
# Hermetic append: keep the real ledger layer, never fire its process hooks
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)
    # The worktree dir must resolve under this test's tmp consumer, never a bench root.
    monkeypatch.delenv("VNX_BENCH_WORKTREE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _state_dirs(tmp_path: Path) -> "tuple[Path, Path]":
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    return data_dir, state_dir


def _ledger_lines(state_dir: Path) -> "list[dict]":
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.is_file():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _finished(state_dir: Path, dispatch_id: str, attempt: int = 1) -> "list[dict]":
    return [line for line in _ledger_lines(state_dir)
            if line.get("event_type") == "dispatch_finished"
            and line.get("dispatch_id") == dispatch_id
            and line.get("attempt") == attempt]


@contextlib.contextmanager
def _release_order_spy(state_dir: Path, dispatch_id: str):
    """Record how many end lines exist at the instant the flock is released.

    ``remove_dispatch_worktree``'s first action is ``_release_occupancy``; the
    end line must already be on disk then (ADR-045 §5), so every entry recorded
    here has to be 1, never 0.
    """
    seen: "list[int]" = []
    real = isolation._release_occupancy

    def _spy(wt_path):
        seen.append(len(_finished(state_dir, dispatch_id)))
        return real(wt_path)

    with patch.object(isolation, "_release_occupancy", _spy):
        yield seen


def _git_repo(tmp_path: Path) -> "tuple[Path, str]":
    """A real one-commit repo, so the exit's ``head_sha`` is read, not faked."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "-c", "user.email=k5@example.invalid", "-c", "user.name=k5",
                    "commit", "--allow-empty", "-q", "-m", "k5 base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return repo, head


def _snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=True,
        target_health={"ephemeral": "healthy", "T0": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T0": True, "T1": True},
        model_pins={
            "T0": ModelPin(model="opus", semantics="floor"),
            "T1": ModelPin(model=MODEL, semantics="floor"),
        },
    )


def _make_plan(tmp_path: Path, dispatch_id: str, *, lane: str = "claude_headless",
               provider: Provider = Provider.CLAUDE, sha256: "str | None" = None) -> ExecutionPlan:
    work = tmp_path / "work" / dispatch_id
    work.mkdir(parents=True)
    instruction = work / "instruction.md"
    instruction.write_text("# Build\nDo the work.\n", encoding="utf-8")
    digest = hashlib.sha256(instruction.read_bytes()).hexdigest()
    headless = lane == "claude_headless"
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id=PROJECT,
        provider=provider,
        model=MODEL,
        lane=lane,
        adapter="claude_subprocess" if headless else "provider",
        target_id="ephemeral" if headless else "T1",
        billing="subscription" if headless else "provider_metered",
        serialization_class="claude-tmux" if headless else None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=60,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=instruction,
        route_reason="test",
        instruction_sha256=digest if sha256 is None else sha256,
        role="backend-developer",
    )


def _v3_handover(dispatch_id: str, *, attempt: int = 1) -> dispatch_start.Handover:
    return dispatch_start.Handover(
        project_id=PROJECT,
        dispatch_id=dispatch_id,
        attempt=attempt,
        lifecycle_mode=dispatch_start.MODE_V3,
        door_pid=os.getpid(),
        door_started_at=dispatch_start.process_started_at(os.getpid()) or "",
        started_at="2026-11-03T00:00:00Z",
    )


def _legacy_handover(dispatch_id: str) -> dispatch_start.Handover:
    return dispatch_start.Handover(
        project_id=PROJECT,
        dispatch_id=dispatch_id,
        attempt=1,
        lifecycle_mode=dispatch_start.MODE_LEGACY,
        door_pid=os.getpid(),
        door_started_at="",
        started_at="2026-11-03T00:00:00Z",
    )


def _fake_spawn(returncode: int, *, timed_out: bool = False):
    """The child seam: spawn_claude's own result, with the pid hook fired."""
    def _spawn(**kwargs):
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(os.getpid())
        return ClaudeSpawnResult(
            returncode=returncode, completion={}, events_written=1, session_id=SESSION,
            timed_out=timed_out, completion_text="k5 done", model_resolved="claude-sonnet-5-5",
        )
    return _spawn


class _ProviderStub:
    """The provider child seam: the same ``_AdapterResult`` ProviderAdapter returns."""

    def __init__(self, result: _AdapterResult):
        self._result = result

    def run(self, plan, instruction, cwd=None, role=None):
        return self._result


@contextlib.contextmanager
def _lane_plumbing(tmp_path: Path, state_dir: Path, *, worktree, worktree_error=None,
                   govern=None):
    """Patch what a lane does NOT own: prompt assembly, integrity, GOVERN, worktree.

    ``_govern`` is the governed receipt writer (the forge stand-in K-4c uses): it
    is stubbed so the test's ledger carries ONLY the lane's own end line, unless
    *govern* is given — then that callable stands in for it (it may raise, or
    return no receipt path).
    """
    consumer = tmp_path / "consumer"
    consumer.mkdir(exist_ok=True)
    receipt = state_dir / "t0_receipts.ndjson"
    receipt.touch()
    if worktree_error is not None:
        create = patch("dispatch_worktree_isolation.create_dispatch_worktree",
                       side_effect=worktree_error)
    else:
        create = patch("dispatch_worktree_isolation.create_dispatch_worktree",
                       return_value=worktree)
    if govern is None:
        govern_patch = patch("dispatch_envelope._govern", return_value=(None, receipt))
    else:
        govern_patch = patch("dispatch_envelope._govern", side_effect=govern)
    with patch("dispatch_envelope._prepare", side_effect=lambda spec: spec.instruction), \
         patch("dispatch_envelope._record_integrity", return_value=None), \
         govern_patch, \
         patch("dispatch_envelope._resolve_phantom_diff", return_value=None), \
         patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=consumer), \
         create:
        yield


# ---------------------------------------------------------------------------
# The exit inventory (K-5 report, `## Changes`): 16 reachable exits over three
# lanes. The lanes' hashless branch (dispatch_envelope.py:923/612) is NOT one:
# ``require_permit`` (913/601) refuses a plan without a valid 64-hex digest long
# before it, so no caller can reach it — pinned by the test below.
# ---------------------------------------------------------------------------

CASES = [
    # headless: one terminal exit (4 outcomes) + 3 pre-spawn returns
    dict(id="headless-success", lane="headless", trigger="ok", end_state="completed",
         reason=None, worktree=True, spawned=True, order=[1]),
    dict(id="headless-child-failed", lane="headless", trigger="child_failed", end_state="failed",
         reason="lane_failed", worktree=True, spawned=True, order=[1]),
    dict(id="headless-timeout", lane="headless", trigger="timeout", end_state="timed_out",
         reason="timeout", worktree=True, spawned=True, order=[1]),
    dict(id="headless-toctou", lane="headless", trigger="toctou", end_state="failed",
         reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="headless-worktree-failed", lane="headless", trigger="worktree_failed",
         end_state="failed", reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="headless-close-out-raised", lane="headless", trigger="close_out_raised",
         end_state="failed", reason="lane_failed", worktree=True, spawned=True, order=[1]),
    # provider: one terminal exit (4 outcomes) + 4 pre-spawn returns
    dict(id="provider-success", lane="provider", trigger="ok", end_state="completed",
         reason=None, worktree=True, spawned=True, order=[1]),
    dict(id="provider-child-failed", lane="provider", trigger="child_failed", end_state="failed",
         reason="lane_failed", worktree=True, spawned=True, order=[1]),
    dict(id="provider-timeout", lane="provider", trigger="timeout", end_state="timed_out",
         reason="timeout", worktree=True, spawned=True, order=[1]),
    dict(id="provider-toctou", lane="provider", trigger="toctou", end_state="failed",
         reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="provider-budget-exceeded", lane="provider", trigger="budget", end_state="failed",
         reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="provider-worktree-failed", lane="provider", trigger="worktree_failed",
         end_state="failed", reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="provider-close-out-raised", lane="provider", trigger="close_out_raised",
         end_state="failed", reason="lane_failed", worktree=True, spawned=True, order=[1]),
    # door: the exits that never reach a lane
    dict(id="door-slot-timeout", lane="door", trigger="slot_timeout", end_state="failed",
         reason="slot_timeout", worktree=False, spawned=False, order=[]),
    dict(id="door-invariant-violation", lane="door", trigger="invariant", end_state="failed",
         reason="pre_spawn_error", worktree=False, spawned=False, order=[]),
    dict(id="door-runtime-error", lane="door", trigger="door_error", end_state="failed",
         reason="door_error", worktree=False, spawned=False, order=[]),
]

CASE_IDS = [case["id"] for case in CASES]


# ---------------------------------------------------------------------------
# Runners: the real lane (headless / provider) and the real door
# ---------------------------------------------------------------------------

def _run_headless(tmp_path, state_dir, dispatch_id, handover, case, govern=None) -> int:
    plan = _make_plan(tmp_path, dispatch_id,
                      sha256="0" * 64 if case["trigger"] == "toctou" else None)
    repo, _head = _git_repo(tmp_path)
    wt_error = RuntimeError("no worktree for you") if case["trigger"] == "worktree_failed" else None
    spawn = {
        "ok": _fake_spawn(0),
        "close_out_raised": _fake_spawn(0),
        "child_failed": _fake_spawn(1),
        "timeout": _fake_spawn(-9, timed_out=True),
    }.get(case["trigger"])
    enforce = (patch("dispatch_envelope._enforce_push_pr",
                     side_effect=RuntimeError("forge unreachable"))
               if case["trigger"] == "close_out_raised"
               else patch("dispatch_envelope._enforce_push_pr", side_effect=lambda **kw: kw["result"]))
    with _lane_plumbing(tmp_path, state_dir, worktree=repo, worktree_error=wt_error,
                        govern=govern), \
         patch("provider_spawns.claude_spawn.spawn_claude", side_effect=spawn if spawn else _fake_spawn(0)), \
         enforce:
        return dispatch_cli._execute_claude_headless(
            plan, issue_permit(plan), state_dir=state_dir,
            data_dir=tmp_path / "vnx-data", role="backend-developer",
            handover=handover, base_sha=None,
        )


def _run_provider(tmp_path, state_dir, dispatch_id, handover, case, govern=None) -> int:
    sha = "0" * 64 if case["trigger"] == "toctou" else None
    plan = _make_plan(tmp_path, dispatch_id, lane="provider", provider=Provider.CODEX, sha256=sha)
    repo, _head = _git_repo(tmp_path)
    wt_error = RuntimeError("no worktree for you") if case["trigger"] == "worktree_failed" else None
    adapter = {
        "child_failed": _AdapterResult(returncode=1, completion_text="", status="failure",
                                       error="provider exit 1"),
        "timeout": _AdapterResult(returncode=-9, completion_text="", status="timeout",
                                  timed_out=True, error="deadline"),
    }.get(case["trigger"], _AdapterResult(returncode=0, completion_text="provider done",
                                          status="success"))
    enforce = (patch("dispatch_envelope._enforce_push_pr",
                     side_effect=RuntimeError("forge unreachable"))
               if case["trigger"] == "close_out_raised"
               else patch("dispatch_envelope._enforce_push_pr", side_effect=lambda **kw: kw["result"]))
    budget = (patch("dispatch_envelope.enforce_daily_budget",
                    side_effect=PaidLaneBudgetExceededError("codex", 9.0, 1.0))
              if case["trigger"] == "budget"
              else patch("dispatch_envelope.enforce_daily_budget", return_value=None))
    with _lane_plumbing(tmp_path, state_dir, worktree=repo, worktree_error=wt_error,
                        govern=govern), \
         patch("dispatch_envelope.ProviderAdapter", side_effect=lambda: _ProviderStub(adapter)), \
         budget, enforce:
        from dispatch_envelope import run_envelope_plan
        return run_envelope_plan(
            plan, issue_permit(plan), state_dir=state_dir,
            data_dir=tmp_path / "vnx-data", role="backend-developer",
            handover=handover, base_sha=None,
        ).returncode


def _make_coordination_db(state_dir: Path) -> Path:
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    return db_path


def _make_bundle(tmp_path: Path, *, dispatch_id: str) -> Path:
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / f"staging-{dispatch_id}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1, "project_id": PROJECT, "dispatch_id": dispatch_id,
        "staging_id": f"staging-{dispatch_id}", "instruction_file": str(instruction),
        "role": "backend-developer", "target_slot": "T1", "gate": "codex_gate",
        "dispatch_paths": [], "provider": "codex", "deadline_seconds": 3600,
        "isolation": "worktree", "base_ref": "origin/main",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def _run_door(tmp_path, state_dir, dispatch_id, case, monkeypatch, *, lifecycle_on=True) -> int:
    spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnx-data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if lifecycle_on:
        monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    else:
        monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)

    ok = _AdapterResult(returncode=0, completion_text="door done", status="success")

    @contextlib.contextmanager
    def _slot(serialization_class, *, dispatch_id):
        trigger = case["trigger"]
        if trigger == "slot_timeout":
            raise dispatch_cli.LockWaitTimeout("no claude-lane slot within the deadline")
        if trigger == "invariant":
            raise dispatch_cli._InvariantViolation("closed set violated — unknown lane")
        yield

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_snapshot()), \
         patch("dispatch_cli.serialize_lane", _slot), \
         patch("dispatch_cli.run_envelope_plan",
               side_effect=RuntimeError("door blew up") if case["trigger"] == "door_error"
               else (lambda *a, **kw: ok)):
        return dispatch_cli.run_dispatch(spec_file)


# ---------------------------------------------------------------------------
# 1. lifecycle ON: exactly one end line per exit, before the flock is released
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_v3_exit_writes_exactly_one_end_line_before_the_release(case, tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = f"20261103-k5-{case['id']}"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)

    with _release_order_spy(state_dir, dispatch_id) as released:
        if case["lane"] == "headless":
            rc = _run_headless(tmp_path, state_dir, dispatch_id, handover, case)
        elif case["lane"] == "provider":
            rc = _run_provider(tmp_path, state_dir, dispatch_id, handover, case)
        else:
            rc = _run_door(tmp_path, state_dir, dispatch_id, case, monkeypatch)

    assert (rc == 0) is (case["end_state"] == "completed"), f"outcome {rc} for {case['id']}"
    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{case['id']}: expected one dispatch_finished, got {lines}"
    line = lines[0]
    assert line["project_id"] == PROJECT
    assert line["attempt"] == 1
    assert line["end_state"] == case["end_state"], line
    assert line.get("reason") == case["reason"], line
    if case["worktree"]:
        assert line.get("head_sha"), f"{case['id']}: no head_sha evidence: {line}"
        assert "no_push" not in line
    else:
        assert "head_sha" not in line, line
        assert line.get("no_push") == "no_push:no_worktree", line
    if case["spawned"]:
        assert line.get("spawned_at"), f"{case['id']}: a spawned run must date its spawn: {line}"
    else:
        assert "spawned_at" not in line, f"{case['id']}: {line}"
    if case["lane"] == "headless" and case["spawned"]:
        assert line.get("session_id") == SESSION, f"{case['id']}: {line}"
    else:
        assert "session_id" not in line, f"{case['id']}: {line}"
    # The occupancy flock is released only AFTER the line is on disk.
    assert released == case["order"], f"{case['id']}: released with {released} end lines on disk"


# ---------------------------------------------------------------------------
# 2. lifecycle OFF (legacy hand-over): nothing written, the K-4c outcome stands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_legacy_exit_writes_no_end_line_and_keeps_the_k4c_outcome(case, tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    dispatch_id = f"20261103-k5-legacy-{case['id']}"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _legacy_handover(dispatch_id)

    with _release_order_spy(state_dir, dispatch_id) as released, \
         patch.object(dispatch_start, "begin", wraps=dispatch_start.begin) as begin:
        if case["lane"] == "headless":
            rc = _run_headless(tmp_path, state_dir, dispatch_id, handover, case)
        elif case["lane"] == "provider":
            rc = _run_provider(tmp_path, state_dir, dispatch_id, handover, case)
        else:
            rc = _run_door(tmp_path, state_dir, dispatch_id, case, monkeypatch,
                           lifecycle_on=False)
            assert begin.call_count == 1, "the door must have handed over before the exit"

    assert (rc == 0) is (case["end_state"] == "completed")
    assert _finished(state_dir, dispatch_id) == [], "a legacy hand-over may write no end line"
    # No finalize ran, so the release sees an empty ledger — the K-4c ordering.
    assert released == ([0] if case["worktree"] else [])


# ---------------------------------------------------------------------------
# 3. a successful dispatch is never booked failed by its own finalizer
# ---------------------------------------------------------------------------

def test_a_raising_finalizer_keeps_the_success_and_releases_the_flock(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k5-finalizer-raises"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)
    case = next(item for item in CASES if item["id"] == "headless-success")
    import dispatch_exit  # noqa: PLC0415 — the module under test, absent on the K-4c head

    with _release_order_spy(state_dir, dispatch_id) as released, \
         patch.object(dispatch_exit, "finalize",
                      side_effect=RuntimeError("publication lock held by a sweep")), \
         caplog.at_level(logging.WARNING, logger="dispatch_exit"):
        rc = _run_headless(tmp_path, state_dir, dispatch_id, handover, case)

    assert rc == 0, "a finalizer failure must never turn a successful run into a failed one"
    assert _finished(state_dir, dispatch_id) == []
    assert released == [0], "the flock must still be released after the finalize raised"
    messages = [record.getMessage() for record in caplog.records]
    assert any(dispatch_id in message and "publication lock held by a sweep" in message
               for message in messages), messages


# ---------------------------------------------------------------------------
# 4. one finalizer, one key: a racing second call adds no second line
# ---------------------------------------------------------------------------

def test_a_second_finalize_on_the_same_key_reports_the_loser(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k5-double-finalize"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)
    import dispatch_exit  # noqa: PLC0415 — the module under test, absent on the K-4c head

    first = dispatch_exit.finalize_exit(handover, state_dir=state_dir, status="success")
    second = dispatch_exit.finalize_exit(handover, state_dir=state_dir, status="failure")

    assert first is not None and first.status == dispatch_lifecycle.STATUS_FINALIZED
    assert second is not None and second.status in {
        dispatch_lifecycle.STATUS_ALREADY_PUBLISHED, dispatch_lifecycle.STATUS_INTENT_LOST}
    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"a second finalizer must not add a second line: {lines}"
    assert lines[0]["end_state"] == "completed"


# ---------------------------------------------------------------------------
# 5. the hashless branch is not an exit: no permit, no lane
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", ["claude_headless", "provider"])
def test_a_hashless_plan_never_reaches_the_lane_exit(lane, tmp_path):
    """``dispatch_envelope``'s hashless return is unreachable, by construction.

    The lane's own ``if not is_valid_instruction_hash(...)`` guard sits behind
    ``require_permit``, and ``issue_permit``/``require_permit`` both refuse a
    plan without a valid 64-hex digest. So no v3 hand-over can take that exit:
    it is defence in depth, not a sixth/seventh termination of a dispatch.
    """
    from dispatch_internal import issue_permit, require_permit  # noqa: PLC0415

    dispatch_id = f"20261103-k5-hashless-{lane}"
    plan = _make_plan(tmp_path, dispatch_id, lane=lane,
                      provider=Provider.CLAUDE if lane == "claude_headless" else Provider.CODEX,
                      sha256="ab" * 3)
    with pytest.raises(PermissionError, match="instruction_sha256"):
        issue_permit(plan)
    # A permit minted for a VALID plan cannot be replayed against the hashless one.
    good = _make_plan(tmp_path, f"{dispatch_id}-good", lane=lane,
                      provider=Provider.CLAUDE if lane == "claude_headless" else Provider.CODEX)
    with pytest.raises(PermissionError, match="instruction_sha256"):
        require_permit(plan, issue_permit(good))


# ---------------------------------------------------------------------------
# 6. round 2 — the end line follows the GOVERNED outcome, not the worker's
#
# K-5's head published the line from the lane's ``finally`` BEFORE ``_govern``
# ran, so a worker success whose governance raised (or whose receipt never
# landed) was booked ``completed`` and could not be corrected. These cases fix
# that outcome: the line is now the governed one, and the occupancy flock is
# still held until it is on disk.
# ---------------------------------------------------------------------------

LANES = ["headless", "provider"]


def _success_case(lane: str) -> dict:
    return {"id": f"{lane}-governed", "lane": lane, "trigger": "ok",
            "worktree": True, "spawned": True}


def _run_lane(lane: str, tmp_path, state_dir, dispatch_id, handover, *, govern=None) -> int:
    case = _success_case(lane)
    if lane == "headless":
        return _run_headless(tmp_path, state_dir, dispatch_id, handover, case, govern=govern)
    return _run_provider(tmp_path, state_dir, dispatch_id, handover, case, govern=govern)


@contextlib.contextmanager
def _teardown_spy():
    """Count the real teardown: ``remove_dispatch_worktree`` must run exactly once."""
    seen: "list[str]" = []
    real = isolation.remove_dispatch_worktree

    def _spy(dispatch_id, **kwargs):
        seen.append(dispatch_id)
        return real(dispatch_id, **kwargs)

    with patch.object(isolation, "remove_dispatch_worktree", _spy):
        yield seen


@contextlib.contextmanager
def _release_snapshot(state_dir: Path, dispatch_id: str):
    """What the ledger holds at the instant the occupancy flock is released.

    The governed receipt and the one end line both have to be there already
    (ADR-045 §5): the order is govern → line → release, never release first.
    """
    seen: "list[dict]" = []
    real = isolation._release_occupancy

    def _spy(wt_path):
        lines = _ledger_lines(state_dir)
        seen.append({
            "end_lines": len([line for line in lines
                              if line.get("event_type") == "dispatch_finished"
                              and line.get("dispatch_id") == dispatch_id]),
            "receipt": any(line.get("event_type") == "dispatch_completed"
                           and line.get("dispatch_id") == dispatch_id for line in lines),
        })
        return real(wt_path)

    with patch.object(isolation, "_release_occupancy", _spy):
        yield seen


@pytest.mark.parametrize("lane", LANES)
def test_a_govern_that_raises_books_failed_and_propagates(lane, tmp_path, monkeypatch):
    """A worker success whose GOVERN raised is a failed attempt, not `completed`."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = f"20261103-k5-r2-govern-raises-{lane}"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)

    def _govern_raises(*_args, **_kwargs):
        raise RuntimeError("govern blew up")

    with _teardown_spy() as torn_down, \
         pytest.raises(RuntimeError, match="govern blew up"):
        _run_lane(lane, tmp_path, state_dir, dispatch_id, handover, govern=_govern_raises)

    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "failed", lines[0]
    assert lines[0]["reason"] == "govern_error", lines[0]
    assert torn_down == [dispatch_id], f"{lane}: teardown {torn_down}"


@pytest.mark.parametrize("lane", LANES)
def test_a_missing_receipt_books_failed(lane, tmp_path, monkeypatch):
    """OI-1287: a worker success with no ledger receipt is not a completed attempt."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = f"20261103-k5-r2-receipt-missing-{lane}"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)

    def _govern_without_receipt(*_args, **_kwargs):
        return None, None

    with _teardown_spy() as torn_down, \
         _release_order_spy(state_dir, dispatch_id) as released:
        rc = _run_lane(lane, tmp_path, state_dir, dispatch_id, handover,
                       govern=_govern_without_receipt)

    assert rc != 0, f"{lane}: a missing receipt must not return a clean exit"
    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "failed", lines[0]
    assert lines[0]["reason"] == "receipt_missing", lines[0]
    assert torn_down == [dispatch_id], f"{lane}: teardown {torn_down}"
    assert released == [1], f"{lane}: released with {released} end lines on disk"


@pytest.mark.parametrize("lane", LANES)
def test_a_governed_success_publishes_before_the_release(lane, tmp_path, monkeypatch):
    """The order is govern → end line → release: both are on disk at the release."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = f"20261103-k5-r2-governed-success-{lane}"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)
    receipt = state_dir / "t0_receipts.ndjson"

    def _govern_writes_receipt(spec, result, _start, _end, **_kwargs):
        with receipt.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event_type": "dispatch_completed",
                "dispatch_id": spec.dispatch_id,
                "project_id": PROJECT,
            }) + "\n")
        return None, receipt

    with _teardown_spy() as torn_down, \
         _release_snapshot(state_dir, dispatch_id) as snapshots:
        rc = _run_lane(lane, tmp_path, state_dir, dispatch_id, handover,
                       govern=_govern_writes_receipt)

    assert rc == 0, f"{lane}: a governed success must return a clean exit"
    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "completed", lines[0]
    assert "reason" not in lines[0], lines[0]
    assert torn_down == [dispatch_id], f"{lane}: teardown {torn_down}"
    assert snapshots == [{"end_lines": 1, "receipt": True}], \
        f"{lane}: the flock was released before the line and the receipt: {snapshots}"


def test_a_keyboard_interrupt_in_govern_still_tears_down(tmp_path, monkeypatch):
    """A BaseException out of GOVERN: the worktree still goes, at most one line."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    dispatch_id = "20261103-k5-r2-keyboard-interrupt"
    _data_dir, state_dir = _state_dirs(tmp_path)
    handover = _v3_handover(dispatch_id)

    def _govern_interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    with _teardown_spy() as torn_down, \
         pytest.raises(KeyboardInterrupt):
        _run_lane("headless", tmp_path, state_dir, dispatch_id, handover,
                  govern=_govern_interrupted)

    assert torn_down == [dispatch_id], f"teardown {torn_down}"
    lines = _finished(state_dir, dispatch_id)
    assert len(lines) <= 1, f"at most one end line per attempt: {lines}"
    if lines:
        assert lines[0]["end_state"] == "failed", lines[0]


# ---------------------------------------------------------------------------
# 7. the one table: a GOVERNED status onto the end line (dispatch_exit)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,timed_out,end_state,reason", [
    ("success", False, "completed", None),
    ("failure", False, "failed", "lane_failed"),
    ("timeout", False, "timed_out", "timeout"),
    ("timeout", True, "timed_out", "timeout"),
    ("failure", True, "timed_out", "timeout"),
    ("receipt_missing", False, "failed", "receipt_missing"),
    # fail-closed: missing, empty and unknown statuses are failures, never a pass
    (None, False, "failed", "lane_failed"),
    ("", False, "failed", "lane_failed"),
    ("   ", False, "failed", "lane_failed"),
    ("completed", False, "failed", "lane_failed"),
    ("SUCCESS", False, "failed", "lane_failed"),
])
def test_the_governed_status_table(status, timed_out, end_state, reason):
    import dispatch_exit  # noqa: PLC0415 — the table under test

    assert dispatch_exit.end_state_for(status, timed_out=timed_out) == (end_state, reason)


def test_the_governed_table_keys_the_envelope_receipt_missing_status():
    """One literal: the table's key IS the status ``_envelope_outcome`` stamps."""
    import dispatch_envelope  # noqa: PLC0415
    import dispatch_exit  # noqa: PLC0415

    key = dispatch_envelope.RECEIPT_MISSING_STATUS
    assert dispatch_exit.GOVERNED_END_STATES[key] == ("failed", key)
