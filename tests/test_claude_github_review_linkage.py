#!/usr/bin/env python3
"""Tests for Claude GitHub review bridge and evidence linkage (PR-4).

Quality gate: gate_pr4_claude_review_linkage
- Claude GitHub review request state is linked to the same review contract as Gemini and Codex
- Optional review states are explicit and auditable
- Review evidence linkage tests pass
"""

import json
import sys
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from claude_github_receipt import (
    ClaudeGitHubReviewFinding,
    ClaudeGitHubReviewReceipt,
    EVIDENCE_STATES,
    INTENTIONALLY_ABSENT_STATES,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_NOT_CONFIGURED,
    STATE_REQUESTED,
    VALID_STATES,
)
from review_contract import (
    Deliverable,
    QualityGate,
    ReviewContract,
)
import review_gate_manager as rgm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def review_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


@pytest.fixture
def sample_contract():
    return ReviewContract(
        pr_id="PR-4",
        pr_title="Claude GitHub Review Bridge And Evidence Linkage",
        feature_title="Review Contracts, Acceptance Idempotency, And Auto-Next Trials",
        branch="feature/review-contract-gates-and-idempotency",
        track="B",
        risk_class="medium",
        merge_policy="human",
        review_stack=["gemini_review", "codex_gate", "claude_github_optional"],
        deliverables=[
            Deliverable(description="Claude GitHub review requests are linked to the same review contract", category="implementation"),
            Deliverable(description="Optional review states are explicit and auditable", category="implementation"),
        ],
        quality_gate=QualityGate(
            gate_id="gate_pr4_claude_review_linkage",
            checks=[
                "Claude GitHub review request state is linked to the same review contract as Gemini and Codex",
                "Optional review states are explicit and auditable",
                "Review evidence linkage tests pass",
            ],
        ),
        changed_files=["scripts/lib/claude_github_receipt.py", "scripts/review_gate_manager.py"],
        content_hash="abc123def456",
    )


# ---------------------------------------------------------------------------
# ClaudeGitHubReviewReceipt unit tests
# ---------------------------------------------------------------------------

class TestClaudeGitHubReviewReceiptStates:
    def test_valid_states_are_complete(self):
        assert STATE_NOT_CONFIGURED in VALID_STATES
        assert STATE_CONFIGURED_DRY_RUN in VALID_STATES
        assert STATE_REQUESTED in VALID_STATES
        assert STATE_BLOCKED in VALID_STATES
        assert STATE_COMPLETED in VALID_STATES

    def test_not_configured_is_intentionally_absent(self):
        receipt = ClaudeGitHubReviewReceipt(pr_id="PR-4", state=STATE_NOT_CONFIGURED)
        assert receipt.was_intentionally_absent() is True
        assert receipt.contributed_evidence() is False

    def test_configured_dry_run_is_intentionally_absent(self):
        receipt = ClaudeGitHubReviewReceipt(pr_id="PR-4", state=STATE_CONFIGURED_DRY_RUN)
        assert receipt.was_intentionally_absent() is True
        assert receipt.contributed_evidence() is False

    def test_requested_contributes_evidence(self):
        receipt = ClaudeGitHubReviewReceipt(pr_id="PR-4", state=STATE_REQUESTED)
        assert receipt.contributed_evidence() is True
        assert receipt.was_intentionally_absent() is False

    def test_completed_contributes_evidence(self):
        receipt = ClaudeGitHubReviewReceipt(pr_id="PR-4", state=STATE_COMPLETED)
        assert receipt.contributed_evidence() is True
        assert receipt.was_intentionally_absent() is False

    def test_blocked_neither_evidence_nor_absent(self):
        receipt = ClaudeGitHubReviewReceipt(pr_id="PR-4", state=STATE_BLOCKED)
        assert receipt.contributed_evidence() is False
        assert receipt.was_intentionally_absent() is False

    def test_to_dict_includes_auditable_state_fields(self):
        receipt = ClaudeGitHubReviewReceipt(
            pr_id="PR-4",
            state=STATE_NOT_CONFIGURED,
            contract_hash="abc123",
            branch="feature/test",
        )
        d = receipt.to_dict()
        assert d["state"] == STATE_NOT_CONFIGURED
        assert d["contract_hash"] == "abc123"
        assert d["contributed_evidence"] is False
        assert d["was_intentionally_absent"] is True
        assert "advisory_findings" in d
        assert "blocking_findings" in d
        assert "advisory_count" in d
        assert "blocking_count" in d

    def test_to_json_roundtrip(self):
        receipt = ClaudeGitHubReviewReceipt(
            pr_id="PR-4",
            state=STATE_REQUESTED,
            contract_hash="xyz789",
            branch="feature/demo",
            gh_comment_body="@claude review",
        )
        parsed = json.loads(receipt.to_json())
        assert parsed["pr_id"] == "PR-4"
        assert parsed["state"] == STATE_REQUESTED
        assert parsed["contract_hash"] == "xyz789"


