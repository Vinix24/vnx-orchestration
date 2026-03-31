#!/usr/bin/env python3
"""Tests for gemini_prompt_renderer: prompt rendering completeness and receipt classification.

Coverage targets (gate_pr2_gemini_contract_review):
- Gemini prompts include deliverables, non-goals, changed files, and declared tests
- Advisory vs blocking findings are emitted distinctly in receipts
- Missing contract fields fail explicitly (MissingContractFieldError)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
from gemini_prompt_renderer import (
    GeminiReviewFinding,
    GeminiReviewReceipt,
    MissingContractFieldError,
    REQUIRED_CONTRACT_FIELDS,
    render_gemini_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_minimal_contract(**overrides) -> ReviewContract:
    """Return the smallest valid ReviewContract for rendering."""
    defaults = dict(
        pr_id="PR-2",
        pr_title="Gemini Review Prompt Renderer And Receipt Contract",
        feature_title="Review Contracts And Gates",
        branch="feature/review-contract-gates-and-idempotency",
        track="B",
        risk_class="medium",
        merge_policy="human",
        review_stack=["gemini_review", "codex_gate", "claude_github_optional"],
        closure_stage="open",
        deliverables=[
            Deliverable(
                description="Gemini review prompts include deliverables, non-goals, changed files, and declared tests",
                category="implementation",
            ),
            Deliverable(
                description="emitted receipts clearly distinguish advisory and blocking findings",
                category="implementation",
            ),
            Deliverable(
                description="missing contract fields fail explicitly instead of silently degrading the prompt",
                category="implementation",
            ),
        ],
        non_goals=[
            "PR-0 deliverables are out of scope for this PR",
            "PR-1 deliverables are out of scope for this PR",
            "PR-3 deliverables are out of scope for this PR",
        ],
        changed_files=[
            "scripts/lib/gemini_prompt_renderer.py",
            "scripts/review_gate_manager.py",
            "tests/test_gemini_prompt_renderer.py",
        ],
    )
    defaults.update(overrides)
    return ReviewContract(**defaults)


# ---------------------------------------------------------------------------
# MissingContractFieldError
# ---------------------------------------------------------------------------

class TestMissingContractFieldError:
    def test_error_carries_field_name(self):
        err = MissingContractFieldError("pr_id")
        assert err.field_name == "pr_id"
        assert "pr_id" in str(err)

    def test_inherits_from_value_error(self):
        assert issubclass(MissingContractFieldError, ValueError)


# ---------------------------------------------------------------------------
# render_gemini_prompt — missing required fields
# ---------------------------------------------------------------------------

class TestRenderGeminiPromptMissingFields:
    def test_missing_pr_id_raises(self):
        contract = _make_minimal_contract(pr_id="")
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "pr_id"

    def test_missing_pr_title_raises(self):
        contract = _make_minimal_contract(pr_title="")
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "pr_title"

    def test_missing_deliverables_raises(self):
        contract = _make_minimal_contract(deliverables=[])
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "deliverables"

    def test_missing_review_stack_raises(self):
        contract = _make_minimal_contract(review_stack=[])
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "review_stack"

    def test_missing_risk_class_raises(self):
        contract = _make_minimal_contract(risk_class="")
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "risk_class"

    def test_missing_merge_policy_raises(self):
        contract = _make_minimal_contract(merge_policy="")
        with pytest.raises(MissingContractFieldError) as exc_info:
            render_gemini_prompt(contract)
        assert exc_info.value.field_name == "merge_policy"

    def test_all_required_fields_covered_by_validation(self):
        """Ensure REQUIRED_CONTRACT_FIELDS matches what validation actually checks."""
        assert set(REQUIRED_CONTRACT_FIELDS) == {
            "pr_id", "pr_title", "deliverables", "review_stack", "risk_class", "merge_policy"
        }


# ---------------------------------------------------------------------------
# render_gemini_prompt — content completeness
# ---------------------------------------------------------------------------

class TestRenderGeminiPromptCompleteness:
    def test_prompt_includes_pr_id_and_title(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "PR-2" in prompt
        assert "Gemini Review Prompt Renderer And Receipt Contract" in prompt

    def test_prompt_includes_deliverables_section(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "## Deliverables" in prompt
        assert "Gemini review prompts include deliverables" in prompt

    def test_prompt_includes_all_deliverable_descriptions(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        for d in contract.deliverables:
            assert d.description in prompt

    def test_prompt_includes_deliverable_categories(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        # All deliverables in _make_minimal_contract have category "implementation"
        assert "[implementation]" in prompt

    def test_prompt_includes_non_goals(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "## Non-Goals" in prompt
        for ng in contract.non_goals:
            assert ng in prompt

    def test_prompt_includes_changed_files(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "## Changed Files" in prompt
        for f in contract.changed_files:
            assert f in prompt

    def test_prompt_uses_scope_files_when_no_changed_files(self):
        contract = _make_minimal_contract(
            changed_files=[],
            scope_files=["scripts/lib/gemini_prompt_renderer.py"],
        )
        prompt = render_gemini_prompt(contract)
        assert "Expected Scope Files" in prompt
        assert "scripts/lib/gemini_prompt_renderer.py" in prompt

    def test_prompt_includes_quality_gate_checks(self):
        gate = QualityGate(
            gate_id="gate_pr2_gemini_contract_review",
            checks=[
                "Gemini prompts include deliverables, non-goals, changed files, and declared tests",
                "Advisory vs blocking findings are emitted distinctly in review receipts",
                "Missing review-contract fields fail explicitly",
            ],
        )
        contract = _make_minimal_contract(quality_gate=gate)
        prompt = render_gemini_prompt(contract)
        assert "## Quality Gate" in prompt
        assert "gate_pr2_gemini_contract_review" in prompt
        for check in gate.checks:
            assert check in prompt

    def test_prompt_includes_declared_tests(self):
        evidence = TestEvidence(
            test_files=["tests/test_gemini_prompt_renderer.py"],
            test_command="pytest tests/test_gemini_prompt_renderer.py -v",
            expected_assertions=30,
        )
        contract = _make_minimal_contract(test_evidence=evidence)
        prompt = render_gemini_prompt(contract)
        assert "## Declared Test Evidence" in prompt
        assert "tests/test_gemini_prompt_renderer.py" in prompt
        assert "pytest tests/test_gemini_prompt_renderer.py -v" in prompt
        assert "30" in prompt

    def test_prompt_includes_deterministic_findings(self):
        findings = [
            DeterministicFinding(
                source="ruff",
                severity="warning",
                message="Unused import `os`",
                file_path="scripts/lib/gemini_prompt_renderer.py",
                line=5,
            ),
        ]
        contract = _make_minimal_contract(deterministic_findings=findings)
        prompt = render_gemini_prompt(contract)
        assert "## Pre-computed Deterministic Findings" in prompt
        assert "ruff" in prompt
        assert "Unused import" in prompt

    def test_prompt_includes_review_instructions_and_response_schema(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "## Review Instructions" in prompt
        assert "blocking" in prompt
        assert "advisory" in prompt
        assert '"severity"' in prompt
        assert '"findings"' in prompt

    def test_prompt_includes_risk_class_and_merge_policy(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "medium" in prompt
        assert "human" in prompt

    def test_prompt_includes_branch(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert "feature/review-contract-gates-and-idempotency" in prompt

    def test_prompt_includes_content_hash_when_present(self):
        from review_contract import materialize_review_contract
        feature_plan = _sample_feature_plan()
        pr_queue = _sample_pr_queue()
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=feature_plan,
            pr_queue_content=pr_queue,
            branch="feature/test",
            changed_files=["scripts/lib/review_contract.py"],
        )
        prompt = render_gemini_prompt(contract)
        assert contract.content_hash in prompt

    def test_prompt_omits_non_goals_section_when_empty(self):
        contract = _make_minimal_contract(non_goals=[])
        prompt = render_gemini_prompt(contract)
        assert "Non-Goals" not in prompt

    def test_prompt_omits_changed_files_section_when_empty_and_no_scope(self):
        contract = _make_minimal_contract(changed_files=[], scope_files=[])
        prompt = render_gemini_prompt(contract)
        assert "## Changed Files" not in prompt
        assert "## Expected Scope Files" not in prompt

    def test_prompt_omits_test_evidence_section_when_none(self):
        contract = _make_minimal_contract(test_evidence=None)
        prompt = render_gemini_prompt(contract)
        assert "Declared Test Evidence" not in prompt

    def test_prompt_omits_deterministic_findings_when_empty(self):
        contract = _make_minimal_contract(deterministic_findings=[])
        prompt = render_gemini_prompt(contract)
        assert "Pre-computed Deterministic Findings" not in prompt

    def test_valid_contract_produces_non_empty_prompt(self):
        contract = _make_minimal_contract()
        prompt = render_gemini_prompt(contract)
        assert len(prompt) > 200


# ---------------------------------------------------------------------------
# GeminiReviewFinding
# ---------------------------------------------------------------------------

class TestGeminiReviewFinding:
    def test_is_blocking(self):
        f = GeminiReviewFinding(severity="blocking", category="correctness", message="bug")
        assert f.is_blocking()
        assert not f.is_advisory()

    def test_is_advisory(self):
        f = GeminiReviewFinding(severity="advisory", category="style", message="nit")
        assert f.is_advisory()
        assert not f.is_blocking()

    def test_to_dict(self):
        f = GeminiReviewFinding(
            severity="blocking", category="security", message="SQL injection", file_path="app.py", line=42
        )
        d = f.to_dict()
        assert d["severity"] == "blocking"
        assert d["category"] == "security"
        assert d["message"] == "SQL injection"
        assert d["file_path"] == "app.py"
        assert d["line"] == 42


# ---------------------------------------------------------------------------
# GeminiReviewReceipt.from_raw_findings — advisory vs blocking classification
# ---------------------------------------------------------------------------

class TestGeminiReviewReceiptFromRawFindings:
    def test_empty_findings_produces_pass(self):
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=[])
        assert receipt.status == "pass"
        assert receipt.advisory_count == 0
        assert receipt.blocking_count == 0
        assert receipt.summary == "LGTM — no findings"

    def test_blocking_severity_classified_as_blocking(self):
        raw = [{"severity": "blocking", "category": "correctness", "message": "bug"}]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 1
        assert receipt.advisory_count == 0
        assert receipt.status == "fail"

    def test_error_severity_treated_as_blocking(self):
        raw = [{"severity": "error", "category": "security", "message": "XSS"}]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 1
        assert receipt.status == "fail"

    def test_advisory_severity_classified_as_advisory(self):
        raw = [{"severity": "advisory", "category": "style", "message": "nit"}]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.advisory_count == 1
        assert receipt.blocking_count == 0
        assert receipt.status == "pass"

    def test_warning_and_info_classified_as_advisory(self):
        raw = [
            {"severity": "warning", "category": "style", "message": "nit1"},
            {"severity": "info", "category": "coverage", "message": "low cov"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.advisory_count == 2
        assert receipt.blocking_count == 0

    def test_mixed_findings_separated_correctly(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "crash"},
            {"severity": "advisory", "category": "style", "message": "nit"},
            {"severity": "error", "category": "security", "message": "vuln"},
            {"severity": "warning", "category": "coverage", "message": "low"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.blocking_count == 2
        assert receipt.advisory_count == 2
        assert receipt.status == "fail"

    def test_advisory_only_findings_produce_pass_status(self):
        raw = [
            {"severity": "advisory", "category": "style", "message": "nit1"},
            {"severity": "warning", "category": "style", "message": "nit2"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert receipt.status == "pass"

    def test_contract_hash_preserved_in_receipt(self):
        receipt = GeminiReviewReceipt.from_raw_findings(
            pr_id="PR-2", raw_findings=[], contract_hash="abc123"
        )
        assert receipt.contract_hash == "abc123"

    def test_summary_includes_counts(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "A"},
            {"severity": "advisory", "category": "style", "message": "B"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        assert "1 blocking" in receipt.summary
        assert "1 advisory" in receipt.summary


# ---------------------------------------------------------------------------
# GeminiReviewReceipt.to_dict — structure for downstream consumers
# ---------------------------------------------------------------------------

class TestGeminiReviewReceiptToDict:
    def test_to_dict_always_has_advisory_and_blocking_lists(self):
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=[])
        d = receipt.to_dict()
        assert "advisory_findings" in d
        assert "blocking_findings" in d
        assert isinstance(d["advisory_findings"], list)
        assert isinstance(d["blocking_findings"], list)

    def test_to_dict_has_counts(self):
        raw = [{"severity": "blocking", "category": "correctness", "message": "oops"}]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        d = receipt.to_dict()
        assert d["blocking_count"] == 1
        assert d["advisory_count"] == 0

    def test_to_dict_is_json_serializable(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "crash"},
            {"severity": "advisory", "category": "style", "message": "nit"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        serialized = json.dumps(receipt.to_dict())
        parsed = json.loads(serialized)
        assert parsed["blocking_count"] == 1
        assert parsed["advisory_count"] == 1

    def test_blocking_findings_not_in_advisory_list(self):
        raw = [
            {"severity": "blocking", "category": "correctness", "message": "hard fail"},
            {"severity": "advisory", "category": "style", "message": "soft nit"},
        ]
        receipt = GeminiReviewReceipt.from_raw_findings(pr_id="PR-2", raw_findings=raw)
        d = receipt.to_dict()
        advisory_msgs = [f["message"] for f in d["advisory_findings"]]
        blocking_msgs = [f["message"] for f in d["blocking_findings"]]
        assert "hard fail" not in advisory_msgs
        assert "hard fail" in blocking_msgs
        assert "soft nit" not in blocking_msgs
        assert "soft nit" in advisory_msgs


# ---------------------------------------------------------------------------
# Integration: ReviewGateManager.record_result now emits advisory/blocking fields
# ---------------------------------------------------------------------------

class TestRecordResultAdvisoryBlockingIntegration:
    @pytest.fixture
    def manager(self, tmp_path, monkeypatch):
        import review_gate_manager as rgm

        data_dir = tmp_path / ".vnx-data"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)

        return rgm.ReviewGateManager()

    def test_record_result_emits_advisory_and_blocking_fields(self, manager):
        result = manager.record_result(
            gate="gemini_review",
            pr_number=2,
            branch="feature/test",
            status="fail",
            summary="1 blocking, 1 advisory",
            findings=[
                {"severity": "blocking", "category": "correctness", "message": "crash"},
                {"severity": "advisory", "category": "style", "message": "nit"},
            ],
            pr_id="PR-2",
        )
        assert "advisory_findings" in result
        assert "blocking_findings" in result
        assert result["blocking_count"] == 1
        assert result["advisory_count"] == 1

    def test_record_result_persists_with_advisory_blocking_fields(self, manager, tmp_path):
        manager.record_result(
            gate="gemini_review",
            pr_number=3,
            branch="feature/test",
            status="pass",
            summary="no blockers",
            findings=[{"severity": "advisory", "category": "style", "message": "nit"}],
            pr_id="PR-3",
        )
        saved = json.loads(
            (manager.results_dir / "pr-3-gemini_review.json").read_text(encoding="utf-8")
        )
        assert "advisory_findings" in saved
        assert "blocking_findings" in saved
        assert saved["advisory_count"] == 1
        assert saved["blocking_count"] == 0

    def test_request_gemini_with_contract_persists_prompt(self, manager, monkeypatch):
        import review_gate_manager as rgm
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

        contract = _make_minimal_contract()
        payload = manager.request_gemini_with_contract(contract=contract, mode="per_pr")

        assert payload["status"] == "queued"
        assert "prompt" in payload
        assert len(payload["prompt"]) > 100
        # The prompt file should be persisted to disk
        request_files = list(manager.requests_dir.glob("pr2-gemini_review-contract.json"))
        assert len(request_files) == 1
        saved = json.loads(request_files[0].read_text(encoding="utf-8"))
        assert "prompt" in saved
        assert "PR-2" in saved["pr_id"]

    def test_request_gemini_with_contract_blocked_when_gemini_unavailable(self, manager, monkeypatch):
        import review_gate_manager as rgm
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: None)
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")

        contract = _make_minimal_contract()
        payload = manager.request_gemini_with_contract(contract=contract)
        assert payload["status"] == "blocked"
        assert payload["reason"] == "gemini_not_available"

    def test_request_gemini_with_contract_raises_on_missing_field(self, manager):
        bad_contract = _make_minimal_contract(pr_id="")
        with pytest.raises(MissingContractFieldError):
            manager.request_gemini_with_contract(contract=bad_contract)


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------

def _sample_feature_plan() -> str:
    return """\
# Feature: Review Contracts And Gates

**Status**: Draft
**Priority**: P0
**Branch**: `feature/review-contract-gates`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

## PR-1: Review Contract Schema And Materializer
**Track**: C
**Priority**: P0
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-4 hours
**Dependencies**: []

### Description
Define the canonical review contract schema.

### Scope
- review contract schema in `scripts/lib/review_contract.py`

### Success Criteria
- each PR can produce one structured review contract
- contract generation is deterministic for the same inputs

### Quality Gate
`gate_pr1_review_contract_schema`:
- [ ] Review contract schema covers deliverables
- [ ] Contract generation is deterministic
"""


def _sample_pr_queue() -> str:
    return """\
# PR Queue

## Status

### ⏳ Queued PRs
- PR-1: Review Contract Schema (dependencies: none) [risk=medium]
"""
