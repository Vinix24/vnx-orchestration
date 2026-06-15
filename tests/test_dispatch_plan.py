"""test_dispatch_plan.py — Tests for compile_plan: pure, total decision function.

Covers all dispatch rules D1-D12 including:
- Plan totality: every non-AUTO provider + every target slot returns ExecutionPlan
- AUTO rejection, staging gate, blocking constraints
- Claude lane fields, provider lane fields
- Model-tier pinning with warn-only overrides
- Target health/capability gating (provider lane only)
- Seed materialize logic
- Digest stability and ExecutionPermit compatibility (PlanLike protocol)
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_internal import issue_permit, require_permit
from dispatch_plan import (
    ConstraintVerdict,
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
)
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Isolation,
    PathAccess,
    Provider,
    Reject,
    ValidatedSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_instruction_file(tmp_path: Path) -> Path:
    f = tmp_path / "instruction.md"
    f.write_text("# Test instruction\n", encoding="utf-8")
    return f


def _make_spec(
    *,
    provider: Provider = Provider.CLAUDE,
    target_slot: str = "T1",
    model: str | None = None,
    dispatch_paths: tuple[DispatchPath, ...] = (),
    target_id_override: str | None = None,
    deadline_seconds: int = 3600,
    tmp_path: Path,
) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-dispatch-001",
        staging_id="staging-001",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot=target_slot,
        gate="human-promoted",
        dispatch_paths=dispatch_paths,
        provider=provider,
        model=model,
        deadline_seconds=deadline_seconds,
        target_id_override=target_id_override,
    )


def _make_vspec(
    *,
    provider: Provider = Provider.CLAUDE,
    target_slot: str = "T1",
    model: str | None = None,
    dispatch_paths: tuple[DispatchPath, ...] = (),
    target_id_override: str | None = None,
    tmp_path: Path,
) -> ValidatedSpec:
    spec = _make_spec(
        provider=provider,
        target_slot=target_slot,
        model=model,
        dispatch_paths=dispatch_paths,
        target_id_override=target_id_override,
        tmp_path=tmp_path,
    )
    return ValidatedSpec(
        spec=spec,
        instruction_text="# Test instruction\n",
        normalized_paths=dispatch_paths,
    )


def _healthy_snapshot(
    *,
    model_pins: dict | None = None,
    claude_serial_enabled: bool = True,
    constraint_verdicts: tuple[ConstraintVerdict, ...] = (),
) -> RuntimeSnapshot:
    """Promoted snapshot with all T0-T3 slots healthy and capable."""
    all_slots = ["T0", "T1", "T2", "T3"]
    return RuntimeSnapshot(
        staging_promoted=True,
        target_health={slot: "healthy" for slot in all_slots},
        target_capable={slot: True for slot in all_slots},
        model_pins=model_pins or {},
        claude_serial_enabled=claude_serial_enabled,
        constraint_verdicts=constraint_verdicts,
    )


# ---------------------------------------------------------------------------
# test_plan_total
# ---------------------------------------------------------------------------

class TestPlanTotal:
    """Every non-AUTO Provider x every target slot → ExecutionPlan; never None, never raises."""

    @pytest.mark.parametrize("provider", [p for p in Provider if p != Provider.AUTO])
    @pytest.mark.parametrize("target_slot", ["T0", "T1", "T2", "T3"])
    def test_compile_returns_plan(self, provider: Provider, target_slot: str, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=provider, target_slot=target_slot, tmp_path=tmp_path)
        snapshot = _healthy_snapshot()
        result = compile_plan(vspec, snapshot)
        assert isinstance(result, ExecutionPlan), (
            f"Expected ExecutionPlan for provider={provider.value} slot={target_slot}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# test_auto_rejected
# ---------------------------------------------------------------------------

class TestAutoRejected:
    def test_auto_rejected(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.AUTO, tmp_path=tmp_path)
        result = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(result, Reject)
        assert result.code == "unresolved-provider"


# ---------------------------------------------------------------------------
# test_claude_lane
# ---------------------------------------------------------------------------

class TestClaudeLane:
    def test_claude_lane_fields(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.lane == "claude_tmux_subscription"
        assert plan.adapter == "tmux_claude"
        assert plan.billing == "subscription"
        assert plan.serialization_class == "claude-tmux"
        assert plan.warmup == "verify_strict"
        assert plan.target_id == "ephemeral"


# ---------------------------------------------------------------------------
# test_provider_lane_parallel
# ---------------------------------------------------------------------------

class TestProviderLaneParallel:
    def test_codex_provider_lane(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CODEX, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.lane == "provider"
        assert plan.adapter == "provider"
        assert plan.serialization_class is None
        assert plan.billing == "provider_metered"


# ---------------------------------------------------------------------------
# test_staging_gate
# ---------------------------------------------------------------------------

class TestStagingGate:
    def test_staging_not_promoted_rejects(self, tmp_path: Path) -> None:
        vspec = _make_vspec(tmp_path=tmp_path)
        snapshot = RuntimeSnapshot(staging_promoted=False)
        result = compile_plan(vspec, snapshot)
        assert isinstance(result, Reject)
        assert result.code == "ADR-006"


# ---------------------------------------------------------------------------
# test_blocking_constraint
# ---------------------------------------------------------------------------

class TestBlockingConstraint:
    def test_blocking_verdict_rejects(self, tmp_path: Path) -> None:
        blocking = ConstraintVerdict(
            code="kimi-via-cli-only",
            severity="blocking",
            message="Kimi must use CLI OAuth, not Moonshot API",
        )
        vspec = _make_vspec(tmp_path=tmp_path)
        result = compile_plan(vspec, _healthy_snapshot(constraint_verdicts=(blocking,)))
        assert isinstance(result, Reject)
        assert result.code == "kimi-via-cli-only"

    def test_warn_verdict_does_not_reject(self, tmp_path: Path) -> None:
        warn = ConstraintVerdict(
            code="t0-opus-only",
            severity="warn",
            message="T0 orchestrator should use opus",
        )
        vspec = _make_vspec(tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot(constraint_verdicts=(warn,)))
        assert isinstance(plan, ExecutionPlan)
        assert any("t0-opus-only" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# test_model_tier_pin
# ---------------------------------------------------------------------------

class TestModelTierPin:
    def test_t0_pinned_to_opus(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T0", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot(model_pins={"T0": "opus"}))
        assert isinstance(plan, ExecutionPlan)
        assert plan.model == "opus"
        assert not any("model-tier" in w for w in plan.warnings)

    def test_t0_requested_sonnet_pinned_opus_keeps_pin_and_warns(self, tmp_path: Path) -> None:
        vspec = _make_vspec(
            provider=Provider.CLAUDE, target_slot="T0", model="sonnet", tmp_path=tmp_path
        )
        plan = compile_plan(vspec, _healthy_snapshot(model_pins={"T0": "opus"}))
        assert isinstance(plan, ExecutionPlan)
        assert plan.model == "opus"
        assert any("model-tier" in w for w in plan.warnings)

    def test_t1_pinned_to_sonnet(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot(model_pins={"T1": "sonnet"}))
        assert isinstance(plan, ExecutionPlan)
        assert plan.model == "sonnet"


# ---------------------------------------------------------------------------
# test_isolation_always_worktree
# ---------------------------------------------------------------------------

class TestIsolationAlwaysWorktree:
    @pytest.mark.parametrize("provider", [p for p in Provider if p != Provider.AUTO])
    def test_isolation_worktree(self, provider: Provider, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=provider, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.isolation == Isolation.WORKTREE
        assert plan.require_worktree is True


# ---------------------------------------------------------------------------
# test_target_health_provider_lane
# ---------------------------------------------------------------------------

class TestTargetHealthProviderLane:
    def test_unhealthy_target_rejects(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CODEX, target_slot="T1", tmp_path=tmp_path)
        snapshot = RuntimeSnapshot(
            staging_promoted=True,
            target_health={"T1": "unhealthy"},
            target_capable={"T1": True},
        )
        result = compile_plan(vspec, snapshot)
        assert isinstance(result, Reject)
        assert result.code == "R-6"

    def test_incapable_target_rejects(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CODEX, target_slot="T1", tmp_path=tmp_path)
        snapshot = RuntimeSnapshot(
            staging_promoted=True,
            target_health={"T1": "healthy"},
            target_capable={"T1": False},
        )
        result = compile_plan(vspec, snapshot)
        assert isinstance(result, Reject)
        assert result.code == "R-5"

    def test_claude_lane_skips_health_check(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        # No target health/capability data — claude lane must succeed regardless
        snapshot = RuntimeSnapshot(staging_promoted=True)
        plan = compile_plan(vspec, snapshot)
        assert isinstance(plan, ExecutionPlan)
        assert plan.target_id == "ephemeral"


# ---------------------------------------------------------------------------
# test_seed_materialize
# ---------------------------------------------------------------------------

class TestSeedMaterialize:
    def test_materialize_at_cwd_true(self, tmp_path: Path) -> None:
        paths = (DispatchPath(PurePosixPath("scripts/lib"), PathAccess.READ_WRITE, True),)
        vspec = _make_vspec(dispatch_paths=paths, tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.seed_materialize is True

    def test_empty_paths_seed_false(self, tmp_path: Path) -> None:
        vspec = _make_vspec(dispatch_paths=(), tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.seed_materialize is False


# ---------------------------------------------------------------------------
# test_digest_stable_and_permit_compatible
# ---------------------------------------------------------------------------

class TestDigestStableAndPermitCompatible:
    def test_same_plan_same_digest(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.digest() == plan.digest()

    def test_changed_field_different_digest(self, tmp_path: Path) -> None:
        snap = _healthy_snapshot()
        vspec_claude = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        vspec_codex = _make_vspec(provider=Provider.CODEX, target_slot="T1", tmp_path=tmp_path)
        plan_claude = compile_plan(vspec_claude, snap)
        plan_codex = compile_plan(vspec_codex, snap)
        assert isinstance(plan_claude, ExecutionPlan)
        assert isinstance(plan_codex, ExecutionPlan)
        assert plan_claude.digest() != plan_codex.digest()

    def test_permit_compatible(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        permit = issue_permit(plan)
        require_permit(plan, permit)  # must not raise PermissionError