class TestClaudeGitHubReviewFinding:
    def test_blocking_finding(self):
        f = ClaudeGitHubReviewFinding(severity="blocking", category="correctness", message="Bug found")
        assert f.is_blocking() is True
        assert f.is_advisory() is False

    def test_advisory_finding(self):
        f = ClaudeGitHubReviewFinding(severity="advisory", category="style", message="Minor style issue")
        assert f.is_advisory() is True
        assert f.is_blocking() is False

    def test_to_dict_shape(self):
        f = ClaudeGitHubReviewFinding(
            severity="advisory", category="coverage", message="Missing test",
            file_path="scripts/foo.py", line=42
        )
        d = f.to_dict()
        assert d == {
            "severity": "advisory",
            "category": "coverage",
            "message": "Missing test",
            "file_path": "scripts/foo.py",
            "line": 42,
        }


class TestFromResultPayload:
    def test_classifies_blocking_and_advisory(self):
        payload = {
            "pr_id": "PR-4",
            "branch": "feature/test",
            "status": "fail",
            "summary": "Issues found",
            "contract_hash": "abc123",
            "completed_at": "2026-03-31T12:00:00Z",
            "requested_at": "",
            "findings": [
                {"severity": "blocking", "category": "correctness", "message": "Bug"},
                {"severity": "error", "category": "security", "message": "XSS"},
                {"severity": "advisory", "category": "style", "message": "Nitpick"},
                {"severity": "info", "category": "style", "message": "Note"},
            ],
        }
        receipt = ClaudeGitHubReviewReceipt.from_result_payload(payload)
        assert receipt.state == STATE_COMPLETED
        assert receipt.blocking_count == 2
        assert receipt.advisory_count == 2
        assert receipt.contributed_evidence() is True
        assert receipt.contract_hash == "abc123"

    def test_classifies_clean_result(self):
        payload = {
            "pr_id": "PR-4",
            "status": "pass",
            "summary": "LGTM",
            "contract_hash": "def456",
            "findings": [],
        }
        receipt = ClaudeGitHubReviewReceipt.from_result_payload(payload)
        assert receipt.state == STATE_COMPLETED
        assert receipt.blocking_count == 0
        assert receipt.advisory_count == 0
        assert receipt.result_status == "pass"


class TestFromRequestPayload:
    def test_reconstructs_from_not_configured(self):
        payload = {
            "pr_id": "PR-4",
            "gate": "claude_github_optional",
            "state": STATE_NOT_CONFIGURED,
            "contract_hash": "abc123",
            "branch": "feature/test",
            "reason": "claude_github_not_configured",
            "requested_at": "2026-03-31T10:00:00Z",
        }
        receipt = ClaudeGitHubReviewReceipt.from_request_payload(payload)
        assert receipt.state == STATE_NOT_CONFIGURED
        assert receipt.contract_hash == "abc123"
        assert receipt.was_intentionally_absent() is True

    def test_reconstructs_from_requested(self):
        payload = {
            "pr_id": "PR-4",
            "state": STATE_REQUESTED,
            "contract_hash": "xyz789",
            "branch": "feature/demo",
            "gh_comment_body": "@claude review",
            "requested_at": "2026-03-31T11:00:00Z",
        }
        receipt = ClaudeGitHubReviewReceipt.from_request_payload(payload)
        assert receipt.state == STATE_REQUESTED
        assert receipt.contributed_evidence() is True


# ---------------------------------------------------------------------------
# Integration tests: request_claude_github_with_contract
# ---------------------------------------------------------------------------

