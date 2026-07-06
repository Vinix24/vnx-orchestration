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

import hashlib
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_internal import is_valid_instruction_hash, issue_permit, require_permit
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
    instruction_text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=dispatch_paths,
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
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
# test_billing_classification — per-provider-billing-phantom PR
# ---------------------------------------------------------------------------

class TestBillingClassification:
    """kimi/glm-harness/litellm:zai/deepseek-harness are OAuth/own-key harness
    lanes for this account, not per-token metered — billing_lanes.py SSOT."""

    @pytest.mark.parametrize(
        "provider",
        [Provider.KIMI, Provider.LITELLM_ZAI, Provider.GLM_HARNESS, Provider.DEEPSEEK_HARNESS],
    )
    def test_subscription_lane_providers_classify_as_subscription(
        self, provider: Provider, tmp_path: Path
    ) -> None:
        vspec = _make_vspec(provider=provider, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.billing == "subscription", (
            f"expected subscription billing for {provider.value}, got {plan.billing!r}"
        )

    def test_claude_lane_stays_subscription(self, tmp_path: Path) -> None:
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.billing == "subscription"

    @pytest.mark.parametrize(
        "provider",
        [Provider.CODEX, Provider.GEMINI, Provider.LITELLM_DEEPSEEK, Provider.LITELLM_MOONSHOT],
    )
    def test_genuinely_metered_providers_stay_provider_metered(
        self, provider: Provider, tmp_path: Path
    ) -> None:
        vspec = _make_vspec(provider=provider, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.billing == "provider_metered", (
            f"expected provider_metered billing for {provider.value}, got {plan.billing!r}"
        )

    def test_headless_claude_stays_api_metered(self, tmp_path: Path) -> None:
        """Headless claude (allow_headless=True) is the one genuinely metered claude lane."""
        vspec = _make_vspec_headless(tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.billing == "api_metered"


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


# ---------------------------------------------------------------------------
# test_instruction_sha256_in_plan
# ---------------------------------------------------------------------------

class TestInstructionSha256InPlan:
    def test_compile_plan_sets_instruction_sha256(self, tmp_path: Path) -> None:
        """compile_plan propagates instruction_sha256 from ValidatedSpec into ExecutionPlan."""
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.instruction_sha256 == vspec.instruction_sha256
        assert len(plan.instruction_sha256) == 64

    @pytest.mark.parametrize("provider", [p for p in Provider if p != Provider.AUTO])
    def test_compiled_plan_hash_is_always_valid_64hex(self, provider: Provider, tmp_path: Path) -> None:
        """P0-3 (PR-4c): a plan from compile_plan never carries an empty/invalid hash —
        the empty default cannot flow through to an executor. Every lane is fail-closed."""
        vspec = _make_vspec(provider=provider, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert is_valid_instruction_hash(plan.instruction_sha256), (
            f"compile_plan produced an invalid hash for {provider.value}: {plan.instruction_sha256!r}"
        )

    def test_instruction_sha256_changes_digest(self, tmp_path: Path) -> None:
        """Changing instruction_sha256 on an otherwise identical plan changes digest()."""
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)

        # Build a variant with a different sha256 (simulate different instruction content)
        from dataclasses import replace
        other_sha = hashlib.sha256(b"different content").hexdigest()
        plan_b = replace(plan, instruction_sha256=other_sha)

        assert plan.digest() != plan_b.digest(), (
            "Plans with different instruction_sha256 must produce different digests"
        )

    def test_headless_plan_hash_is_valid(self, tmp_path: Path) -> None:
        """claude_headless plan always carries a valid 64-hex instruction hash."""
        spec = DispatchSpec(
            schema_version=1,
            project_id="vnx-dev",
            dispatch_id="test-headless-hash",
            staging_id="staging-headless",
            instruction_file=_fake_instruction_file(tmp_path),
            role="backend-developer",
            target_slot="T1",
            gate="human-promoted",
            dispatch_paths=(),
            provider=Provider.CLAUDE,
            allow_headless=True,
            headless_reason="benchmark",
        )
        instruction_text = "# Test instruction\n"
        vspec = ValidatedSpec(
            spec=spec,
            instruction_text=instruction_text,
            normalized_paths=(),
            instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
        )
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert is_valid_instruction_hash(plan.instruction_sha256)

    def test_default_sha256_is_empty_string(self, tmp_path: Path) -> None:
        """ExecutionPlan constructed without instruction_sha256 defaults to empty string."""
        ifile = tmp_path / "instruction.md"
        ifile.write_text("test", encoding="utf-8")
        plan = ExecutionPlan(
            dispatch_id="test-default-sha",
            project_id="vnx-dev",
            provider=Provider.CLAUDE,
            model="sonnet",
            lane="claude_tmux_subscription",
            adapter="tmux_claude",
            target_id="ephemeral",
            billing="subscription",
            serialization_class="claude-tmux",
            isolation=Isolation.WORKTREE,
            require_worktree=True,
            seed_materialize=False,
            instruction_delivery="file_ref",
            report_contract="required",
            warmup="verify_strict",
            deadline_seconds=3600,
            base_ref="origin/main",
            dispatch_paths=(),
            instruction_file=ifile,
            route_reason="D1",
        )
        assert plan.instruction_sha256 == ""


# ---------------------------------------------------------------------------
# PR-5 — claude_headless lane
# ---------------------------------------------------------------------------

def _make_vspec_headless(
    *,
    headless_reason: str = "burst benchmark",
    target_slot: str = "T1",
    model: str | None = None,
    tmp_path: Path,
) -> ValidatedSpec:
    """Build a ValidatedSpec with allow_headless=True for headless lane tests."""
    spec = DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="test-headless-dispatch",
        staging_id="staging-headless-001",
        instruction_file=_fake_instruction_file(tmp_path),
        role="backend-developer",
        target_slot=target_slot,
        gate="human-promoted",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        model=model,
        allow_headless=True,
        headless_reason=headless_reason,
    )
    instruction_text = "# Test instruction\n"
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction_text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


class TestClaudeHeadlessLane:
    def test_headless_optin_lane_fields(self, tmp_path: Path) -> None:
        """allow_headless=True → lane=claude_headless, adapter=claude_subprocess, billing=api_metered."""
        vspec = _make_vspec_headless(tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.lane == "claude_headless"
        assert plan.adapter == "claude_subprocess"
        assert plan.billing == "api_metered"
        assert plan.serialization_class is None
        assert plan.warmup == "n/a"
        assert plan.target_id == "ephemeral"

    def test_headless_warning_contains_reason(self, tmp_path: Path) -> None:
        """Headless plan carries a LOUD warning containing the headless_reason."""
        reason = "quarterly burst load benchmark"
        vspec = _make_vspec_headless(headless_reason=reason, tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert any("HEADLESS API-billing opted-in" in w for w in plan.warnings)
        assert any(reason in w for w in plan.warnings)

    def test_default_claude_remains_tmux(self, tmp_path: Path) -> None:
        """Default Claude (allow_headless=False) → claude_tmux_subscription unchanged."""
        vspec = _make_vspec(provider=Provider.CLAUDE, target_slot="T1", tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.lane == "claude_tmux_subscription"
        assert plan.billing == "subscription"
        assert plan.serialization_class == "claude-tmux"
        assert plan.warmup == "verify_strict"
        assert not any("HEADLESS" in w for w in plan.warnings)

    def test_headless_isolation_always_worktree(self, tmp_path: Path) -> None:
        """claude_headless inherits the universal isolation=WORKTREE rule."""
        vspec = _make_vspec_headless(tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot())
        assert isinstance(plan, ExecutionPlan)
        assert plan.isolation == Isolation.WORKTREE
        assert plan.require_worktree is True

    def test_headless_serial_disabled_still_no_serial_class(self, tmp_path: Path) -> None:
        """claude_headless never gets serialization_class even when claude_serial_enabled=True."""
        vspec = _make_vspec_headless(tmp_path=tmp_path)
        plan = compile_plan(vspec, _healthy_snapshot(claude_serial_enabled=True))
        assert isinstance(plan, ExecutionPlan)
        assert plan.serialization_class is None
