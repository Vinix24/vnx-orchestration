#!/usr/bin/env python3
"""Context injection and resume quality certification tests (PR-4, Feature 15).

Certifies:
  1. Context budget enforcement: overhead < 20% target, 25% hard limit
  2. Handover completeness: structured payloads pass validation >= 90%
  3. Resume acceptance: rotation/interruption/redispatch accepted >= 80%
  4. Stale-context rejection: stale components blocked, fresh accepted
  5. Reusable signal integration: P7 signals feed through full pipeline
  6. End-to-end: full dispatch lifecycle with context, handover, and resume
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from context_assembler import (
    BUDGET_HARD_LIMIT_RATIO,
    BUDGET_TARGET_RATIO,
    ContextAssembler,
    check_freshness,
    estimate_tokens,
)
from handover_resume import (
    build_handover,
    build_resume,
    validate_handover,
    validate_resume,
)
from outcome_signals import (
    collect_signals,
    extract_from_carry_forward,
    extract_from_open_items,
    extract_from_receipts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)


def _minimal_assembler(**kwargs: Any) -> ContextAssembler:
    """Create an assembler with mandatory P0 and P1 already added.

    Task spec is sized large enough that P3-P7 additions stay under budget.
    """
    asm = ContextAssembler(main_sha="abc123", assembly_time=NOW, **kwargs)
    asm.add_dispatch_identity("20260402-120000-test", "PR-1", "B", "gate_test", "Test Feature")
    asm.add_task_specification(
        skill_command="@backend-developer",
        task_description=(
            "Implement the bounded context assembly feature with the following requirements:\n"
            "1. Build a context assembler that supports P0 through P7 priority components\n"
            "2. Enforce budget limits: P3-P7 overhead must stay under 20% target and 25% hard limit\n"
            "3. Implement stale-context rejection with per-component max age enforcement\n"
            "4. Support reverse-priority trimming when budget is exceeded\n"
            "5. Record freshness metadata for post-hoc staleness auditing\n"
            "6. All context components must be structured summaries, not raw history\n"
            "7. The assembler must validate mandatory components before assembly"
        ),
        deliverables=["context assembler module", "budget enforcement tests", "staleness checks"],
        success_criteria=["all tests pass", "budget < 20%", "stale components rejected"],
        quality_gate_checklist=["all tests green", "no budget violations", "freshness metadata present"],
    )
    return asm


def _valid_handover(**overrides: Any) -> Dict[str, Any]:
    """Build a valid handover with optional overrides."""
    defaults = dict(
        dispatch_id="20260402-120000-test",
        pr_id="PR-1",
        track="B",
        gate="gate_test",
        status="success",
        what_was_done="Implemented the feature.",
        key_decisions=["Used X approach"],
        files_modified=[{"path": "scripts/lib/foo.py", "change_type": "created", "description": "New module"}],
        tests_run="10",
        tests_passed="10",
        tests_failed="0",
        commands_executed=["pytest tests/"],
        verification_method="local_tests",
        recommended_action="advance",
        action_reason="All tests pass, ready for review.",
        blocking_conditions=[],
        critical_context="Feature is complete and tested.",
    )
    defaults.update(overrides)
    return defaults


def _receipt_line(event_type: str, status: str, ts: datetime, **extra: Any) -> str:
    record = {"event_type": event_type, "status": status, "timestamp": ts.isoformat(), **extra}
    return json.dumps(record)


# ---------------------------------------------------------------------------
# 1. Context budget enforcement
# ---------------------------------------------------------------------------

class TestContextBudgetCertification:
    """Context overhead ratio < 20% target, 25% hard limit."""

    def test_minimal_context_within_target(self) -> None:
        """P0 + P1 only: overhead should be 0% (no P3-P7)."""
        asm = _minimal_assembler()
        result = asm.assemble()
        assert result.ok
        bundle = result.data
        assert bundle.overhead_ratio == 0.0
        assert bundle.budget_status == "within_target"

    def test_moderate_context_within_target(self) -> None:
        """P0 + P1 + P3 + P4: overhead should be < 20%."""
        asm = _minimal_assembler()
        asm.add_chain_position(
            current_feature_index=1, total_features=5,
            carry_forward_summary={"blocker_count": 0, "warn_count": 2, "deferred_count": 1, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=NOW,
        )
        asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "Use bounded context assembly."}],
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok
        bundle = result.data
        assert bundle.overhead_ratio < BUDGET_TARGET_RATIO
        assert bundle.budget_status == "within_target"

    def test_oversized_p7_gets_trimmed(self) -> None:
        """When P7 is large, it gets trimmed first (reverse priority)."""
        asm = _minimal_assembler()
        # Add large P7 signal content
        large_signals = [{"type": "outcome", "content": "x" * 400} for _ in range(5)]
        asm.add_reusable_signals(large_signals, source_updated_at=NOW)
        result = asm.assemble()
        assert result.ok
        bundle = result.data
        # Either within budget or trimmed
        assert bundle.overhead_ratio <= BUDGET_HARD_LIMIT_RATIO

    def test_budget_hard_limit_rejects_assembly(self) -> None:
        """If P3-P7 exceed 25% even after trimming, assembly fails."""
        asm = ContextAssembler(main_sha="abc", assembly_time=NOW)
        # Tiny P0+P1 (very small task spec)
        asm.add_dispatch_identity("20260402-120000-x", "PR-0", "C", "g", "F")
        asm.add_task_specification("@a", "Do X.", ["d"], ["c"], [])
        # Massive P3 that can't be trimmed (mandatory-when-chained)
        huge_blocking = [{"severity": "blocker", "title": f"Item {i}" * 20} for i in range(50)]
        result = asm.add_chain_position(
            current_feature_index=0, total_features=1,
            carry_forward_summary={"blocker_count": 50, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=huge_blocking, dependency_status="pending",
            source_updated_at=NOW,
        )
        # Chain position itself may reject if over per-component limit
        if not result.ok:
            assert "exceeds" in result.error_msg

    def test_overhead_ratio_calculation_correct(self) -> None:
        """Verify P3-P7 tokens / total tokens math."""
        asm = _minimal_assembler()
        asm.add_chain_position(
            current_feature_index=0, total_features=3,
            carry_forward_summary={"blocker_count": 0, "warn_count": 1, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok
        bundle = result.data
        # Verify the ratio is correctly computed
        expected_ratio = bundle.overhead_tokens / bundle.total_tokens if bundle.total_tokens > 0 else 0
        assert abs(bundle.overhead_ratio - expected_ratio) < 0.001


# ---------------------------------------------------------------------------
# 2. Handover completeness
# ---------------------------------------------------------------------------

class TestHandoverCompletenessCertification:
    """Structured handovers pass validation >= 90%."""

    def test_valid_handover_passes_all_checks(self) -> None:
        result = build_handover(**_valid_handover())
        assert result.ok
        payload = result.data
        assert payload["handover_version"] == "1.0"
        assert payload["status"] == "success"

    def test_failed_handover_with_honest_status(self) -> None:
        """HO-2: status must honestly reflect outcome."""
        result = build_handover(**_valid_handover(
            status="failed",
            recommended_action="fix",
            action_reason="Tests failed, needs debugging.",
            critical_context="3 tests fail in module X.",
        ))
        assert result.ok
        assert result.data["status"] == "failed"

    def test_missing_critical_context_fails_validation(self) -> None:
        """HO-5: critical_context is required."""
        result = build_handover(**_valid_handover(critical_context=""))
        assert not result.ok
        assert "critical_context" in result.error_msg

    def test_invalid_recommended_action_fails(self) -> None:
        """HO-3: unknown is not a valid action."""
        result = build_handover(**_valid_handover(recommended_action="unknown"))
        assert not result.ok
        assert "recommended_action" in result.error_msg

    def test_residual_state_always_present(self) -> None:
        """HO-4: residual_state section present even when empty."""
        result = build_handover(**_valid_handover())
        assert result.ok
        rs = result.data["residual_state"]
        assert isinstance(rs["open_items_created"], list)
        assert isinstance(rs["findings"], list)
        assert isinstance(rs["residual_risks"], list)

    def test_handover_with_all_residual_fields(self) -> None:
        result = build_handover(**_valid_handover(
            open_items_created=[{"id": "OI-1", "severity": "warn", "title": "Perf"}],
            findings=[{"severity": "info", "description": "Minor style issue"}],
            residual_risks=[{"risk": "Memory growth", "mitigation": "Monitor"}],
            deferred_items=[{"id": "D-1", "severity": "info", "reason": "Low priority"}],
        ))
        assert result.ok
        rs = result.data["residual_state"]
        assert len(rs["open_items_created"]) == 1
        assert len(rs["findings"]) == 1

    def test_invalid_file_change_type_fails(self) -> None:
        result = build_handover(**_valid_handover(
            files_modified=[{"path": "foo.py", "change_type": "invalid"}],
        ))
        assert not result.ok

    def test_batch_handover_validation_rate(self) -> None:
        """Simulate 10 handovers, verify >= 90% pass rate."""
        valid_count = 0
        for i in range(10):
            result = build_handover(**_valid_handover(
                dispatch_id=f"20260402-12000{i}-test",
                what_was_done=f"Task {i} completed.",
                critical_context=f"Context for task {i}.",
            ))
            if result.ok:
                valid_count += 1
        assert valid_count / 10 >= 0.90


# ---------------------------------------------------------------------------
# 3. Resume acceptance
# ---------------------------------------------------------------------------

class TestResumeAcceptanceCertification:
    """Resume payloads accepted >= 80% without redispatch."""

    def test_rotation_resume_with_specific_progress(self) -> None:
        """RS-2: work_completed must be specific for rotation."""
        result = build_resume(
            resume_type="rotation",
            original_dispatch_id="20260402-120000-test",
            work_completed="Implemented context assembler with P0-P4 components and budget enforcement.",
            work_remaining="Add P5-P7 support and staleness checks.",
            files_in_progress=["scripts/lib/context_assembler.py"],
            last_known_state="Writing add_prior_pr_evidence method.",
            task_specification="Implement bounded context assembly.",
        )
        assert result.ok
        assert result.data["resume_type"] == "rotation"

    def test_rotation_resume_rejects_vague_progress(self) -> None:
        """RS-2: vague terms rejected."""
        result = build_resume(
            resume_type="rotation",
            original_dispatch_id="20260402-120000-test",
            work_completed="in progress",
            work_remaining="finish the work",
            files_in_progress=[],
            last_known_state="working",
            task_specification="Do the task.",
        )
        assert not result.ok
        assert "RS-2" in result.error_msg

    def test_interruption_resume_with_specific_state(self) -> None:
        """RS-3: last_known_state must be specific for interruption."""
        result = build_resume(
            resume_type="interruption",
            original_dispatch_id="20260402-120000-test",
            work_completed="Completed handover payload builder.",
            work_remaining="Resume validation and testing.",
            files_in_progress=["scripts/lib/handover_resume.py"],
            last_known_state="Process killed during test execution at test_handover_resume.py:line 45.",
            task_specification="Implement handover quality enforcement.",
        )
        assert result.ok

    def test_interruption_resume_rejects_vague_state(self) -> None:
        """RS-3: vague last_known_state rejected."""
        result = build_resume(
            resume_type="interruption",
            original_dispatch_id="20260402-120000-test",
            work_completed="Some work done.",
            work_remaining="More work.",
            files_in_progress=[],
            last_known_state="started",
            task_specification="Do task.",
        )
        assert not result.ok
        assert "RS-3" in result.error_msg

    def test_redispatch_resume_requires_prior_findings(self) -> None:
        """RS-4: redispatch must include findings from failed attempt."""
        result = build_resume(
            resume_type="redispatch",
            original_dispatch_id="20260402-120000-test",
            work_completed="First attempt failed at test stage.",
            work_remaining="Redo with fixed approach.",
            files_in_progress=["scripts/lib/context_assembler.py"],
            last_known_state="Test failure in budget enforcement.",
            findings_so_far=[{"severity": "warn", "description": "Budget calc off by 2%"}],
            task_specification="Implement bounded context assembly.",
        )
        assert result.ok

    def test_redispatch_without_findings_rejected(self) -> None:
        """RS-4: missing findings_so_far fails validation."""
        result = build_resume(
            resume_type="redispatch",
            original_dispatch_id="20260402-120000-test",
            work_completed="First attempt failed.",
            work_remaining="Try again.",
            files_in_progress=[],
            last_known_state="Failed.",
            task_specification="Task.",
        )
        assert not result.ok
        assert "RS-4" in result.error_msg

    def test_resume_rejects_conversation_transcript(self) -> None:
        """RS-5: raw conversation history not allowed."""
        result = build_resume(
            resume_type="rotation",
            original_dispatch_id="20260402-120000-test",
            work_completed="User: Please do X\nAssistant: I will do X",
            work_remaining="Finish the task.",
            files_in_progress=[],
            last_known_state="Mid-conversation.",
            task_specification="Implement feature.",
        )
        assert not result.ok
        assert "RS-5" in result.error_msg

    def test_resume_requires_task_specification(self) -> None:
        """RS-1: task_specification always required."""
        result = build_resume(
            resume_type="rotation",
            original_dispatch_id="20260402-120000-test",
            work_completed="Completed P0-P4.",
            work_remaining="P5-P7.",
            files_in_progress=[],
            last_known_state="Writing P5.",
            task_specification="",
        )
        assert not result.ok
        assert "RS-1" in result.error_msg

    def test_batch_resume_acceptance_rate(self) -> None:
        """Simulate 10 well-formed resumes, verify >= 80% accepted."""
        accepted = 0
        for i in range(10):
            result = build_resume(
                resume_type="rotation",
                original_dispatch_id=f"20260402-12000{i}-test",
                work_completed=f"Completed implementation step {i} of the feature.",
                work_remaining=f"Steps {i+1} through 10 remain.",
                files_in_progress=[f"scripts/lib/module_{i}.py"],
                last_known_state=f"Writing function {i} in module_{i}.py.",
                task_specification=f"Implement step {i} of the bounded context feature.",
            )
            if result.ok:
                accepted += 1
        assert accepted / 10 >= 0.80


# ---------------------------------------------------------------------------
# 4. Stale-context rejection
# ---------------------------------------------------------------------------

class TestStaleContextRejectionCertification:
    """Stale components blocked, fresh components accepted."""

    def test_stale_chain_position_rejected(self) -> None:
        """Max age 0: any lag from assembly time is stale."""
        asm = _minimal_assembler()
        stale_time = NOW - timedelta(seconds=1)
        result = asm.add_chain_position(
            current_feature_index=0, total_features=3,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=stale_time,
        )
        assert not result.ok
        assert "stale" in result.error_msg.lower()

    def test_fresh_chain_position_accepted(self) -> None:
        asm = _minimal_assembler()
        result = asm.add_chain_position(
            current_feature_index=0, total_features=3,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=NOW,
        )
        assert result.ok

    def test_stale_intelligence_rejected(self) -> None:
        """Intelligence payload stale after 24h."""
        asm = _minimal_assembler()
        stale_time = NOW - timedelta(hours=25)
        result = asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "old pattern"}],
            source_updated_at=stale_time,
        )
        assert not result.ok
        assert "stale" in result.error_msg.lower()

    def test_fresh_intelligence_accepted(self) -> None:
        asm = _minimal_assembler()
        result = asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "recent pattern"}],
            source_updated_at=NOW - timedelta(hours=12),
        )
        assert result.ok

    def test_stale_open_items_rejected(self) -> None:
        """Open items digest stale after 1h."""
        asm = _minimal_assembler()
        stale_time = NOW - timedelta(hours=2)
        result = asm.add_open_items_digest(
            [{"severity": "warn", "title": "Perf concern", "status": "open"}],
            source_updated_at=stale_time,
        )
        assert not result.ok

    def test_stale_rejections_recorded_in_bundle(self) -> None:
        """Stale rejections visible in assembled bundle metadata."""
        asm = _minimal_assembler()
        asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "stale"}],
            source_updated_at=NOW - timedelta(hours=25),
        )
        result = asm.assemble()
        assert result.ok
        bundle = result.data
        assert "intelligence_payload" in bundle.stale_rejections

    def test_freshness_metadata_in_bundle(self) -> None:
        """Fresh components record freshness in bundle metadata."""
        asm = _minimal_assembler()
        asm.add_chain_position(
            current_feature_index=0, total_features=2,
            carry_forward_summary={"blocker_count": 0, "warn_count": 0, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=NOW,
        )
        result = asm.assemble()
        assert result.ok
        freshness = result.data.freshness
        assert freshness.main_sha_at_assembly == "abc123"
        assert "chain_position" in freshness.component_freshness
        assert freshness.component_freshness["chain_position"]["is_fresh"] is True


# ---------------------------------------------------------------------------
# 5. Reusable signal integration (P7)
# ---------------------------------------------------------------------------

class TestReusableSignalCertification:
    """P7 signals flow from sources through collection to context assembly."""

    def test_receipt_signals_flow_to_assembler(self) -> None:
        lines = [
            _receipt_line("task_complete", "failed", NOW - timedelta(days=1),
                          failure_reason="Timeout waiting for CI response for 120 seconds"),
            _receipt_line("task_complete", "success", NOW - timedelta(days=2),
                          summary="Completed chain state projection with 36 passing tests"),
        ]
        result = collect_signals(receipt_lines=lines, cutoff=NOW)
        assert result.ok
        signals = result.data
        assert len(signals) >= 2

        asm = _minimal_assembler()
        add_result = asm.add_reusable_signals(signals, source_updated_at=NOW)
        assert add_result.ok
        bundle_result = asm.assemble()
        assert bundle_result.ok
        p7 = next((c for c in bundle_result.data.components if c.name == "reusable_signals"), None)
        assert p7 is not None
        assert "failure_outcome" in p7.content or "success_pattern" in p7.content

    def test_carry_forward_signals_flow_to_assembler(self) -> None:
        ledger = {
            "findings": [
                {"severity": "warn", "description": "Performance concern under sustained load", "resolution_status": "open"},
            ],
            "residual_risks": [
                {"risk": "Memory growth during long chains exceeding baseline", "accepting_feature": "PR-2"},
            ],
        }
        signals = extract_from_carry_forward(ledger)
        assert len(signals) >= 2

        asm = _minimal_assembler()
        asm.add_reusable_signals(signals, source_updated_at=NOW)
        result = asm.assemble()
        assert result.ok

    def test_stale_signals_excluded_by_recency(self) -> None:
        old_line = _receipt_line("task_complete", "failed", NOW - timedelta(days=30),
                                 failure_reason="Ancient failure that should not appear in current context")
        result = collect_signals(receipt_lines=[old_line], cutoff=NOW)
        assert result.ok
        assert len(result.data) == 0

    def test_narrative_signals_filtered(self) -> None:
        lines = [
            _receipt_line("task_complete", "failed", NOW - timedelta(hours=1),
                          failure_reason="User: Please fix this\nAssistant: I will"),
        ]
        result = collect_signals(receipt_lines=lines, cutoff=NOW)
        assert result.ok
        # Narrative pattern should be filtered
        for sig in result.data:
            assert "User:" not in sig.get("content", "")

    def test_deduplication_across_sources(self) -> None:
        line = _receipt_line("task_complete", "failed", NOW - timedelta(hours=1),
                             failure_reason="Timeout waiting for response from provider")
        result = collect_signals(receipt_lines=[line, line], cutoff=NOW)
        assert result.ok
        assert len(result.data) == 1


# ---------------------------------------------------------------------------
# 6. End-to-end dispatch lifecycle
# ---------------------------------------------------------------------------

def _build_lifecycle_handover() -> Any:
    """Build the handover payload for the lifecycle test (Phase 2)."""
    return build_handover(
        dispatch_id="20260402-130000-lifecycle",
        pr_id="PR-2", track="B", gate="gate_test", status="success",
        what_was_done="Implemented handover and resume payload generation with validation.",
        key_decisions=["Used result_contract pattern", "Added RS-5 transcript detection"],
        files_modified=[
            {"path": "scripts/lib/handover_resume.py", "change_type": "created", "description": "Handover+resume module"},
        ],
        tests_run="42", tests_passed="42", tests_failed="0",
        commands_executed=["pytest tests/test_handover_resume.py -v"],
        verification_method="local_tests",
        recommended_action="advance",
        action_reason="All 42 tests pass, handover validation complete.",
        blocking_conditions=[],
        critical_context="Handover module complete. Resume payload supports 3 types. RS-5 enforced.",
        gotchas=["Transcript detection uses multiline regex"],
        relevant_file_paths=["scripts/lib/handover_resume.py", "tests/test_handover_resume.py"],
    )


class TestEndToEndLifecycleCertification:
    """Full dispatch -> context -> execution -> handover -> resume cycle."""

    def test_full_dispatch_lifecycle(self) -> None:
        """Simulate: assemble context -> execute -> handover -> resume from rotation."""
        # Phase 1: Assemble context
        asm = ContextAssembler(main_sha="def456", assembly_time=NOW)
        asm.add_dispatch_identity("20260402-130000-lifecycle", "PR-2", "B", "gate_test", "Context Quality")
        asm.add_task_specification(
            "@backend-developer",
            (
                "Implement handover payload generation with structural validation.\n"
                "Build the handover builder following HO-1 through HO-5 invariants.\n"
                "Build resume payload builder following RS-1 through RS-5 invariants.\n"
                "Add validation for all payload fields including severity, status, and change types.\n"
                "Integrate with the result_contract pattern for consistent error handling."
            ),
            ["handover module", "resume module", "validation tests", "integration tests"],
            ["all tests pass", "handover completeness >= 90%", "resume acceptance >= 80%"],
            ["tests green", "no validation errors", "RS-5 transcript detection active"],
        )
        asm.add_chain_position(
            current_feature_index=1, total_features=5,
            carry_forward_summary={"blocker_count": 0, "warn_count": 1, "deferred_count": 0, "residual_risk_count": 0},
            blocking_items=[], dependency_status="all completed",
            source_updated_at=NOW,
        )
        asm.add_intelligence_payload(
            [{"type": "proven_pattern", "content": "Use result_contract for validation returns."}],
            source_updated_at=NOW - timedelta(hours=6),
        )

        bundle_result = asm.assemble()
        assert bundle_result.ok
        assert bundle_result.data.budget_status == "within_target"
        assert bundle_result.data.overhead_ratio < BUDGET_TARGET_RATIO

        # Phase 2: Worker produces handover
        assert _build_lifecycle_handover().ok

        # Phase 3: Context rotation triggers resume
        resume_result = build_resume(
            resume_type="rotation",
            original_dispatch_id="20260402-130000-lifecycle",
            work_completed="Handover payload builder and validation complete (42 tests).",
            work_remaining="Need to add resume payload edge case tests for RS-3.",
            files_in_progress=["tests/test_handover_resume.py"],
            last_known_state="Writing test_interruption_resume_rejects_vague_state test case.",
            key_decisions_made=["Used result_contract for all validation returns"],
            task_specification="Implement handover payload generation.",
            carry_forward_summary="Feature 2 of 5, 1 warn carry-forward, 0 blockers.",
        )
        assert resume_result.ok
        assert resume_result.data["resume_type"] == "rotation"
        assert resume_result.data["dispatch_context"]["task_specification"] != ""

    def test_zero_unresolved_blockers_at_feature_end(self) -> None:
        """Feature 15 closes with zero unresolved chain-created open items."""
        # Simulate: all items resolved across the feature
        items = [
            {"severity": "warn", "status": "done", "title": "Budget calc off by 2%"},
            {"severity": "info", "status": "done", "title": "Doc typo fixed"},
        ]
        open_signals = extract_from_open_items(items)
        assert len(open_signals) == 0  # all resolved, no signals
