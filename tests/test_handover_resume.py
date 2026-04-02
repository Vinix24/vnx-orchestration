#!/usr/bin/env python3
"""Handover and resume payload tests for PR-2.

Covers:
  1. Handover payload construction and validation (HO-1..HO-5)
  2. Resume payload construction and validation (RS-1..RS-5)
  3. Residual state and open items survive into handovers
  4. Resume fidelity across all three resume types
  5. Edge cases and invalid inputs
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from handover_resume import (
    VALID_ACTIONS,
    VALID_RESUME_TYPES,
    VALID_STATUSES,
    VALID_VERIFICATION_METHODS,
    build_handover,
    build_resume,
    validate_handover,
    validate_resume,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_handover_kwargs() -> Dict[str, Any]:
    return dict(
        dispatch_id="20260402-120000-test-dispatch",
        pr_id="PR-2", track="B", gate="gate_pr2_test",
        status="success",
        what_was_done="Implemented handover payloads.",
        key_decisions=["Used builder pattern"],
        files_modified=[
            {"path": "scripts/lib/handover_resume.py", "change_type": "created", "description": "New module"},
        ],
        tests_run="10", tests_passed="10", tests_failed="0",
        commands_executed=["python -m pytest tests/"],
        verification_method="local_tests",
        recommended_action="advance",
        action_reason="All tests pass, no blockers",
        blocking_conditions=[],
        open_items_created=[],
        findings=[],
        residual_risks=[],
        deferred_items=[],
        critical_context="Handover module is new; no existing callers yet.",
        gotchas=["Token estimation is approximate"],
        relevant_file_paths=["scripts/lib/handover_resume.py"],
    )


def _valid_resume_kwargs() -> Dict[str, Any]:
    return dict(
        resume_type="rotation",
        original_dispatch_id="20260402-120000-test-dispatch",
        original_session_id="session-abc123",
        work_completed="Implemented handover builder and 5 tests.",
        work_remaining="Resume builder and remaining tests.",
        files_in_progress=["scripts/lib/handover_resume.py"],
        last_known_state="Handover builder done, resume builder not started.",
        key_decisions_made=["Used result_contract pattern"],
        findings_so_far=[],
        blockers_encountered=[],
        task_specification="Implement standardized handover and resume payloads.",
        carry_forward_summary="Feature 3 of 5, 0 blockers",
    )


# ---------------------------------------------------------------------------
# 1. Handover payload construction
# ---------------------------------------------------------------------------

class TestHandoverConstruction:

    def test_valid_handover_succeeds(self) -> None:
        result = build_handover(**_valid_handover_kwargs())
        assert result.ok is True
        payload = result.data
        assert payload["handover_version"] == "1.0"
        assert payload["status"] == "success"
        assert payload["dispatch_id"] == "20260402-120000-test-dispatch"

    def test_handover_includes_all_sections(self) -> None:
        result = build_handover(**_valid_handover_kwargs())
        payload = result.data
        assert "completion_summary" in payload
        assert "evidence" in payload
        assert "next_action" in payload
        assert "residual_state" in payload
        assert "context_for_next" in payload

    def test_failed_handover_with_honest_status(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["status"] = "failed"
        kwargs["recommended_action"] = "fix"
        kwargs["action_reason"] = "3 tests failed"
        result = build_handover(**kwargs)
        assert result.ok is True
        assert result.data["status"] == "failed"

    def test_partial_handover_accepted(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["status"] = "partial"
        kwargs["recommended_action"] = "review"
        kwargs["action_reason"] = "Only half complete"
        result = build_handover(**kwargs)
        assert result.ok is True
        assert result.data["status"] == "partial"


# ---------------------------------------------------------------------------
# 2. Handover validation (HO invariants)
# ---------------------------------------------------------------------------

class TestHandoverValidation:

    def test_ho2_invalid_status_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["status"] = "unknown"
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "status" in result.error_msg

    def test_ho3_unknown_action_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["recommended_action"] = "unknown"
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "recommended_action" in result.error_msg

    def test_ho3_empty_reason_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["action_reason"] = ""
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "reason" in result.error_msg

    def test_ho4_residual_state_with_empty_arrays_accepted(self) -> None:
        result = build_handover(**_valid_handover_kwargs())
        assert result.ok is True
        rs = result.data["residual_state"]
        assert rs["open_items_created"] == []
        assert rs["findings"] == []
        assert rs["residual_risks"] == []
        assert rs["deferred_items"] == []

    def test_ho5_missing_critical_context_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["critical_context"] = ""
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "critical_context" in result.error_msg
        assert "HO-5" in result.error_msg

    def test_invalid_dispatch_id_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["dispatch_id"] = "bad-id"
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "dispatch_id" in result.error_msg

    def test_invalid_track_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["track"] = "Z"
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "track" in result.error_msg

    def test_invalid_verification_method_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["verification_method"] = "magic"
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "verification_method" in result.error_msg

    def test_invalid_change_type_in_files_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["files_modified"] = [{"path": "f.py", "change_type": "updated"}]
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "change_type" in result.error_msg

    def test_invalid_open_item_severity_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["open_items_created"] = [{"id": "OI-1", "severity": "critical", "title": "T"}]
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "severity" in result.error_msg

    def test_missing_what_was_done_rejected(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["what_was_done"] = ""
        result = build_handover(**kwargs)
        assert result.ok is False
        assert "what_was_done" in result.error_msg

    def test_all_valid_actions_accepted(self) -> None:
        for action in VALID_ACTIONS:
            kwargs = _valid_handover_kwargs()
            kwargs["recommended_action"] = action
            result = build_handover(**kwargs)
            assert result.ok is True, f"Action {action} should be valid"

    def test_all_valid_statuses_accepted(self) -> None:
        for status in VALID_STATUSES:
            kwargs = _valid_handover_kwargs()
            kwargs["status"] = status
            result = build_handover(**kwargs)
            assert result.ok is True, f"Status {status} should be valid"


# ---------------------------------------------------------------------------
# 3. Residual state survives into handovers
# ---------------------------------------------------------------------------

class TestResidualStateSurvival:

    def test_open_items_preserved(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["open_items_created"] = [
            {"id": "OI-1", "severity": "warn", "title": "Perf concern"},
            {"id": "OI-2", "severity": "blocker", "title": "Security gap"},
        ]
        result = build_handover(**kwargs)
        assert result.ok is True
        ois = result.data["residual_state"]["open_items_created"]
        assert len(ois) == 2
        assert ois[0]["id"] == "OI-1"
        assert ois[1]["severity"] == "blocker"

    def test_findings_preserved(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["findings"] = [
            {"severity": "warn", "description": "Token estimation rough"},
        ]
        result = build_handover(**kwargs)
        assert result.ok is True
        assert len(result.data["residual_state"]["findings"]) == 1

    def test_residual_risks_preserved(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["residual_risks"] = [
            {"risk": "Context overflow under load", "mitigation": "Add circuit breaker"},
        ]
        result = build_handover(**kwargs)
        assert result.ok is True
        risks = result.data["residual_state"]["residual_risks"]
        assert len(risks) == 1
        assert risks[0]["risk"] == "Context overflow under load"

    def test_deferred_items_preserved(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["deferred_items"] = [
            {"id": "D-1", "severity": "info", "reason": "Low priority"},
        ]
        result = build_handover(**kwargs)
        assert result.ok is True
        assert len(result.data["residual_state"]["deferred_items"]) == 1

    def test_gotchas_and_file_paths_in_context_for_next(self) -> None:
        kwargs = _valid_handover_kwargs()
        kwargs["gotchas"] = ["Watch out for token estimation", "P2 has no limit"]
        kwargs["relevant_file_paths"] = ["scripts/lib/context_assembler.py"]
        result = build_handover(**kwargs)
        assert result.ok is True
        ctx = result.data["context_for_next"]
        assert len(ctx["gotchas"]) == 2
        assert "context_assembler.py" in ctx["relevant_file_paths"][0]


# ---------------------------------------------------------------------------
# 4. Resume payload construction
# ---------------------------------------------------------------------------

class TestResumeConstruction:

    def test_valid_rotation_resume_succeeds(self) -> None:
        result = build_resume(**_valid_resume_kwargs())
        assert result.ok is True
        payload = result.data
        assert payload["resume_version"] == "1.0"
        assert payload["resume_type"] == "rotation"

    def test_valid_interruption_resume_succeeds(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "interruption"
        kwargs["last_known_state"] = "Editing handover_resume.py line 150"
        result = build_resume(**kwargs)
        assert result.ok is True

    def test_valid_redispatch_resume_succeeds(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "redispatch"
        kwargs["findings_so_far"] = [
            {"severity": "warn", "description": "Prior attempt had import error"},
        ]
        result = build_resume(**kwargs)
        assert result.ok is True

    def test_resume_includes_all_sections(self) -> None:
        result = build_resume(**_valid_resume_kwargs())
        payload = result.data
        assert "prior_progress" in payload
        assert "context_snapshot" in payload
        assert "dispatch_context" in payload


# ---------------------------------------------------------------------------
# 5. Resume validation (RS invariants)
# ---------------------------------------------------------------------------

class TestResumeValidation:

    def test_rs1_missing_task_spec_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["task_specification"] = ""
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "task_specification" in result.error_msg
        assert "RS-1" in result.error_msg

    def test_rs2_rotation_vague_work_completed_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "rotation"
        kwargs["work_completed"] = "in progress"
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "RS-2" in result.error_msg

    def test_rs2_rotation_specific_work_completed_accepted(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "rotation"
        kwargs["work_completed"] = "Built handover module with 5 validators"
        result = build_resume(**kwargs)
        assert result.ok is True

    def test_rs3_interruption_vague_state_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "interruption"
        kwargs["last_known_state"] = "in progress"
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "RS-3" in result.error_msg

    def test_rs3_interruption_specific_state_accepted(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "interruption"
        kwargs["last_known_state"] = "Writing test_handover_resume.py, 15 of 20 tests done"
        result = build_resume(**kwargs)
        assert result.ok is True

    def test_rs4_redispatch_missing_findings_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "redispatch"
        kwargs["findings_so_far"] = []
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "RS-4" in result.error_msg

    def test_rs4_redispatch_with_findings_accepted(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "redispatch"
        kwargs["findings_so_far"] = [
            {"severity": "warn", "description": "Import path wrong"},
        ]
        result = build_resume(**kwargs)
        assert result.ok is True

    def test_invalid_resume_type_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "restart"
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "resume_type" in result.error_msg

    def test_invalid_dispatch_id_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["original_dispatch_id"] = "bad"
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "dispatch_id" in result.error_msg

    def test_missing_work_remaining_rejected(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["work_remaining"] = ""
        result = build_resume(**kwargs)
        assert result.ok is False
        assert "work_remaining" in result.error_msg

    def test_all_resume_types_accepted_when_valid(self) -> None:
        for rt in VALID_RESUME_TYPES:
            kwargs = _valid_resume_kwargs()
            kwargs["resume_type"] = rt
            if rt == "interruption":
                kwargs["last_known_state"] = "Editing file X at line 50"
            if rt == "redispatch":
                kwargs["findings_so_far"] = [{"severity": "info", "description": "Prior issue"}]
            result = build_resume(**kwargs)
            assert result.ok is True, f"Resume type {rt} should be valid"


# ---------------------------------------------------------------------------
# 6. Residual state survives into resumes
# ---------------------------------------------------------------------------

class TestResumeResidualSurvival:

    def test_findings_survive_into_resume(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["resume_type"] = "redispatch"
        kwargs["findings_so_far"] = [
            {"severity": "warn", "description": "Budget check missed edge case"},
            {"severity": "blocker", "description": "Missing validation on P3"},
        ]
        result = build_resume(**kwargs)
        assert result.ok is True
        findings = result.data["context_snapshot"]["findings_so_far"]
        assert len(findings) == 2
        assert findings[1]["severity"] == "blocker"

    def test_blockers_survive_into_resume(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["blockers_encountered"] = ["DB connection failed", "Missing env var"]
        result = build_resume(**kwargs)
        assert result.ok is True
        blockers = result.data["context_snapshot"]["blockers_encountered"]
        assert len(blockers) == 2

    def test_key_decisions_survive_into_resume(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["key_decisions_made"] = ["Used dataclass pattern", "Skipped P7 for now"]
        result = build_resume(**kwargs)
        assert result.ok is True
        decisions = result.data["context_snapshot"]["key_decisions_made"]
        assert len(decisions) == 2

    def test_carry_forward_summary_in_resume(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["carry_forward_summary"] = "Feature 3 of 5, 1 blocker, 2 warnings"
        result = build_resume(**kwargs)
        assert result.ok is True
        assert "1 blocker" in result.data["dispatch_context"]["carry_forward_summary"]

    def test_task_spec_survives_into_resume(self) -> None:
        kwargs = _valid_resume_kwargs()
        kwargs["task_specification"] = "Implement bounded context assembly with budget enforcement."
        result = build_resume(**kwargs)
        assert result.ok is True
        assert "bounded context" in result.data["dispatch_context"]["task_specification"]