class TestRequestClaudeGitHubWithContract:
    def test_not_configured_when_env_disabled(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        assert receipt.state == STATE_NOT_CONFIGURED
        assert receipt.contract_hash == sample_contract.content_hash
        assert receipt.pr_id == "PR-4"
        assert receipt.was_intentionally_absent() is True
        assert receipt.contributed_evidence() is False

    def test_not_configured_when_gh_missing(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda _: None)

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        assert receipt.state == STATE_NOT_CONFIGURED
        assert receipt.was_intentionally_absent() is True

    def test_configured_dry_run_when_trigger_not_set(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/gh" if tool == "gh" else None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0")

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        assert receipt.state == STATE_CONFIGURED_DRY_RUN
        assert receipt.was_intentionally_absent() is True
        assert receipt.contributed_evidence() is False
        assert receipt.contract_hash == sample_contract.content_hash

    def test_requested_when_trigger_set_and_gh_succeeds(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/gh" if tool == "gh" else None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "1")
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        captured: Dict[str, list] = {"argv": []}

        def _capture(cmd, *a, **kw):
            captured["argv"] = list(cmd)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            return mock_proc

        monkeypatch.setattr(rgm.subprocess, "run", _capture)

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(
            contract=sample_contract, pr_number=42,
        )

        assert receipt.state == STATE_REQUESTED
        assert receipt.contributed_evidence() is True
        assert receipt.gh_comment_body == "@claude review"
        assert receipt.contract_hash == sample_contract.content_hash
        # gh pr comment must target the real GitHub PR number, not the
        # governance pr_id (e.g. "PR-4") which is not a valid PR ref.
        assert "42" in captured["argv"]
        assert "PR-4" not in captured["argv"]

    def test_blocked_when_trigger_set_but_gh_fails(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/gh" if tool == "gh" else None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "1")

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "gh: authentication required"
        monkeypatch.setattr(rgm.subprocess, "run", lambda *a, **kw: mock_proc)

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(
            contract=sample_contract, pr_number=42,
        )

        assert receipt.state == STATE_BLOCKED
        assert receipt.contributed_evidence() is False
        assert receipt.reason == "claude_github_trigger_failed"

    def test_blocked_when_trigger_set_but_pr_number_missing(
        self, review_env, monkeypatch, sample_contract
    ):
        """Triggering gh pr comment without a real PR number must block, not
        silently target the governance pr_id (e.g. "PR-4")."""
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/gh" if tool == "gh" else None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "1")

        # No ``gh pr comment`` call must be issued when pr_number is missing.
        # Other subprocess.run calls (e.g. path resolution via ``git rev-parse``)
        # are expected and unrelated to the gh trigger path.
        gh_calls: list = []

        def _trace_run(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "gh":
                gh_calls.append(list(cmd))
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = ""
            mock_proc.stderr = ""
            return mock_proc

        monkeypatch.setattr(rgm.subprocess, "run", _trace_run)

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        assert gh_calls == []
        assert receipt.state == STATE_BLOCKED
        assert receipt.reason == "missing_github_pr_number"

    def test_request_persisted_to_disk_with_contract_hash(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        manager.request_claude_github_with_contract(contract=sample_contract)

        request_file = manager.requests_dir / "pr4-claude_github_optional-contract.json"
        assert request_file.exists()
        saved = json.loads(request_file.read_text(encoding="utf-8"))
        assert saved["contract_hash"] == sample_contract.content_hash
        assert saved["pr_id"] == "PR-4"
        assert saved["state"] == STATE_NOT_CONFIGURED

    def test_non_completed_state_mirrored_into_results_dir(
        self, review_env, monkeypatch, sample_contract
    ):
        """Closure verifier reads optional-gate state from results/, not requests/.

        Non-completed states (not_configured, configured_dry_run, requested,
        blocked) must be mirrored into review_gates/results/ as a result record
        so closure_verifier._find_gate_result can locate the explicit state.
        """
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        manager.request_claude_github_with_contract(contract=sample_contract)

        result_file = manager.results_dir / "pr4-claude_github_optional-contract.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["pr_id"] == "PR-4"
        assert saved["state"] == STATE_NOT_CONFIGURED
        assert saved["was_intentionally_absent"] is True
        assert saved["contract_hash"] == sample_contract.content_hash

    def test_governance_receipt_emitted_with_contract_linkage(self, review_env, monkeypatch, sample_contract):
        receipts = []
        monkeypatch.setattr(
            rgm, "emit_governance_receipt",
            lambda event, **kw: receipts.append({"event": event, **kw})
        )
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        manager.request_claude_github_with_contract(contract=sample_contract)

        assert len(receipts) == 1
        receipt_payload = receipts[0]
        assert receipt_payload["event"] == "review_gate_request"
        assert receipt_payload["gate"] == "claude_github_optional"
        assert receipt_payload["contract_hash"] == sample_contract.content_hash
        assert receipt_payload["pr_id"] == "PR-4"
        assert "contributed_evidence" in receipt_payload
        assert "was_intentionally_absent" in receipt_payload


# ---------------------------------------------------------------------------
# Integration tests: record_claude_github_result
# ---------------------------------------------------------------------------

class TestRecordClaudeGitHubResult:
    def test_records_result_linked_to_contract(self, review_env, monkeypatch, sample_contract):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)

        manager = rgm.ReviewGateManager()
        receipt = manager.record_claude_github_result(
            pr_id="PR-4",
            branch="feature/review-contract-gates-and-idempotency",
            status="pass",
            summary="LGTM — Claude GitHub review complete",
            findings=[],
            contract_hash=sample_contract.content_hash,
            report_path=str((review_env / ".vnx-data/unified_reports/claude-contract-pr4.md").resolve()),
        )

        assert receipt.state == STATE_COMPLETED
        assert receipt.result_status == "pass"
        assert receipt.contract_hash == sample_contract.content_hash
        assert receipt.contributed_evidence() is True

    def test_result_classifies_findings_advisory_vs_blocking(self, review_env, monkeypatch):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)

        manager = rgm.ReviewGateManager()
        receipt = manager.record_claude_github_result(
            pr_id="PR-4",
            branch="feature/test",
            status="fail",
            summary="Blocking issue found",
            findings=[
                {"severity": "blocking", "category": "correctness", "message": "Missing guard"},
                {"severity": "advisory", "category": "style", "message": "Rename variable"},
                {"severity": "info", "category": "coverage", "message": "Add test"},
            ],
            contract_hash="abc123",
            report_path=str((review_env / ".vnx-data/unified_reports/claude-blocking-pr4.md").resolve()),
        )

        assert receipt.blocking_count == 1
        assert receipt.advisory_count == 2
        assert receipt.blocking_findings[0].message == "Missing guard"

    def test_result_persisted_to_disk_with_contract_hash(self, review_env, monkeypatch):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)

        manager = rgm.ReviewGateManager()
        manager.record_claude_github_result(
            pr_id="PR-4",
            branch="feature/test",
            status="pass",
            summary="Clean",
            contract_hash="hash999",
            report_path=str((review_env / ".vnx-data/unified_reports/claude-pr4.md").resolve()),
        )

        result_file = manager.results_dir / "pr4-claude_github_optional-contract.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["contract_hash"] == "hash999"
        assert saved["state"] == STATE_COMPLETED
        assert saved["contributed_evidence"] is True

    def test_result_canonicalizes_relative_report_path(self, review_env, monkeypatch):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)

        manager = rgm.ReviewGateManager()
        manager.record_claude_github_result(
            pr_id="PR-4",
            branch="feature/test",
            status="pass",
            summary="Clean",
            contract_hash="hash999",
            report_path=".vnx-data/unified_reports/claude-review.md",
        )

        result_file = manager.results_dir / "pr4-claude_github_optional-contract.json"
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        expected = str((review_env / ".vnx-data/unified_reports/claude-review.md").resolve())
        assert saved["report_path"] == expected

    def test_result_governance_receipt_includes_contract_hash(self, review_env, monkeypatch):
        receipts = []
        monkeypatch.setattr(
            rgm, "emit_governance_receipt",
            lambda event, **kw: receipts.append({"event": event, **kw})
        )

        manager = rgm.ReviewGateManager()
        manager.record_claude_github_result(
            pr_id="PR-4",
            branch="feature/test",
            status="pass",
            summary="LGTM",
            contract_hash="linked_hash_777",
            report_path=str((review_env / ".vnx-data/unified_reports/claude-pr4-receipt.md").resolve()),
        )

        assert len(receipts) == 1
        r = receipts[0]
        assert r["event"] == "review_gate_result"
        assert r["gate"] == "claude_github_optional"
        assert r["contract_hash"] == "linked_hash_777"
        assert "advisory_findings" in r
        assert "blocking_findings" in r
        assert "contributed_evidence" in r


# ---------------------------------------------------------------------------
# Gate: explicit state auditing across all three reviewers
# ---------------------------------------------------------------------------

class TestReviewContractLinkageAcrossStack:
    """Verify that T0 can see whether GitHub review contributed evidence or was absent."""

    def test_all_three_gates_produce_auditable_state(self, review_env, monkeypatch, sample_contract):
        """Simulate a full review stack request and verify all three gates are linkable."""
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()

        # Gemini — contract-driven path
        gemini_payload = manager.request_gemini_with_contract(contract=sample_contract)
        assert gemini_payload["contract_hash"] == sample_contract.content_hash
        assert gemini_payload["gate"] == "gemini_review"

        # Claude GitHub — contract-driven path
        claude_receipt = manager.request_claude_github_with_contract(contract=sample_contract)
        assert claude_receipt.contract_hash == sample_contract.content_hash
        assert claude_receipt.state in VALID_STATES

        # Both are linked to the same contract hash
        assert gemini_payload["contract_hash"] == claude_receipt.contract_hash

    def test_not_configured_state_is_explicit_not_silent(self, review_env, monkeypatch, sample_contract):
        """Ensure absence is never silent — state is always one of the known values."""
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        # State is always explicit
        assert receipt.state in VALID_STATES
        # Absence is surfaced, not hidden
        assert receipt.was_intentionally_absent() is True

    def test_configured_dry_run_is_distinguishable_from_not_configured(
        self, review_env, monkeypatch, sample_contract
    ):
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "1")
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/gh" if tool == "gh" else None)
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0")

        manager = rgm.ReviewGateManager()
        receipt = manager.request_claude_github_with_contract(contract=sample_contract)

        assert receipt.state == STATE_CONFIGURED_DRY_RUN
        assert receipt.state != STATE_NOT_CONFIGURED
        # Both are intentionally absent but distinguishable
        assert receipt.was_intentionally_absent() is True
