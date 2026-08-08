"""test_headless_worktree_isolation.py — OI-1045: headless lane honors isolation=worktree.

Verifies that:
1. run_envelope_headless_plan creates a dispatch worktree and passes cwd=wt_path
   to ClaudeSubprocessAdapter — the worker runs inside the worktree, not the
   main checkout.
2. Failed worktree creation hard-aborts the headless dispatch — no silent
   fallback to the main checkout (the exact OI-1045 failure mode).

These tests MUST fail on the pre-fix code (origin/main) and go green on the
fix branch. Pure unit assertions — no worker spawn.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    EnvelopeSpec,
    _AdapterResult,
    run_envelope_headless_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Isolation,
    Provider,
    ValidatedSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_instruction_file(tmp_path: Path) -> Path:
    f = tmp_path / "instruction.md"
    f.write_text("# Test instruction\n", encoding="utf-8")
    return f


def _make_headless_spec(
    *,
    tmp_path: Path,
    pr_id: Optional[str] = None,
    target_slot: str = "T1",
    model: str = "sonnet",
) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-headless-wt",
        staging_id="staging-headless-wt",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot=target_slot,
        gate="human-promoted",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        model=model,
        pr_id=pr_id,
        allow_headless=True,
        headless_reason="test",
    )


def _make_vspec_from_spec(spec: DispatchSpec) -> ValidatedSpec:
    instruction_text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


def _healthy_snapshot() -> RuntimeSnapshot:
    all_slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={slot: "healthy" for slot in all_slots},
        target_capable={slot: True for slot in all_slots},
    )


def _fake_adapter_success() -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="done", status="success")


# ---------------------------------------------------------------------------
# Test 1 — worktree created and cwd passed to adapter
# ---------------------------------------------------------------------------


class TestHeadlessWorktreeIsolation:
    """OI-1045: headless lane must create a worktree and run the adapter inside it."""

    def test_worktree_created_and_cwd_passed_to_adapter(self, tmp_path: Path) -> None:
        """run_envelope_headless_plan creates a worktree and passes cwd to the adapter."""
        spec = _make_headless_spec(tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert plan.lane == "claude_headless", f"Expected claude_headless, got {plan.lane}"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        fake_consumer_root = tmp_path / "consumer-root"
        fake_consumer_root.mkdir()
        fake_wt_path = fake_consumer_root / ".vnx-data" / "worktrees" / f"dispatch-{plan.dispatch_id}"
        fake_wt_path.mkdir(parents=True)

        captured_cwd: list = []

        def capture_adapter_run(spec_arg, cwd=None, **kwargs):
            captured_cwd.append(cwd)
            return _fake_adapter_success()

        def capture_govern(spec_arg, *args, **kwargs):
            return (fake_receipt, fake_receipt)

        with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=fake_wt_path), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          side_effect=capture_adapter_run), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success", (
            f"Expected success, got status={result.status!r} error={result.error!r}"
        )
        assert captured_cwd, (
            "ClaudeSubprocessAdapter.run() was never called — cwd not captured"
        )
        passed_cwd = captured_cwd[0]
        assert passed_cwd is not None, (
            "cwd was not passed to ClaudeSubprocessAdapter.run() — "
            "worker would run in the main checkout"
        )
        assert passed_cwd == fake_wt_path, (
            f"Expected cwd={fake_wt_path} (the dispatch worktree), "
            f"got cwd={passed_cwd} — worker runs in the wrong directory"
        )

    def test_failed_worktree_creation_aborts_headless_dispatch(self, tmp_path: Path) -> None:
        """When create_dispatch_worktree raises, the headless dispatch fails hard."""
        spec = _make_headless_spec(tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert plan.lane == "claude_headless"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        fake_consumer_root = tmp_path / "consumer-root"
        fake_consumer_root.mkdir()

        adapter_called = []

        def refuse_create(*args, **kwargs):
            raise RuntimeError("simulated worktree creation failure")

        def capture_adapter_run(*args, **kwargs):
            adapter_called.append(True)
            return _fake_adapter_success()

        def capture_govern(spec_arg, *args, **kwargs):
            return (fake_receipt, fake_receipt)

        with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   side_effect=refuse_create), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          side_effect=capture_adapter_run), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        # OI-1045 core assertion: the dispatch must FAIL, not silently continue.
        assert result.status == "failure", (
            f"Expected status='failure' when worktree creation fails, "
            f"got status={result.status!r} — the dispatch fell through to "
            f"the adapter without a worktree (the exact OI-1045 bug)"
        )
        assert not adapter_called, (
            "ClaudeSubprocessAdapter.run() was called despite worktree creation "
            "failure — the adapter should never be reached on a failed worktree"
        )
        assert result.error is not None, (
            "result.error must carry the isolation error message"
        )
        assert "worktree creation failed" in result.error, (
            f"Error message should mention worktree creation failure, got: {result.error!r}"
        )

    def test_worktree_removed_after_successful_dispatch(self, tmp_path: Path) -> None:
        """The worktree is torn down (remove_dispatch_worktree called) after a successful dispatch."""
        spec = _make_headless_spec(tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert plan.lane == "claude_headless"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        fake_consumer_root = tmp_path / "consumer-root"
        fake_consumer_root.mkdir()
        fake_wt_path = fake_consumer_root / ".vnx-data" / "worktrees" / f"dispatch-{plan.dispatch_id}"
        fake_wt_path.mkdir(parents=True)

        remove_calls = []

        def capture_remove(*args, **kwargs):
            remove_calls.append(True)

        def capture_govern(spec_arg, *args, **kwargs):
            return (fake_receipt, fake_receipt)

        with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=fake_wt_path), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree",
                   side_effect=capture_remove), \
             patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success"
        assert remove_calls, (
            "remove_dispatch_worktree was never called — teardown is missing; "
            "the worktree would leak on every headless dispatch"
        )
