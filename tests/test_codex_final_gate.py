#!/usr/bin/env python3
"""Tests for Codex final gate prompt renderer, enforcement, and receipts."""

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
from codex_final_gate import (
    CodexFinalGateReceipt,
    CodexGateEnforcementResult,
    check_gate_clearance,
    enforce_codex_gate,
    evaluate_and_record,
    render_codex_prompt,
    main,
)


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
            changed_files=["scripts/dispatcher_v8_minimal.sh"],
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


# ---------------------------------------------------------------------------
# Prompt renderer tests
# ---------------------------------------------------------------------------

class TestRenderCodexPrompt:
    def test_prompt_includes_header_fields(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "PR-3" in prompt
        assert "Codex Final Gate Prompt Renderer And Headless Enforcement" in prompt
        assert "Review Contracts And Gates" in prompt
        assert "feature/review-contract-gates" in prompt
        assert "high" in prompt
        assert "human" in prompt
        assert "in_review" in prompt

    def test_prompt_includes_deliverables(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Deliverables" in prompt
        assert "deliverable-aware Codex final gate prompt renderer" in prompt
        assert "required/optional Codex gate enforcement" in prompt
        assert "structured residual-risk receipts" in prompt

    def test_prompt_includes_non_goals(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Non-Goals" in prompt
        assert "PR-2 deliverables are out of scope" in prompt

    def test_prompt_includes_changed_files(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Changed Files" in prompt
        assert "`scripts/codex_final_gate.py`" in prompt
        assert "`tests/test_codex_final_gate.py`" in prompt

    def test_prompt_includes_test_evidence(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Test Evidence" in prompt
        assert "`tests/test_codex_final_gate.py`" in prompt
        assert "`pytest tests/test_codex_final_gate.py -v`" in prompt

    def test_prompt_includes_deterministic_findings(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Deterministic Findings" in prompt
        assert "[warning]" in prompt
        assert "ruff" in prompt
        assert "unused import" in prompt

    def test_prompt_includes_quality_gate(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "gate_pr3_codex_final_gate_contract" in prompt
        assert "- [ ]" in prompt

    def test_prompt_includes_closure_stage(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "Closure Stage" in prompt
        assert "in_review" in prompt

    def test_prompt_includes_review_instructions(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "## Review Instructions" in prompt
        assert "Deliverable completeness" in prompt
        assert "Residual risk" in prompt

    def test_prompt_includes_dispatch_id(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "20260331-143522-codex-final-gate" in prompt

    def test_prompt_includes_content_hash(self):
        contract = _make_contract()
        prompt = render_codex_prompt(contract)
        assert "abc123def456" in prompt

    def test_prompt_includes_dependencies(self):
        contract = _make_contract(dependencies=["PR-1"])
        prompt = render_codex_prompt(contract)
        assert "PR-1" in prompt

    def test_missing_pr_id_raises(self):
        contract = _make_contract(pr_id="")
        with pytest.raises(ValueError, match="pr_id"):
            render_codex_prompt(contract)

    def test_missing_deliverables_raises(self):
        contract = _make_contract(deliverables=[])
        with pytest.raises(ValueError, match="deliverables"):
            render_codex_prompt(contract)

    def test_missing_review_stack_raises(self):
        contract = _make_contract(review_stack=[])
        with pytest.raises(ValueError, match="review_stack"):
            render_codex_prompt(contract)

    def test_no_test_evidence_omits_section(self):
        contract = _make_contract(test_evidence=None)
        prompt = render_codex_prompt(contract)
        assert "## Test Evidence" not in prompt

    def test_no_findings_omits_section(self):
        contract = _make_contract(deterministic_findings=[])
        prompt = render_codex_prompt(contract)
        assert "## Deterministic Findings" not in prompt

    def test_no_non_goals_omits_section(self):
        contract = _make_contract(non_goals=[])
        prompt = render_codex_prompt(contract)
        assert "## Non-Goals" not in prompt


# ---------------------------------------------------------------------------
# Receipt tests
# ---------------------------------------------------------------------------

class TestCodexFinalGateReceipt:
    def test_receipt_roundtrip(self):
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            required=True,
            enforcement_reasons=["risk_class_high"],
            findings=[{"severity": "info", "message": "all clear"}],
            residual_risk="monitoring not yet proven in prod",
            rerun_required=False,
            rerun_reason=None,
            content_hash="abc123",
            prompt_rendered=True,
            recorded_at="2026-03-31T14:00:00Z",
        )
        d = receipt.to_dict()
        restored = CodexFinalGateReceipt.from_dict(d)
        assert restored == receipt

    def test_receipt_json_roundtrip(self):
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="fail",
            findings=[{"severity": "error", "message": "missing test"}],
            residual_risk="untested code path",
            rerun_required=True,
            rerun_reason="missing_coverage",
        )
        text = receipt.to_json()
        restored = CodexFinalGateReceipt.from_json(text)
        assert restored == receipt

    def test_receipt_default_values(self):
        receipt = CodexFinalGateReceipt(pr_id="PR-1")
        assert receipt.verdict == "pending"
        assert receipt.required is False
        assert receipt.findings == []
        assert receipt.residual_risk is None
        assert receipt.rerun_required is False

    def test_from_dict_missing_fields_uses_defaults(self):
        receipt = CodexFinalGateReceipt.from_dict({"pr_id": "PR-5"})
        assert receipt.pr_id == "PR-5"
        assert receipt.verdict == "pending"
        assert receipt.gate == "codex_final_gate"


# ---------------------------------------------------------------------------
# Gate clearance tests
# ---------------------------------------------------------------------------

class TestCheckGateClearance:
    def test_not_required_clears(self):
        contract = _make_low_risk_contract()
        result = check_gate_clearance(contract, None)
        assert result["cleared"] is True
        assert result["reason"] == "codex_gate_not_required"

    def test_required_no_receipt_blocks(self):
        contract = _make_contract()
        result = check_gate_clearance(contract, None)
        assert result["cleared"] is False
        assert "missing_codex_gate_receipt" in result["blockers"]

    def test_required_pass_verdict_clears(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            required=True,
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is True
        assert result["reason"] == "codex_gate_passed"

    def test_required_fail_verdict_blocks(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="fail",
            required=True,
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert "codex_gate_failed" in result["blockers"]

    def test_required_blocked_verdict_blocks(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="blocked",
            required=True,
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert "codex_gate_blocked" in result["blockers"]

    def test_required_pending_verdict_blocks(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pending",
            required=True,
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert "codex_gate_pending" in result["blockers"]

    def test_rerun_required_blocks(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            required=True,
            rerun_required=True,
            rerun_reason="contract_changed",
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert "codex_gate_rerun_required" in result["blockers"]

    def test_stale_content_hash_blocks(self):
        contract = _make_contract(content_hash="current_hash")
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            required=True,
            content_hash="old_hash",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert "codex_gate_stale_receipt" in result["blockers"]

    def test_unresolved_errors_block(self):
        contract = _make_contract()
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            required=True,
            findings=[{"severity": "error", "message": "critical bug"}],
            content_hash="abc123def456",
        )
        result = check_gate_clearance(contract, receipt)
        assert result["cleared"] is False
        assert any("unresolved_errors" in b for b in result["blockers"])


# ---------------------------------------------------------------------------
# evaluate_and_record tests
# ---------------------------------------------------------------------------

class TestEvaluateAndRecord:
    def test_pending_without_verdict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / ".vnx-data" / "state"))
        (tmp_path / ".vnx-data" / "state").mkdir(parents=True)
        (tmp_path / ".vnx-data" / "receipts").mkdir(parents=True)
        receipts_file = tmp_path / ".vnx-data" / "receipts" / "receipts.ndjson"
        receipts_file.touch()
        monkeypatch.setenv("VNX_RECEIPTS_FILE", str(receipts_file))

        contract = _make_contract()
        out = tmp_path / "receipt.json"
        receipt = evaluate_and_record(contract, output_path=out)
        assert receipt.verdict == "pending"
        assert receipt.required is True
        assert receipt.prompt_rendered is True
        assert out.exists()

    def test_with_pass_verdict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / ".vnx-data" / "state"))
        (tmp_path / ".vnx-data" / "state").mkdir(parents=True)
        (tmp_path / ".vnx-data" / "receipts").mkdir(parents=True)
        receipts_file = tmp_path / ".vnx-data" / "receipts" / "receipts.ndjson"
        receipts_file.touch()
        monkeypatch.setenv("VNX_RECEIPTS_FILE", str(receipts_file))

        contract = _make_contract()
        verdict = {
            "verdict": "pass",
            "findings": [{"severity": "info", "message": "all checks pass"}],
            "residual_risk": "monitoring not proven",
            "rerun_required": False,
            "rerun_reason": None,
        }
        receipt = evaluate_and_record(contract, codex_verdict=verdict)
        assert receipt.verdict == "pass"
        assert receipt.residual_risk == "monitoring not proven"
        assert len(receipt.findings) == 1

    def test_blocked_when_contract_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / ".vnx-data" / "state"))
        (tmp_path / ".vnx-data" / "state").mkdir(parents=True)
        (tmp_path / ".vnx-data" / "receipts").mkdir(parents=True)
        receipts_file = tmp_path / ".vnx-data" / "receipts" / "receipts.ndjson"
        receipts_file.touch()
        monkeypatch.setenv("VNX_RECEIPTS_FILE", str(receipts_file))

        contract = _make_contract(
            pr_id="",
            deliverables=[],
            changed_files=["scripts/dispatcher_v8_minimal.sh"],
        )
        receipt = evaluate_and_record(contract)
        assert receipt.verdict == "blocked"
        assert receipt.rerun_required is True
        assert receipt.rerun_reason == "contract_incomplete"


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestCLI:
    def _write_contract(self, tmp_path):
        contract = _make_contract()
        path = tmp_path / "contract.json"
        path.write_text(contract.to_json(), encoding="utf-8")
        return path

    def test_render_prompt_stdout(self, tmp_path):
        contract_path = self._write_contract(tmp_path)
        exit_code = main(["render-prompt", "--contract", str(contract_path)])
        assert exit_code == 0

    def test_render_prompt_to_file(self, tmp_path):
        contract_path = self._write_contract(tmp_path)
        out = tmp_path / "prompt.md"
        exit_code = main(["render-prompt", "--contract", str(contract_path), "--output", str(out)])
        assert exit_code == 0
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "PR-3" in content

    def test_render_prompt_missing_contract(self, tmp_path):
        exit_code = main(["render-prompt", "--contract", str(tmp_path / "missing.json")])
        assert exit_code == 20  # EXIT_IO

    def test_enforce_command(self, tmp_path):
        contract_path = self._write_contract(tmp_path)
        exit_code = main(["enforce", "--contract", str(contract_path)])
        assert exit_code == 0

    def test_check_clearance_no_receipt(self, tmp_path):
        contract_path = self._write_contract(tmp_path)
        exit_code = main(["check-clearance", "--contract", str(contract_path)])
        assert exit_code == 0

    def test_check_clearance_with_receipt(self, tmp_path):
        contract_path = self._write_contract(tmp_path)
        receipt = CodexFinalGateReceipt(
            pr_id="PR-3",
            verdict="pass",
            content_hash="abc123def456",
        )
        receipt_path = tmp_path / "receipt.json"
        receipt_path.write_text(receipt.to_json(), encoding="utf-8")
        exit_code = main(["check-clearance", "--contract", str(contract_path), "--receipt", str(receipt_path)])
        assert exit_code == 0
