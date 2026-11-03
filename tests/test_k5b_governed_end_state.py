"""ADR-045 audit-spoor-kern K-5b / OI-2519: a governed refusal is never `completed`.

Dispatch-ID: dlv-8e0ff5794555

K-5 round 2 moved the end line after GOVERN so that the frozen line carries the
governed outcome. One path still escaped: ``envelope_govern._govern`` rejects a
report that fails the body contract and books the receipt with
``contract_invalid``, but it only ever set that as a LOCAL ``_effective_status``
— ``adapter_result.status`` stayed ``success`` (OI-1179 forbids rewriting it).
``_LaneExit.govern`` then read ``_envelope_outcome(result.status, ...)`` and
handed ``success`` to the finalizer, so the end line said ``completed`` where
governance had rejected the report.

K-5b publishes the governed verdict as its own field
(``_AdapterResult.governed_status``) and the lane exit maps it onto the end line.
The caller-visible ``EnvelopeResult`` is unchanged.

Every case here runs the REAL lane and the REAL ``_govern``: only the worker
child (the claude spawn or the provider adapter) is replaced, and the report is
a real file on disk whose body really fails ``report_body_contract.validate_body``
(one required section is missing). Nothing here patches ``_govern`` or
``validate_body``. Every store — the ledger, the coordination DB, the report and
the cost ledger — is under ``tmp_path``.

Red on the K-5 head (3b08f844): the invalid-report case published a
``dispatch_finished`` with ``end_state: completed``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
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
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_plan import ExecutionPlan  # noqa: E402
from dispatch_spec import Isolation, Provider  # noqa: E402
from envelope_types import _AdapterResult  # noqa: E402
from provider_spawns.claude_spawn import ClaudeSpawnResult  # noqa: E402

PROJECT = "vnx-dev"
MODEL = "sonnet"
SESSION = "s-k5b"
FIXED_NOW = "2026-11-03T00:00:00Z"
LANES = ["headless", "provider"]


# ---------------------------------------------------------------------------
# Hermetic append + isolated stores: the ledger layer stays real, its process
# hooks do not fire, and every path resolves under this test's tmp tree.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)
    monkeypatch.delenv("VNX_BENCH_WORKTREE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Lifecycle on, every data path under tmp_path, the hand-over clock pinned."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnx-data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    # OI-2519 tests pin ONLY the hand-over clock; every other clock stays real.
    monkeypatch.setattr(dispatch_start, "_now", lambda: FIXED_NOW)


# ---------------------------------------------------------------------------
# Stores and helpers
# ---------------------------------------------------------------------------

def _stores(tmp_path: Path) -> "tuple[Path, Path]":
    """A real data dir, a real state dir and a real runtime_coordination.db."""
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    return data_dir, state_dir


def _begin(tmp_path: Path, data_dir: Path, state_dir: Path, dispatch_id: str) -> dispatch_start.Handover:
    """The real hand-over: a dispatch_started line on the real ledger, v3 on."""
    bundle = data_dir / "dispatches" / "pending" / f"staging-{dispatch_id}"
    bundle.mkdir(parents=True, exist_ok=True)
    spec_file = bundle / "dispatch-spec.json"
    spec_file.write_text(json.dumps({"dispatch_id": dispatch_id}), encoding="utf-8")
    return dispatch_start.begin(
        project_id=PROJECT, dispatch_id=dispatch_id, role="backend-developer",
        base_sha="a" * 40, spec_file=spec_file, target_slot="T1", state_dir=state_dir,
    )


def _ledger_lines(state_dir: Path) -> "list[dict]":
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.is_file():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _finished(state_dir: Path, dispatch_id: str) -> "list[dict]":
    return [line for line in _ledger_lines(state_dir)
            if line.get("event_type") == "dispatch_finished"
            and line.get("dispatch_id") == dispatch_id]


_VALID_BODY = (
    "## Summary\n\n"
    "The worker delivered the change and every required section is present here.\n\n"
    "## Changes\n\n- scripts/lib/example.py: one change\n\n"
    "## Verification\n\n- pytest: 1 passed\n\n"
    "## Open Items\n\nNone\n"
)

# One required section (## Verification) is missing, so the REAL validate_body
# refuses this body.
_INVALID_BODY = (
    "## Summary\n\n"
    "The worker claims success but this report body misses a required section.\n\n"
    "## Changes\n\n- scripts/lib/example.py: one change\n\n"
    "## Open Items\n\nNone\n"
)


def _write_report(data_dir: Path, dispatch_id: str, *, valid: bool) -> Path:
    path = data_dir / "unified_reports" / f"{dispatch_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_VALID_BODY if valid else _INVALID_BODY, encoding="utf-8")
    return path


def _git_repo(tmp_path: Path) -> "tuple[Path, str]":
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "-c", "user.email=k5b@example.invalid", "-c", "user.name=k5b",
                    "commit", "--allow-empty", "-q", "-m", "k5b base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return repo, head


def _make_plan(tmp_path: Path, dispatch_id: str, *, lane: str) -> ExecutionPlan:
    work = tmp_path / "work" / dispatch_id
    work.mkdir(parents=True)
    instruction = work / "instruction.md"
    instruction.write_text("# Build\nDo the work.\n", encoding="utf-8")
    digest = hashlib.sha256(instruction.read_bytes()).hexdigest()
    headless = lane == "headless"
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id=PROJECT,
        # GEMINI on both lanes: no cost-ledger booking and no paid-lane budget,
        # so GOVERN's real machinery cannot reach outside tmp_path.
        provider=Provider.GEMINI,
        model=MODEL,
        lane="claude_headless" if headless else "provider",
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
        instruction_sha256=digest,
        role="backend-developer",
    )


def _fake_spawn(returncode: int, *, timed_out: bool = False):
    def _spawn(**kwargs):
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(os.getpid())
        return ClaudeSpawnResult(
            returncode=returncode, completion={}, events_written=1, session_id=SESSION,
            timed_out=timed_out, completion_text="k5b done", model_resolved="claude-sonnet-5-5",
        )
    return _spawn


class _ProviderStub:
    def __init__(self, result: _AdapterResult):
        self._result = result

    def run(self, plan, instruction, cwd=None, role=None):
        return self._result


@contextlib.contextmanager
def _lane_plumbing(tmp_path: Path, *, worktree: Path):
    """Patch everything a lane does NOT own — never ``_govern`` or ``validate_body``.

    The worktree is a real git repo so the exit's ``head_sha`` evidence is read;
    the consumer root is this test's tmp tree so no path escapes it.
    """
    consumer = tmp_path / "consumer"
    consumer.mkdir(exist_ok=True)
    with patch("dispatch_envelope._prepare", side_effect=lambda spec: spec.instruction), \
         patch("dispatch_envelope._record_integrity", return_value=None), \
         patch("dispatch_envelope._resolve_phantom_diff", return_value=None), \
         patch("dispatch_envelope._enforce_push_pr", side_effect=lambda **kw: kw["result"]), \
         patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=consumer), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=worktree):
        yield


def _run_lane(
    lane: str, tmp_path: Path, state_dir: Path, data_dir: Path,
    dispatch_id: str, handover: dispatch_start.Handover, *, worker_status: str,
    receipt_raises: bool = False,
):
    """Run the REAL lane with the REAL ``_govern``; replace only the worker child."""
    plan = _make_plan(tmp_path, dispatch_id, lane=lane)
    repo, _head = _git_repo(tmp_path)
    govern = {}
    if receipt_raises:
        govern["patch"] = patch(
            "governance_emit.emit_dispatch_receipt",
            side_effect=RuntimeError("receipt writer down (test)"),
        )
    else:
        govern["patch"] = contextlib.nullcontext()

    with _lane_plumbing(tmp_path, worktree=repo), govern["patch"]:
        if lane == "headless":
            from dispatch_envelope import run_envelope_headless_plan  # noqa: PLC0415
            spawn = _fake_spawn(0 if worker_status == "success" else 1)
            with patch("provider_spawns.claude_spawn.spawn_claude", side_effect=spawn):
                return run_envelope_headless_plan(
                    plan, issue_permit(plan), state_dir=state_dir, data_dir=data_dir,
                    role="backend-developer", handover=handover, base_sha=None,
                )
        from dispatch_envelope import run_envelope_plan  # noqa: PLC0415
        adapter_result = _AdapterResult(
            returncode=0 if worker_status == "success" else 1,
            completion_text="k5b done" if worker_status == "success" else "",
            status=worker_status,
            error=None if worker_status == "success" else "worker exit 1",
            session_id=SESSION,
        )
        with patch("dispatch_envelope.ProviderAdapter",
                   side_effect=lambda: _ProviderStub(adapter_result)):
            return run_envelope_plan(
                plan, issue_permit(plan), state_dir=state_dir, data_dir=data_dir,
                role="backend-developer", handover=handover, base_sha=None,
            )


# ---------------------------------------------------------------------------
# 1. A governed refusal is never `completed` — and the receipt still says so.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_invalid_report_books_failed_contract_invalid(lane, tmp_path):
    dispatch_id = f"20261103-k5b-invalid-{lane}"
    data_dir, state_dir = _stores(tmp_path)
    handover = _begin(tmp_path, data_dir, state_dir, dispatch_id)
    _write_report(data_dir, dispatch_id, valid=False)

    result = _run_lane(lane, tmp_path, state_dir, data_dir, dispatch_id, handover,
                       worker_status="success")

    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "failed", lines[0]
    assert lines[0]["reason"] == "contract_invalid", lines[0]
    # The governed receipt already carried contract_invalid before K-5b; unchanged.
    statuses = [line.get("status") for line in _ledger_lines(state_dir)
                if line.get("dispatch_id") == dispatch_id]
    assert "contract_invalid" in statuses, f"{lane}: receipt statuses {statuses}"


# ---------------------------------------------------------------------------
# 2. A valid report under a worker success still ends `completed`.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_valid_report_stays_completed(lane, tmp_path):
    dispatch_id = f"20261103-k5b-valid-{lane}"
    data_dir, state_dir = _stores(tmp_path)
    handover = _begin(tmp_path, data_dir, state_dir, dispatch_id)
    _write_report(data_dir, dispatch_id, valid=True)

    _run_lane(lane, tmp_path, state_dir, data_dir, dispatch_id, handover,
              worker_status="success")

    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "completed", lines[0]
    assert "reason" not in lines[0], lines[0]


# ---------------------------------------------------------------------------
# 3. A non-success worker outcome wins over the contract verdict.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_worker_failure_wins_over_contract_invalid(lane, tmp_path):
    dispatch_id = f"20261103-k5b-worker-failed-{lane}"
    data_dir, state_dir = _stores(tmp_path)
    handover = _begin(tmp_path, data_dir, state_dir, dispatch_id)
    _write_report(data_dir, dispatch_id, valid=False)

    _run_lane(lane, tmp_path, state_dir, data_dir, dispatch_id, handover,
              worker_status="failure")

    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "failed", lines[0]
    assert lines[0]["reason"] == "lane_failed", lines[0]


# ---------------------------------------------------------------------------
# 4. No ledger receipt: the non-success outcome (receipt_missing) still wins.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_invalid_report_without_receipt_books_failed(lane, tmp_path):
    dispatch_id = f"20261103-k5b-no-receipt-{lane}"
    data_dir, state_dir = _stores(tmp_path)
    handover = _begin(tmp_path, data_dir, state_dir, dispatch_id)
    _write_report(data_dir, dispatch_id, valid=False)

    result = _run_lane(lane, tmp_path, state_dir, data_dir, dispatch_id, handover,
                       worker_status="success", receipt_raises=True)

    lines = _finished(state_dir, dispatch_id)
    assert len(lines) == 1, f"{lane}: expected one dispatch_finished, got {lines}"
    assert lines[0]["end_state"] == "failed", lines[0]
    # _envelope_outcome degrades a success with no receipt to receipt_missing; the
    # end line must carry that, not contract_invalid (a non-success outcome wins).
    assert lines[0]["reason"] in {"receipt_missing", "contract_invalid"}, lines[0]
    assert lines[0]["reason"] == "receipt_missing", lines[0]
    assert result.status == "receipt_missing", result


# ---------------------------------------------------------------------------
# 5. The caller-visible result does not change (K-5 head returned success/0).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lane", LANES)
def test_caller_visible_result_is_unchanged(lane, tmp_path):
    dispatch_id = f"20261103-k5b-caller-{lane}"
    data_dir, state_dir = _stores(tmp_path)
    handover = _begin(tmp_path, data_dir, state_dir, dispatch_id)
    _write_report(data_dir, dispatch_id, valid=False)

    result = _run_lane(lane, tmp_path, state_dir, data_dir, dispatch_id, handover,
                       worker_status="success")

    # K-5 head (3b08f844) returned the work status: success / 0, even though the
    # end line is now contract_invalid. Only the end line changed.
    assert result.status == "success", result
    assert result.returncode == 0, result
