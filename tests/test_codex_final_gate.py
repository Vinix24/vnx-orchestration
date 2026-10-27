#!/usr/bin/env python3
"""Tests for the Codex final gate enforcement."""

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from review_contract import (
    Deliverable,
    DeterministicFinding,
    QualityGate,
    ReviewContract,
    TestEvidence,
)
from codex_final_gate import enforce_codex_gate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_contract(**overrides):
    defaults = dict(
        pr_id="PR-3",
        pr_title="Codex Final Gate Prompt Renderer And Headless Enforcement",
        feature_title="Review Contracts And Gates",
        branch="feature/review-contract-gates",
        track="C",
        risk_class="high",
        merge_policy="human",
        review_stack=["gemini_review", "codex_gate", "claude_github_optional"],
        closure_stage="in_review",
        deliverables=[
            Deliverable(description="deliverable-aware Codex final gate prompt renderer", category="implementation"),
            Deliverable(description="required/optional Codex gate enforcement", category="implementation"),
            Deliverable(description="structured residual-risk receipts", category="implementation"),
        ],
        non_goals=["PR-2 deliverables are out of scope for this PR"],
        scope_files=["scripts/codex_final_gate.py"],
        changed_files=[
            "scripts/codex_final_gate.py",
            "tests/test_codex_final_gate.py",
        ],
        quality_gate=QualityGate(
            gate_id="gate_pr3_codex_final_gate_contract",
            checks=[
                "Runtime or governance PRs cannot clear without a Codex final gate when policy requires it",
                "Codex prompts include deliverables, non-goals, tests, changed files, and closure stage",
                "Final-gate receipts include findings, residual risk, and rerun requirements",
            ],
        ),
        test_evidence=TestEvidence(
            test_files=["tests/test_codex_final_gate.py"],
            test_command="pytest tests/test_codex_final_gate.py -v",
        ),
        deterministic_findings=[
            DeterministicFinding(source="ruff", severity="warning", message="unused import", file_path="scripts/codex_final_gate.py", line=5),
        ],
        dispatch_id="20260331-143522-codex-final-gate",
        content_hash="abc123def456",
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


def _make_low_risk_contract():
    return _make_contract(
        risk_class="low",
        changed_files=["docs/README.md"],
        review_stack=["gemini_review"],
    )


# ---------------------------------------------------------------------------
# Enforcement tests
# ---------------------------------------------------------------------------

class TestEnforceCodexGate:
    def test_high_risk_class_requires_gate(self):
        contract = _make_contract(risk_class="high")
        result = enforce_codex_gate(contract)
        assert result.required is True
        assert "risk_class_high" in result.reasons

    def test_low_risk_no_governance_paths_not_required(self):
        contract = _make_low_risk_contract()
        result = enforce_codex_gate(contract)
        assert result.required is False
        assert result.reasons == []

    def test_medium_risk_with_codex_in_stack_required(self):
        contract = _make_contract(
            risk_class="medium",
            review_stack=["gemini_review", "codex_gate"],
            changed_files=["docs/README.md"],
        )
        result = enforce_codex_gate(contract)
        assert result.required is True
        assert "codex_gate_in_review_stack" in result.reasons

    def test_governance_paths_trigger_requirement(self):
        contract = _make_contract(
            risk_class="low",
            review_stack=["gemini_review"],
            changed_files=["scripts/dispatcher_minimal.sh"],
        )
        result = enforce_codex_gate(contract)
        assert result.required is True
        assert result.touches_governance is True

    def test_runtime_paths_trigger_requirement(self):
        contract = _make_contract(
            risk_class="low",
            review_stack=["gemini_review"],
            changed_files=["scripts/lib/runtime_coordination.py"],
        )
        result = enforce_codex_gate(contract)
        assert result.required is True
        assert result.touches_runtime is True

    def test_sql_files_trigger_requirement(self):
        contract = _make_contract(
            risk_class="low",
            review_stack=["gemini_review"],
            changed_files=["schemas/runtime_coordination.sql"],
        )
        result = enforce_codex_gate(contract)
        assert result.required is True
        assert result.high_risk_by_path is True

    def test_enforcement_result_serializes(self):
        contract = _make_contract()
        result = enforce_codex_gate(contract)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "required" in d
        assert "reasons" in d
        assert isinstance(d["reasons"], list)

    def test_sparse_contract_does_not_crash_or_require(self):
        """A contract without pr_id, deliverables or a review stack is tolerated.

        The removed prompt renderer raised on those missing fields; enforcement
        reads none of them, so it must decide on what it has, never crash and
        never invent a requirement.
        """
        contract = _make_contract(
            pr_id="",
            risk_class="low",
            deliverables=[],
            review_stack=[],
            changed_files=[],
            scope_files=[],
            test_evidence=None,
            deterministic_findings=[],
        )
        result = enforce_codex_gate(contract)
        assert result.required is False
        assert result.reasons == []
        assert result.touches_governance is False
        assert result.touches_runtime is False
