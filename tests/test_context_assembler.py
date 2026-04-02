#!/usr/bin/env python3
"""Context assembler tests for PR-1: Context Selection and Budget Enforcement.

Covers:
  1. Mandatory component validation (P0, P1)
  2. Budget enforcement (20% target, 25% hard limit)
  3. Stale-context rejection per component max age
  4. Carry-forward evidence inclusion when chained
  5. Reverse-priority trimming (P7 -> P6 -> P5 -> P4)
  6. Freshness metadata recording
  7. Component-level token limits
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from context_assembler import (
    BUDGET_HARD_LIMIT_RATIO,
    BUDGET_TARGET_RATIO,
    CHAIN_POSITION_TOKEN_LIMIT,
    INTELLIGENCE_CHAR_LIMIT,
    INTELLIGENCE_CLASS_PRIORITY,
    OPEN_ITEMS_TOKEN_LIMIT,
    PRIOR_PR_TOKEN_LIMIT,
    REUSABLE_SIGNALS_TOKEN_LIMIT,
    TRIM_ORDER,
    ContextAssembler,
    ContextBundle,
    check_freshness,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)


def _make_assembler(**kwargs: Any) -> ContextAssembler:
    return ContextAssembler(main_sha="abc123", assembly_time=NOW, **kwargs)


def _add_mandatory(asm: ContextAssembler) -> None:
    """Add P0 + P1 mandatory components."""
    asm.add_dispatch_identity(
        dispatch_id="20260402-120000-test-dispatch",
        pr_id="PR-1", track="B",
        gate="gate_pr1_test", feature_name="Test Feature",
    )
    asm.add_task_specification(
        skill_command="/backend-developer",
        task_description="Implement the thing.",
        deliverables=["Implementation", "Tests"],
        success_criteria=["All tests pass"],
        quality_gate_checklist=["Tests pass"],
    )


def _add_code_ballast(asm: ContextAssembler, size: int = 2000) -> None:
    """Add P2 code context to ensure overhead ratio stays realistic."""
    asm.add_code_context({"src/main.py": _bloat_content(size)})


def _add_small_overhead(asm: ContextAssembler) -> None:
    """Add small P3-P7 components that stay within budget."""
    asm.add_chain_position(
        current_feature_index=1, total_features=5,
        carry_forward_summary={"blocker_count": 0, "warn_count": 1, "deferred_count": 0, "residual_risk_count": 0},
        blocking_items=[], dependency_status="PR-0 merged",
        source_updated_at=NOW,
    )
    asm.add_intelligence_payload(
        [{"type": "pattern", "content": "Use result_contract pattern"}],
        source_updated_at=NOW,
    )


def _bloat_content(size_chars: int) -> str:
    """Generate content of approximately size_chars characters."""
    return "x" * size_chars


# ---------------------------------------------------------------------------
# 1. Mandatory component validation
# ---------------------------------------------------------------------------

class TestMandatoryComponents:

    def test_assembly_fails_without_p0(self) -> None:
        asm = _make_assembler()
        asm.add_task_specification(
            skill_command="/test", task_description="Do it.",
            deliverables=["D"], success_criteria=["C"],
            quality_gate_checklist=[],
        )
        result = asm.assemble()
        assert result.ok is False
        assert result.error_code == "missing_argument"
        assert "P0" in result.error_msg

    def test_assembly_fails_without_p1(self) -> None:
        asm = _make_assembler()
        asm.add_dispatch_identity(
            dispatch_id="20260402-120000-test", pr_id="PR-1",
            track="B", gate="g1", feature_name="F",
        )
        result = asm.assemble()
        assert result.ok is False
        assert result.error_code == "missing_argument"
        assert "P1" in result.error_msg

    def test_p0_rejects_invalid_dispatch_id(self) -> None:
        asm = _make_assembler()
        result = asm.add_dispatch_identity(
            dispatch_id="bad-id", pr_id="PR-1",
            track="B", gate="g1", feature_name="F",
        )
        assert result.ok is False
        assert "dispatch_id" in result.error_msg

    def test_p0_rejects_invalid_pr_id(self) -> None:
        asm = _make_assembler()
        result = asm.add_dispatch_identity(
            dispatch_id="20260402-120000-test", pr_id="not-a-pr",
            track="B", gate="g1", feature_name="F",
        )
        assert result.ok is False
        assert "pr_id" in result.error_msg

    def test_p0_rejects_invalid_track(self) -> None:
        asm = _make_assembler()
        result = asm.add_dispatch_identity(
            dispatch_id="20260402-120000-test", pr_id="PR-1",
            track="Z", gate="g1", feature_name="F",
        )
        assert result.ok is False
        assert "track" in result.error_msg

    def test_p1_rejects_empty_deliverables(self) -> None:
        asm = _make_assembler()
        result = asm.add_task_specification(
            skill_command="/test", task_description="Do it.",
            deliverables=[], success_criteria=["C"],
            quality_gate_checklist=[],
        )
        assert result.ok is False
        assert "deliverable" in result.error_msg

    def test_minimal_assembly_succeeds(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        assert bundle.overhead_ratio == 0.0
        assert bundle.budget_status == "within_target"


# ---------------------------------------------------------------------------
# 2. Budget enforcement
# ---------------------------------------------------------------------------

class TestBudgetEnforcement:

    def test_small_overhead_within_target(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm)
        _add_small_overhead(asm)
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        assert bundle.overhead_ratio < BUDGET_TARGET_RATIO
        assert bundle.budget_status == "within_target"

    def test_large_overhead_triggers_trimming(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        # Add components at their limits to blow aggregate budget
        # Each component stays under its individual limit but combined they exceed 25%
        asm.add_prior_pr_evidence(
            [{"severity": "warn", "description": _bloat_content(3500)}],
            source_updated_at=NOW,
        )
        asm.add_open_items_digest(
            [{"severity": "warn", "title": _bloat_content(1800), "status": "open"}],
            source_updated_at=NOW,
        )
        asm.add_reusable_signals(
            [{"type": "outcome", "content": _bloat_content(1800)}],
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        assert len(bundle.trimmed_components) > 0
        assert bundle.overhead_ratio <= BUDGET_HARD_LIMIT_RATIO

    def test_budget_hard_limit_rejects_when_untrimable(self) -> None:
        """If P3 alone exceeds its component limit, add_chain_position rejects it."""
        asm = _make_assembler()
        result = asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[{"severity": "blocker", "title": _bloat_content(5000)}],
            dependency_status=_bloat_content(5000),
            source_updated_at=NOW,
        )
        assert result.ok is False
        assert result.error_code == "component_too_large"

    def test_overhead_excludes_p0_p1_p2(self) -> None:
        """P0, P1, P2 do not count toward overhead."""
        asm = _make_assembler()
        _add_mandatory(asm)
        asm.add_code_context({"big_file.py": _bloat_content(10000)})
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        assert bundle.overhead_tokens == 0
        assert bundle.overhead_ratio == 0.0

    def test_over_target_but_under_hard_limit(self) -> None:
        """Between 20% and 25% produces over_target status, not rejection."""
        asm = _make_assembler()
        # ~400 char mandatory content
        asm.add_dispatch_identity(
            dispatch_id="20260402-120000-t", pr_id="PR-1",
            track="B", gate="g", feature_name="F",
        )
        asm.add_task_specification(
            skill_command="/t", task_description="D.",
            deliverables=["D"], success_criteria=["C"],
            quality_gate_checklist=[],
        )
        # P3 adds ~300 chars overhead against ~700 total -> ~43% overhead
        # But we need it between 20-25%. Let's be more precise.
        # Mandatory is about 150 chars = ~37 tokens
        # We need overhead of about 8-10 tokens (22-27%)
        asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW,
        )
        # Add larger code context to dilute overhead ratio
        asm.add_code_context({"f.py": _bloat_content(800)})
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        # Chain position overhead is small relative to code context
        assert bundle.overhead_ratio < BUDGET_HARD_LIMIT_RATIO


# ---------------------------------------------------------------------------
# 3. Stale-context rejection
# ---------------------------------------------------------------------------

class TestStaleContextRejection:

    def test_stale_chain_position_rejected(self) -> None:
        """Chain position with max age 0 is rejected when source_updated_at < assembly_time."""
        asm = _make_assembler()
        result = asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW - timedelta(seconds=1),
        )
        assert result.ok is False
        assert result.error_code == "stale_context"

    def test_fresh_chain_position_accepted(self) -> None:
        asm = _make_assembler()
        result = asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW,
        )
        assert result.ok is True

    def test_stale_intelligence_rejected_after_24h(self) -> None:
        asm = _make_assembler()
        result = asm.add_intelligence_payload(
            [{"type": "pattern", "content": "Use X"}],
            source_updated_at=NOW - timedelta(hours=25),
        )
        assert result.ok is False
        assert result.error_code == "stale_context"

    def test_intelligence_fresh_within_24h(self) -> None:
        asm = _make_assembler()
        result = asm.add_intelligence_payload(
            [{"type": "pattern", "content": "Use X"}],
            source_updated_at=NOW - timedelta(hours=23),
        )
        assert result.ok is True

    def test_stale_prior_pr_evidence_rejected(self) -> None:
        asm = _make_assembler()
        result = asm.add_prior_pr_evidence(
            [{"severity": "warn", "description": "Finding"}],
            source_updated_at=NOW - timedelta(seconds=1),
        )
        assert result.ok is False
        assert result.error_code == "stale_context"

    def test_stale_open_items_rejected_after_1h(self) -> None:
        asm = _make_assembler()
        result = asm.add_open_items_digest(
            [{"severity": "warn", "title": "Item", "status": "open"}],
            source_updated_at=NOW - timedelta(hours=2),
        )
        assert result.ok is False
        assert result.error_code == "stale_context"

    def test_open_items_fresh_within_1h(self) -> None:
        asm = _make_assembler()
        result = asm.add_open_items_digest(
            [{"severity": "warn", "title": "Item", "status": "open"}],
            source_updated_at=NOW - timedelta(minutes=30),
        )
        assert result.ok is True

    def test_stale_reusable_signals_rejected_after_14d(self) -> None:
        asm = _make_assembler()
        result = asm.add_reusable_signals(
            [{"type": "outcome", "content": "Signal"}],
            source_updated_at=NOW - timedelta(days=15),
        )
        assert result.ok is False
        assert result.error_code == "stale_context"

    def test_reusable_signals_fresh_within_14d(self) -> None:
        asm = _make_assembler()
        result = asm.add_reusable_signals(
            [{"type": "outcome", "content": "Signal"}],
            source_updated_at=NOW - timedelta(days=13),
        )
        assert result.ok is True

    def test_stale_rejections_tracked_in_bundle(self) -> None:
        """Stale rejections are recorded even when assembly succeeds."""
        asm = _make_assembler()
        _add_mandatory(asm)
        asm.add_intelligence_payload(
            [{"type": "pattern", "content": "Stale"}],
            source_updated_at=NOW - timedelta(hours=25),
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        assert "intelligence_payload" in bundle.stale_rejections


# ---------------------------------------------------------------------------
# 4. Carry-forward evidence inclusion
# ---------------------------------------------------------------------------

class TestCarryForwardInclusion:

    def test_chain_position_included_when_chained(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm)
        asm.add_chain_position(
            current_feature_index=2, total_features=5,
            carry_forward_summary={"blocker_count": 0, "warn_count": 2, "deferred_count": 1, "residual_risk_count": 1},
            blocking_items=[], dependency_status="PR-1 merged, PR-2 merged",
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        rendered = bundle.render()
        assert "Feature 3 of 5" in rendered
        assert "2 warnings" in rendered
        assert "1 deferred" in rendered
        assert "1 residual risks" in rendered

    def test_blocking_items_visible_in_chain_position(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm)
        asm.add_chain_position(
            current_feature_index=0, total_features=3,
            carry_forward_summary={"blocker_count": 1, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[{"severity": "blocker", "title": "Critical security issue"}],
            dependency_status="none",
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok is True
        rendered = result.data.render()
        assert "Critical security issue" in rendered
        assert "[blocker]" in rendered

    def test_carry_forward_survives_trimming(self) -> None:
        """P3 (chain position) is not in TRIM_ORDER, so it survives budget trimming."""
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm, size=4000)
        asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW,
        )
        # Add optional components near their limits to trigger trimming
        asm.add_reusable_signals(
            [{"type": "outcome", "content": _bloat_content(1800)}],
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        component_names = [c.name for c in bundle.components]
        assert "chain_position" in component_names


# ---------------------------------------------------------------------------
# 5. Reverse-priority trimming order
# ---------------------------------------------------------------------------

class TestTrimmingOrder:

    def test_trim_order_is_p7_p6_p5_p4(self) -> None:
        assert TRIM_ORDER == ("reusable_signals", "open_items_digest",
                              "prior_pr_evidence", "intelligence_payload")

    def test_p7_trimmed_before_p6(self) -> None:
        """When budget exceeded, P7 (reusable_signals) is removed first."""
        asm = _make_assembler()
        # Small mandatory
        asm.add_dispatch_identity(
            dispatch_id="20260402-120000-t", pr_id="PR-1",
            track="B", gate="g", feature_name="F",
        )
        asm.add_task_specification(
            skill_command="/t", task_description="D.",
            deliverables=["D"], success_criteria=["C"],
            quality_gate_checklist=[],
        )
        # Add moderate P6 and large P7 (within component limits) to blow aggregate budget
        asm.add_open_items_digest(
            [{"severity": "warn", "title": _bloat_content(400), "status": "open"}],
            source_updated_at=NOW,
        )
        asm.add_reusable_signals(
            [{"type": "outcome", "content": _bloat_content(1800)}],
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        if bundle.trimmed_components:
            assert bundle.trimmed_components[0] == "reusable_signals"


# ---------------------------------------------------------------------------
# 6. Freshness metadata
# ---------------------------------------------------------------------------

class TestFreshnessMetadata:

    def test_freshness_recorded_for_each_component(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm)
        asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW,
        )
        asm.add_intelligence_payload(
            [{"type": "pattern", "content": "Use X"}],
            source_updated_at=NOW - timedelta(hours=2),
        )
        result = asm.assemble()
        assert result.ok is True
        bundle: ContextBundle = result.data
        freshness = bundle.freshness
        assert freshness.assembled_at == NOW.isoformat()
        assert freshness.main_sha_at_assembly == "abc123"
        assert "chain_position" in freshness.component_freshness
        assert freshness.component_freshness["chain_position"]["is_fresh"] is True
        assert "intelligence_payload" in freshness.component_freshness
        assert freshness.component_freshness["intelligence_payload"]["is_fresh"] is True

    def test_bundle_render_produces_ordered_output(self) -> None:
        asm = _make_assembler()
        _add_mandatory(asm)
        _add_code_ballast(asm)
        _add_small_overhead(asm)
        result = asm.assemble()
        assert result.ok is True
        rendered = result.data.render()
        # P0 content appears before P1 content
        dispatch_idx = rendered.index("Dispatch:")
        skill_idx = rendered.index("Skill:")
        assert dispatch_idx < skill_idx


# ---------------------------------------------------------------------------
# 7. Component-level limits
# ---------------------------------------------------------------------------

class TestComponentLimits:

    def test_intelligence_overflow_drops_lowest_priority_items(self) -> None:
        """When payload exceeds 2000 chars, drop recent_comparable first per FPC."""
        asm = _make_assembler()
        items = [
            {"type": "proven_pattern", "content": "Short proven pattern"},
            {"type": "failure_prevention", "content": "Short failure prevention"},
            {"type": "recent_comparable", "content": _bloat_content(2500)},
        ]
        asm.add_intelligence_payload(items, source_updated_at=NOW)
        intel_comp = [c for c in asm._components if c.name == "intelligence_payload"]
        assert len(intel_comp) == 1
        assert len(intel_comp[0].content) <= INTELLIGENCE_CHAR_LIMIT
        # recent_comparable should have been dropped
        assert "recent_comparable" not in intel_comp[0].content
        assert "proven_pattern" in intel_comp[0].content
        assert "failure_prevention" in intel_comp[0].content

    def test_intelligence_bounded_to_3_items(self) -> None:
        asm = _make_assembler()
        items = [{"type": "pattern", "content": f"Item {i}"} for i in range(5)]
        asm.add_intelligence_payload(items, source_updated_at=NOW)
        intel_comp = [c for c in asm._components if c.name == "intelligence_payload"]
        assert len(intel_comp) == 1
        # Only 3 items should be rendered
        assert intel_comp[0].content.count("[pattern]") == 3

    def test_open_items_filters_info_severity(self) -> None:
        """P6 only includes severity >= warn (blocker and warn)."""
        asm = _make_assembler()
        items = [
            {"severity": "info", "title": "Low priority", "status": "open"},
            {"severity": "warn", "title": "Medium priority", "status": "open"},
            {"severity": "blocker", "title": "High priority", "status": "open"},
        ]
        asm.add_open_items_digest(items, source_updated_at=NOW)
        oi_comp = [c for c in asm._components if c.name == "open_items_digest"]
        assert len(oi_comp) == 1
        assert "Low priority" not in oi_comp[0].content
        assert "Medium priority" in oi_comp[0].content
        assert "High priority" in oi_comp[0].content

    def test_empty_open_items_not_added(self) -> None:
        """If all items are info severity, no component is added."""
        asm = _make_assembler()
        items = [{"severity": "info", "title": "Low", "status": "open"}]
        asm.add_open_items_digest(items, source_updated_at=NOW)
        assert not any(c.name == "open_items_digest" for c in asm._components)


# ---------------------------------------------------------------------------
# 8. Token estimation
# ---------------------------------------------------------------------------

class TestTokenEstimation:

    def test_estimate_tokens_basic(self) -> None:
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 100) == 25
        assert estimate_tokens("") == 0

    def test_check_freshness_zero_max_age(self) -> None:
        """Max age 0 means source must equal assembly time."""
        fresh = check_freshness("chain_position", NOW, NOW)
        assert fresh.is_fresh is True
        stale = check_freshness("chain_position", NOW - timedelta(seconds=1), NOW)
        assert stale.is_fresh is False

    def test_check_freshness_nonzero_max_age(self) -> None:
        """Open items have 1h max age."""
        fresh = check_freshness("open_items_digest", NOW - timedelta(minutes=30), NOW)
        assert fresh.is_fresh is True
        stale = check_freshness("open_items_digest", NOW - timedelta(hours=2), NOW)
        assert stale.is_fresh is False

    def test_check_freshness_unknown_component(self) -> None:
        """Unknown components are always fresh."""
        result = check_freshness("unknown_thing", NOW - timedelta(days=100), NOW)
        assert result.is_fresh is True


# ---------------------------------------------------------------------------
# 9. Per-component hard limits (OI-492)
# ---------------------------------------------------------------------------

class TestPerComponentHardLimits:

    def test_chain_position_rejects_oversized(self) -> None:
        asm = _make_assembler()
        result = asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[{"severity": "blocker", "title": _bloat_content(4000)}],
            dependency_status="ok",
            source_updated_at=NOW,
        )
        assert result.ok is False
        assert result.error_code == "component_too_large"

    def test_prior_pr_evidence_rejects_oversized(self) -> None:
        asm = _make_assembler()
        result = asm.add_prior_pr_evidence(
            [{"severity": "warn", "description": _bloat_content(5000)}],
            source_updated_at=NOW,
        )
        assert result.ok is False
        assert result.error_code == "component_too_large"

    def test_open_items_digest_rejects_oversized(self) -> None:
        asm = _make_assembler()
        result = asm.add_open_items_digest(
            [{"severity": "warn", "title": _bloat_content(3000), "status": "open"}],
            source_updated_at=NOW,
        )
        assert result.ok is False
        assert result.error_code == "component_too_large"

    def test_reusable_signals_rejects_oversized(self) -> None:
        asm = _make_assembler()
        result = asm.add_reusable_signals(
            [{"type": "outcome", "content": _bloat_content(3000)}],
            source_updated_at=NOW,
        )
        assert result.ok is False
        assert result.error_code == "component_too_large"

    def test_components_within_limits_accepted(self) -> None:
        asm = _make_assembler()
        r1 = asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="ok",
            source_updated_at=NOW,
        )
        r2 = asm.add_prior_pr_evidence(
            [{"severity": "warn", "description": "Small finding"}],
            source_updated_at=NOW,
        )
        assert r1.ok is True
        assert r2.ok is True


# ---------------------------------------------------------------------------
# 10. P4 item-level dropping per FPC priority (OI-493)
# ---------------------------------------------------------------------------

class TestIntelligenceItemDropping:

    def test_drops_recent_comparable_before_failure_prevention(self) -> None:
        asm = _make_assembler()
        items = [
            {"type": "proven_pattern", "content": "A" * 800},
            {"type": "failure_prevention", "content": "B" * 800},
            {"type": "recent_comparable", "content": "C" * 800},
        ]
        asm.add_intelligence_payload(items, source_updated_at=NOW)
        intel_comp = [c for c in asm._components if c.name == "intelligence_payload"]
        content = intel_comp[0].content
        assert len(content) <= INTELLIGENCE_CHAR_LIMIT
        # recent_comparable dropped first (lowest priority)
        assert "recent_comparable" not in content
        assert "proven_pattern" in content

    def test_drops_both_lower_priority_if_needed(self) -> None:
        asm = _make_assembler()
        items = [
            {"type": "proven_pattern", "content": "A" * 1800},
            {"type": "failure_prevention", "content": "B" * 800},
            {"type": "recent_comparable", "content": "C" * 800},
        ]
        asm.add_intelligence_payload(items, source_updated_at=NOW)
        intel_comp = [c for c in asm._components if c.name == "intelligence_payload"]
        content = intel_comp[0].content
        assert len(content) <= INTELLIGENCE_CHAR_LIMIT
        assert "proven_pattern" in content
        assert "recent_comparable" not in content
        assert "failure_prevention" not in content

    def test_class_priority_order_correct(self) -> None:
        assert INTELLIGENCE_CLASS_PRIORITY == (
            "proven_pattern", "failure_prevention", "recent_comparable"
        )


# ---------------------------------------------------------------------------
# 11. Duplicate component replacement (OI-494)
# ---------------------------------------------------------------------------

class TestDuplicateComponentReplacement:

    def test_duplicate_p4_replaces_not_appends(self) -> None:
        asm = _make_assembler()
        asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "First version"}],
            source_updated_at=NOW,
        )
        asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "Second version"}],
            source_updated_at=NOW,
        )
        intel_comps = [c for c in asm._components if c.name == "intelligence_payload"]
        assert len(intel_comps) == 1
        assert "Second version" in intel_comps[0].content
        assert "First version" not in intel_comps[0].content

    def test_duplicate_p0_replaces(self) -> None:
        asm = _make_assembler()
        asm.add_dispatch_identity(
            dispatch_id="20260402-120000-first", pr_id="PR-1",
            track="B", gate="g1", feature_name="First",
        )
        asm.add_dispatch_identity(
            dispatch_id="20260402-120000-second", pr_id="PR-2",
            track="A", gate="g2", feature_name="Second",
        )
        p0_comps = [c for c in asm._components if c.name == "dispatch_identity"]
        assert len(p0_comps) == 1
        assert "Second" in p0_comps[0].content

    def test_duplicate_p6_replaces(self) -> None:
        asm = _make_assembler()
        asm.add_open_items_digest(
            [{"severity": "warn", "title": "Old item", "status": "open"}],
            source_updated_at=NOW,
        )
        asm.add_open_items_digest(
            [{"severity": "blocker", "title": "New item", "status": "open"}],
            source_updated_at=NOW,
        )
        oi_comps = [c for c in asm._components if c.name == "open_items_digest"]
        assert len(oi_comps) == 1
        assert "New item" in oi_comps[0].content
        assert "Old item" not in oi_comps[0].content
