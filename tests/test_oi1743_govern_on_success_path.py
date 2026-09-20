"""tests/test_oi1743_govern_on_success_path.py — OI-1743 regression.

A dispatch that did its work must write its OWN completion receipt on the
success path, EVEN WHEN a step AFTER the worker raises. Before the fix, the
worker run + _enforce_push_pr lived in a `try` with only a `finally`
(worktree teardown) and NO `except`. An exception raised after a successful
worker propagated straight past the `_govern` call below it, so no report and
no receipt were emitted. A fast FAILURE reached _govern (the failure path
skips _enforce_push_pr), but a SUCCESS that then stumbled in the close-out
did not. That is the inversion this test pins down.

The test is red on the old code (signature intact): the exception propagates
out of run_envelope_headless_plan / run_envelope_plan and _govern is never
called, so no receipt line lands. After the fix the exception is caught, a
failure result is built, and _govern runs and writes a receipt.

These tests run the REAL lane functions. Only the adapter, the worktree
allocator, and the push/PR guard are mocked — _govern runs for real against a
real state_dir, so the receipt line on disk is the assertion, not a mock call.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    ClaudeSubprocessAdapter,
    ProviderAdapter,
    _AdapterResult,
    run_envelope_headless_plan,
    run_envelope_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import Isolation, Provider


# ---------------------------------------------------------------------------
# Plan fixtures (mirror test_dispatch_envelope_plan.py shapes)
# ---------------------------------------------------------------------------


def _make_headless_plan(tmp_path: Path) -> ExecutionPlan:
    instruction_file = tmp_path / "claude_inst.md"
    instruction_file.write_text("# Claude dispatch\nDo the work.", encoding="utf-8")
    sha = hashlib.sha256(instruction_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id="test-oi1743-headless",
        project_id="vnx-dev",
        provider=Provider.CLAUDE,
        model="sonnet",
        lane="claude_headless",
        adapter="claude_subprocess",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="D1,D2,D3",
        instruction_sha256=sha,
    )


def _make_provider_plan(tmp_path: Path) -> ExecutionPlan:
    instruction_file = tmp_path / "kimi_inst.md"
    instruction_file.write_text("# Kimi dispatch\nDo the work.", encoding="utf-8")
    sha = hashlib.sha256(instruction_file.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    return ExecutionPlan(
        dispatch_id="test-oi1743-provider",
        project_id="vnx-dev",
        provider=Provider.KIMI,
        model="kimi-k3",
        lane="provider",
        adapter="provider",
        target_id="T1",
        billing="provider_metered",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="D1,D2,D3",
        instruction_sha256=sha,
    )


# ---------------------------------------------------------------------------
# Headless lane: a close-out exception after a successful worker must still
# reach _govern and write a receipt.
# ---------------------------------------------------------------------------


def test_headless_closeout_exception_still_writes_receipt(tmp_path):
    """The worker succeeds, then _enforce_push_pr raises. Before the fix the
    exception propagated past _govern and no receipt landed. After the fix
    the exception is caught and a failure receipt is written that names the
    dispatch and the close-out failure."""
    plan = _make_headless_plan(tmp_path)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()
    (data_dir / "unified_reports").mkdir(parents=True)

    _fake_consumer_root = tmp_path / "consumer-root"

    worker_success = _AdapterResult(
        returncode=0, completion_text="done", status="success", model="sonnet",
    )

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=_fake_consumer_root), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch.object(ClaudeSubprocessAdapter, "run", return_value=worker_success), \
         patch("dispatch_envelope._enforce_push_pr", side_effect=RuntimeError("close-out boom")):
        result = run_envelope_headless_plan(
            plan, permit, state_dir=state_dir, data_dir=data_dir,
        )

    # The dispatch outcome is a failure (the close-out raised), NOT a silent
    # success and NOT an uncaught exception.
    assert result.status == "failure", (
        f"a close-out exception after a successful worker must surface as a "
        f"governed failure, got status={result.status!r}"
    )
    assert result.receipt_path is not None, (
        "a successful-then-failed dispatch must still write its own receipt "
        "on the success path — no receipt means the gap OI-1743 closes"
    )
    assert result.receipt_path.exists()

    # The receipt line on disk names this dispatch — the audit trail is not
    # silent.
    import json
    lines = [
        json.loads(l) for l in result.receipt_path.read_text().splitlines()
        if l.strip()
    ]
    own = [r for r in lines if r.get("dispatch_id") == plan.dispatch_id]
    assert own, "the dispatch's own receipt line must land on the ledger"
    assert own[-1].get("status") != "success", (
        "a dispatch whose close-out raised must never leave a 'success' "
        "receipt as its last word"
    )


def test_headless_worker_exception_still_writes_receipt(tmp_path):
    """The worker itself raises (an unguarded spawn error the adapter did not
    catch). Before the fix this propagated past _govern. After the fix a
    failure receipt is written so the dispatch is never silent."""
    plan = _make_headless_plan(tmp_path)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    _fake_consumer_root = tmp_path / "consumer-root"

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=_fake_consumer_root), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch.object(
             ClaudeSubprocessAdapter, "run",
             side_effect=OSError("spawn died"),
         ):
        result = run_envelope_headless_plan(
            plan, permit, state_dir=state_dir, data_dir=data_dir,
        )

    assert result.status == "failure"
    assert result.receipt_path is not None
    assert result.receipt_path.exists()


# ---------------------------------------------------------------------------
# Provider lane: the same close-out-exception-after-success invariant.
# ---------------------------------------------------------------------------


def test_provider_closeout_exception_still_writes_receipt(tmp_path):
    """The provider worker succeeds, then _enforce_push_pr raises. The
    provider lane shared the exact try/finally-without-except shape of the
    headless lane, so the same gap applied. The fix mirrors the headless
    lane and must keep _govern reachable here too."""
    plan = _make_provider_plan(tmp_path)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()
    (data_dir / "unified_reports").mkdir(parents=True)

    _fake_consumer_root = tmp_path / "consumer-root"

    worker_success = _AdapterResult(
        returncode=0, completion_text="done", status="success", model="kimi-k3",
    )

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=_fake_consumer_root), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch.object(ProviderAdapter, "run", return_value=worker_success), \
         patch("dispatch_envelope._enforce_push_pr", side_effect=RuntimeError("close-out boom")):
        result = run_envelope_plan(
            plan, permit, state_dir=state_dir, data_dir=data_dir,
        )

    assert result.status == "failure", (
        f"a close-out exception after a successful worker must surface as a "
        f"governed failure, got status={result.status!r}"
    )
    assert result.receipt_path is not None, (
        "the provider lane must write its own receipt on the success path "
        "even when the close-out raises (OI-1743)"
    )
    assert result.receipt_path.exists()

    import json
    lines = [
        json.loads(l) for l in result.receipt_path.read_text().splitlines()
        if l.strip()
    ]
    own = [r for r in lines if r.get("dispatch_id") == plan.dispatch_id]
    assert own, "the dispatch's own receipt line must land on the ledger"
    assert own[-1].get("status") != "success"


def test_headless_closeout_exception_preserves_worker_completion(tmp_path):
    """When the worker succeeded and the close-out then raised, the failure
    result preserves the worker's completion_text so a 'worker succeeded,
    close-out failed' receipt carries the worker's truth, not an empty
    fabrication (OI-1743 point A)."""
    plan = _make_headless_plan(tmp_path)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()
    (data_dir / "unified_reports").mkdir(parents=True)

    _fake_consumer_root = tmp_path / "consumer-root"

    worker_success = _AdapterResult(
        returncode=0, completion_text="the worker did the thing",
        status="success", model="sonnet",
    )

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=_fake_consumer_root), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch.object(ClaudeSubprocessAdapter, "run", return_value=worker_success), \
         patch("dispatch_envelope._enforce_push_pr", side_effect=RuntimeError("close-out boom")):
        result = run_envelope_headless_plan(
            plan, permit, state_dir=state_dir, data_dir=data_dir,
        )

    # The governed EnvelopeResult carries the worker's completion_text (the
    # receipt itself records a failure status; the completion text is the
    # worker's own output, preserved).
    assert "the worker did the thing" in result.completion_text, (
        "the worker's completion_text must survive a close-out exception, "
        "not be blanked to an empty fabrication"
    )
    assert result.error is not None
    assert "close-out boom" in result.error or "OI-1743" in result.error
