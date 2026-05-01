"""Tests for W3F receipt metadata fixes.

Covers:
- OI-1076: gate request payloads embed commit_sha
- OI-1128: _mark_gate_unavailable forwards dispatch_id into result records
- OI-1129: contract flows always include dispatch_id at top-level payload
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import importlib
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


# ---------------------------------------------------------------------------
# OI-1076: commit_sha embedded in request payloads
# ---------------------------------------------------------------------------

class TestCommitShaInPayloads:
    """OI-1076: all gate request payloads must include commit_sha."""

    def _get_real_sha(self) -> str:
        """Get the real HEAD sha for assertion."""
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
            cwd=str(VNX_ROOT),
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def test_gemini_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=1,
                branch="fix/test",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-gemini",
            )

        req_file = manager_env["requests_dir"] / "pr-1-gemini_review.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "gemini_review payload must contain commit_sha"
        # commit_sha is either a valid 40-char hex or empty string (git unavailable)
        sha = payload["commit_sha"]
        assert isinstance(sha, str), "commit_sha must be a string"
        if sha:
            assert len(sha) == 40, f"commit_sha must be 40-char hex, got: {sha!r}"

    def test_codex_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=2,
                branch="fix/test",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-codex",
            )

        req_file = manager_env["requests_dir"] / "pr-2-codex_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "codex_gate payload must contain commit_sha"

    def test_ci_gate_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=3,
                branch="fix/test",
                review_stack=["ci_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-ci",
            )

        req_file = manager_env["requests_dir"] / "pr-3-ci_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "ci_gate payload must contain commit_sha"

    def test_claude_github_request_payload_includes_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=4,
                branch="fix/test",
                review_stack=["claude_github_optional"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-claude-gh",
            )

        # OI-1307: claude_github_optional now routes through contract-based path
        # _contract_slug("4") = "4", so file is "4-claude_github_optional-contract.json"
        req_file = manager_env["requests_dir"] / "4-claude_github_optional-contract.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "commit_sha" in payload, "claude_github_optional payload must contain commit_sha"

    def test_commit_sha_matches_git_rev_parse_head(self, manager_env, monkeypatch):
        """commit_sha in payload must equal `git rev-parse HEAD` at request time."""
        monkeypatch.chdir(str(VNX_ROOT))
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        manager = _make_manager()

        expected_sha = self._get_real_sha()
        if not expected_sha:
            pytest.skip("git not available in test environment")

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=5,
                branch="fix/sha-verify",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-sha-match",
            )

        results_dir = manager_env["state_dir"] / "review_gates" / "requests"
        # Manager resolves paths from VNX_ROOT so look in actual VNX requests dir
        import scripts.lib.gate_request_handler as grh
        sha_from_helper = grh._get_head_commit_sha()
        assert sha_from_helper == expected_sha, (
            f"_get_head_commit_sha() should return {expected_sha!r}, got {sha_from_helper!r}"
        )

    def test_gemini_contract_payload_includes_commit_sha(self, manager_env, monkeypatch):
        """_build_gemini_contract_payload must embed commit_sha."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock prompt"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-10",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["scripts/foo.py"],
                        content_hash="abc123",
                    ),
                    mode="per_pr",
                    dispatch_id="test-sha-contract",
                )

        assert "commit_sha" in payload, "gemini contract payload must contain commit_sha"

    def test_ci_gate_contract_payload_includes_commit_sha(self, manager_env, monkeypatch):
        """_build_ci_gate_contract_payload must embed commit_sha."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-11",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["scripts/foo.py"],
                    content_hash="def456",
                ),
                pr_number=11,
                mode="per_pr",
                dispatch_id="test-sha-ci-contract",
            )

        assert "commit_sha" in payload, "ci_gate contract payload must contain commit_sha"


# ---------------------------------------------------------------------------
# OI-1128: _mark_gate_unavailable forwards dispatch_id
# ---------------------------------------------------------------------------

class TestMarkGateUnavailableDispatchId:
    """OI-1128: _mark_gate_unavailable must forward dispatch_id to the result record."""

    def test_not_executable_result_includes_dispatch_id(self, manager_env, monkeypatch):
        """When gemini gate is unavailable, the result record must include dispatch_id."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        manager = _make_manager()
        dispatch_id = "20260501-oi1128-test-A"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=20,
                branch="fix/unavail-test",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        result_file = manager_env["results_dir"] / "pr-20-gemini_review.json"
        assert result_file.exists(), "not_executable result file must be written"
        result = json.loads(result_file.read_text())
        assert result["status"] == "not_executable"
        assert result.get("dispatch_id") == dispatch_id, (
            f"result record must include dispatch_id={dispatch_id!r}, got: {result.get('dispatch_id')!r}"
        )

    def test_not_executable_result_includes_dispatch_id_for_codex(self, manager_env, monkeypatch):
        """codex_gate unavailable: result record must include dispatch_id."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
        manager = _make_manager()
        dispatch_id = "20260501-oi1128-test-B"

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=21,
                branch="fix/unavail-codex",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id=dispatch_id,
            )

        result_file = manager_env["results_dir"] / "pr-21-codex_gate.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result.get("dispatch_id") == dispatch_id

    def test_not_executable_result_omits_dispatch_id_when_not_provided(self, manager_env, monkeypatch):
        """When dispatch_id is not provided, not_executable result omits it."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=22,
                branch="fix/no-dispatch-id",
                review_stack=["gemini_review"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
            )

        result_file = manager_env["results_dir"] / "pr-22-gemini_review.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result["status"] == "not_executable"
        assert "dispatch_id" not in result, (
            "result record must not have dispatch_id when none was provided"
        )

    def test_mark_gate_unavailable_direct_call_forwards_dispatch_id(self, manager_env, monkeypatch):
        """Direct invocation of _mark_gate_unavailable writes dispatch_id to result file."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        dispatch_id = "20260501-oi1128-direct"

        payload: Dict[str, Any] = {
            "gate": "gemini_review",
            "status": "not_executable",
            "requested_at": "2026-05-01T00:00:00Z",
        }

        manager._mark_gate_unavailable(
            payload,
            gate="gemini_review",
            binary_name="gemini",
            pr_number=30,
            pr_id="",
            dispatch_id=dispatch_id,
        )

        result_file = manager_env["results_dir"] / "pr-30-gemini_review.json"
        assert result_file.exists()
        result = json.loads(result_file.read_text())
        assert result.get("dispatch_id") == dispatch_id


# ---------------------------------------------------------------------------
# OI-1129: contract flows always include dispatch_id at top-level
# ---------------------------------------------------------------------------

class TestContractFlowDispatchIdTopLevel:
    """OI-1129: contract flows must always emit dispatch_id at the top-level of the payload."""

    def test_gemini_contract_always_has_dispatch_id_when_provided(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-50",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["f.py"],
                        content_hash="aaa",
                    ),
                    mode="per_pr",
                    dispatch_id="contract-dispatch-50",
                )

        assert payload.get("dispatch_id") == "contract-dispatch-50", (
            "dispatch_id must be at top-level of gemini contract payload"
        )

    def test_gemini_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        """When dispatch_id='', it must still appear at top-level (not absent)."""
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            with patch("gate_request_handler.render_gemini_prompt", return_value="mock"):
                payload = manager.request_gemini_with_contract(
                    contract=ReviewContract(
                        pr_id="PR-51",
                        branch="fix/test",
                        risk_class="low",
                        changed_files=["f.py"],
                        content_hash="bbb",
                    ),
                    mode="per_pr",
                )

        assert "dispatch_id" in payload, (
            "dispatch_id must always be present at top-level in gemini contract payload"
        )
        assert payload["dispatch_id"] == "", (
            "dispatch_id must be empty string when not provided, not absent"
        )

    def test_claude_github_contract_has_dispatch_id_at_top_level(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            receipt = manager.request_claude_github_with_contract(
                contract=ReviewContract(
                    pr_id="PR-52",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="ccc",
                ),
                mode="per_pr",
                dispatch_id="contract-dispatch-52",
            )

        # The persisted payload file is what matters for top-level dispatch_id
        # Contract slug: PR-52 → pr52 (lower, hyphens removed)
        requests_dir = manager_env["requests_dir"]
        req_file = requests_dir / "pr52-claude_github_optional-contract.json"
        assert req_file.exists(), f"Expected contract request file at {req_file}"
        payload = json.loads(req_file.read_text())
        assert payload.get("dispatch_id") == "contract-dispatch-52", (
            "dispatch_id must be at top-level of claude_github contract payload"
        )

    def test_claude_github_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_claude_github_with_contract(
                contract=ReviewContract(
                    pr_id="PR-53",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="ddd",
                ),
                mode="per_pr",
            )

        # Contract slug: PR-53 → pr53 (lower, hyphens removed)
        requests_dir = manager_env["requests_dir"]
        req_file = requests_dir / "pr53-claude_github_optional-contract.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert "dispatch_id" in payload, "dispatch_id must always be present in claude_github contract payload"
        assert payload["dispatch_id"] == ""

    def test_ci_gate_contract_has_dispatch_id_at_top_level(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-54",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="eee",
                ),
                pr_number=54,
                mode="per_pr",
                dispatch_id="contract-dispatch-54",
            )

        assert payload.get("dispatch_id") == "contract-dispatch-54", (
            "dispatch_id must be at top-level of ci_gate contract payload"
        )

    def test_ci_gate_contract_has_dispatch_id_even_when_empty(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")

        from review_contract import ReviewContract
        manager = _make_manager()

        with patch("governance_receipts.emit_governance_receipt"):
            payload = manager.request_ci_gate_with_contract(
                contract=ReviewContract(
                    pr_id="PR-55",
                    branch="fix/test",
                    risk_class="low",
                    changed_files=["f.py"],
                    content_hash="fff",
                ),
                pr_number=55,
                mode="per_pr",
            )

        assert "dispatch_id" in payload, "dispatch_id must always be present in ci_gate contract payload"
        assert payload["dispatch_id"] == ""
