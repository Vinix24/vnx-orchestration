"""test_dispatch_envelope_field_propagation.py — OI-982 + OI-985: ExecutionPlan → EnvelopeSpec field propagation.

Verifies that:
1. pr_id flows from DispatchSpec → ExecutionPlan → EnvelopeSpec in both
   run_envelope_plan (provider lane) and run_envelope_headless_plan.
2. Chain-link fields (parent_dispatch, task_class, tier_from, tier_to) flow
   from ExecutionPlan → EnvelopeSpec in run_envelope_headless_plan.

These tests MUST fail on the pre-fix code (origin/main) and go green on the
fix branch. Pure unit assertions — no worker spawn.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    EnvelopeSpec,
    ProviderAdapter,
    _AdapterResult,
    run_envelope_headless_plan,
    run_envelope_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import (
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
)
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


def _make_spec_with_pr_id(
    *,
    pr_id: Optional[str],
    provider: Provider = Provider.CODEX,
    target_slot: str = "T1",
    model: str | None = None,
    allow_headless: bool = False,
    tmp_path: Path,
) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-prid-prop",
        staging_id="staging-prid",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot=target_slot,
        gate="codex_gate",
        dispatch_paths=(),
        provider=provider,
        model=model,
        pr_id=pr_id,
        allow_headless=allow_headless,
        headless_reason="test" if allow_headless else None,
    )


def _make_vspec_from_spec(spec: DispatchSpec) -> ValidatedSpec:
    instruction_text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


def _healthy_snapshot(
    *,
    parent_dispatch: Optional[str] = None,
    task_class: Optional[str] = None,
    tier_from: Optional[str] = None,
    tier_to: Optional[str] = None,
) -> RuntimeSnapshot:
    all_slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={slot: "healthy" for slot in all_slots},
        target_capable={slot: True for slot in all_slots},
        parent_dispatch=parent_dispatch,
        task_class=task_class,
        tier_from=tier_from,
        tier_to=tier_to,
    )


def _fake_adapter_success() -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="done", status="success")


# ---------------------------------------------------------------------------
# Test 1 — pr_id propagation: DispatchSpec → ExecutionPlan → EnvelopeSpec
# ---------------------------------------------------------------------------


class TestPrIdPropagation:
    """OI-982: pr_id must flow from DispatchSpec through both envelope entry points."""

    def test_pr_id_in_compiled_plan(self, tmp_path: Path) -> None:
        """compile_plan copies pr_id from DispatchSpec onto ExecutionPlan."""
        spec = _make_spec_with_pr_id(pr_id="123", provider=Provider.CODEX, tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.pr_id == "123", (
            f"Expected plan.pr_id='123', got {plan.pr_id!r}"
        )

    def test_pr_id_from_plan_to_envelope_spec_via_run_envelope_plan(
        self, tmp_path: Path
    ) -> None:
        """run_envelope_plan passes plan.pr_id to EnvelopeSpec (provider lane)."""
        spec = _make_spec_with_pr_id(pr_id="123", provider=Provider.CODEX, tmp_path=tmp_path)
        vspec = _make_vspec_from_spec(spec)
        plan = compile_plan(vspec, _healthy_snapshot())
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        _fake_consumer_root = tmp_path / "consumer-root"
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch.object(ProviderAdapter, "run", return_value=_fake_adapter_success()), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called — EnvelopeSpec never constructed"
        assert captured_specs[0].pr_id == "123", (
            f"run_envelope_plan: expected EnvelopeSpec.pr_id='123', "
            f"got {captured_specs[0].pr_id!r}"
        )

    def test_pr_id_from_plan_to_envelope_spec_via_run_envelope_headless_plan(
        self, tmp_path: Path
    ) -> None:
        """run_envelope_headless_plan passes plan.pr_id to EnvelopeSpec (headless lane)."""
        spec = _make_spec_with_pr_id(
            pr_id="123", provider=Provider.CLAUDE, allow_headless=True, tmp_path=tmp_path
        )
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

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        # This test asserts field PROPAGATION (pr_id), not push/PR enforcement —
        # which has its own dedicated coverage in test_pr_enforcement.py and
        # test_lane_matrix_row7_push_pr.py (confirmed green, 27 tests). Stubbing
        # the worktree allocator here (same form as TestPrIdPropagation's provider
        # lane test above) narrows this test's scope to what it actually claims to
        # test. Running the real allocator would drag in real `git worktree add`,
        # `ls-remote`, `git push` and `gh pr create` and rewrite a genuine success
        # to status="failure" on any CI runner where those cannot succeed — the
        # exact CI flake this dispatch fixes.
        _fake_consumer_root = tmp_path / "consumer-root"
        with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called — EnvelopeSpec never constructed"
        assert captured_specs[0].pr_id == "123", (
            f"run_envelope_headless_plan: expected EnvelopeSpec.pr_id='123', "
            f"got {captured_specs[0].pr_id!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 — chain-link field propagation through run_envelope_headless_plan
# ---------------------------------------------------------------------------


class TestChainLinkPropagationHeadless:
    """OI-985: chain-link fields must flow to EnvelopeSpec in the headless lane."""

    def test_chainlink_fields_in_headless_envelope_spec(self, tmp_path: Path) -> None:
        """All four chain-link fields reach EnvelopeSpec via run_envelope_headless_plan."""
        spec = _make_spec_with_pr_id(
            pr_id=None, provider=Provider.CLAUDE, allow_headless=True, tmp_path=tmp_path
        )
        vspec = _make_vspec_from_spec(spec)
        snapshot = _healthy_snapshot(
            parent_dispatch="20260801-000000-parent-dispatch",
            task_class="build",
            tier_from="T1",
            tier_to="T2",
        )
        plan = compile_plan(vspec, snapshot)
        assert plan.lane == "claude_headless"
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        captured_specs: list = []

        def capture_govern(spec_arg, *args, **kwargs):
            captured_specs.append(spec_arg)
            return (None, fake_receipt)

        # Asserts chain-link field PROPAGATION, not push/PR enforcement (covered
        # in test_pr_enforcement.py + test_lane_matrix_row7_push_pr.py, 27 green).
        # Stub the worktree allocator in the same form as the provider-lane test
        # in TestPrIdPropagation so the real `git worktree add`/`git push`/`gh pr
        # create` path cannot rewrite a genuine success to failure on a CI runner
        # that lacks git/gh/network — the source of this dispatch's CI flake.
        _fake_consumer_root = tmp_path / "consumer-root"
        with patch.object(dispatch_envelope.ClaudeSubprocessAdapter, "run",
                          return_value=_fake_adapter_success()), \
             patch("dispatch_worktree_isolation.resolve_consumer_project_root",
                   return_value=_fake_consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree",
                   return_value=tmp_path / "fake-wt"), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
             patch("dispatch_envelope._govern", side_effect=capture_govern):
            result = run_envelope_headless_plan(
                plan, permit, state_dir=state_dir, data_dir=data_dir,
                role=plan.role,
            )

        assert result.status == "success"
        assert captured_specs, "_govern was not called"

        env_spec = captured_specs[0]
        assert env_spec.parent_dispatch == "20260801-000000-parent-dispatch", (
            f"Expected parent_dispatch on EnvelopeSpec, got {env_spec.parent_dispatch!r}"
        )
        assert env_spec.task_class == "build", (
            f"Expected task_class='build', got {env_spec.task_class!r}"
        )
        assert env_spec.tier_from == "T1", (
            f"Expected tier_from='T1', got {env_spec.tier_from!r}"
        )
        assert env_spec.tier_to == "T2", (
            f"Expected tier_to='T2', got {env_spec.tier_to!r}"
        )
