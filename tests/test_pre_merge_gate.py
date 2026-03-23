#!/usr/bin/env python3
"""Tests for pre-merge gate enforcement (PR-6).

Tests the individual gate checks and the orchestrator that combines them
into a deterministic GO/HOLD verdict.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from pre_merge_gate import (
    check_open_items,
    check_cqs,
    check_git_cleanliness,
    check_contract_verification,
    check_pytest,
    check_quality_advisory,
    check_pr_size,
    check_artifacts,
    check_shell_syntax,
    run_gate_checks,
    store_gate_result,
    format_human_readable,
    _find_dispatch_for_pr,
    _is_artifact_path,
    CQS_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Create a state directory with standard structure."""
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


@pytest.fixture
def dispatch_dir(tmp_path):
    """Create a dispatch directory with standard subdirs."""
    dd = tmp_path / "dispatches"
    for sub in ("pending", "active", "completed", "staging"):
        (dd / sub).mkdir(parents=True)
    return dd


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project root."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "tests").mkdir()
    return root


# ---------------------------------------------------------------------------
# check_open_items
# ---------------------------------------------------------------------------

class TestCheckOpenItems:

    def test_no_file(self, state_dir):
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "GO"
        assert result["blockers"] == 0

    def test_no_blockers(self, state_dir):
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "open", "severity": "warn", "title": "Minor issue", "pr_id": "PR-6"},
                {"id": "OI-002", "status": "open", "severity": "info", "title": "Note", "pr_id": "PR-6"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "GO"
        assert result["warnings"] == 1

    def test_blocker_present(self, state_dir):
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "open", "severity": "blocker", "title": "Critical bug", "pr_id": "PR-6"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "HOLD"
        assert result["blockers"] == 1
        assert "Critical bug" in result["blocker_titles"]

    def test_blocker_for_different_pr(self, state_dir):
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "open", "severity": "blocker", "title": "Other PR issue", "pr_id": "PR-5"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "GO"

    def test_resolved_blocker_ignored(self, state_dir):
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "done", "severity": "blocker", "title": "Fixed", "pr_id": "PR-6"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "GO"

    def test_global_blocker_included(self, state_dir):
        """Blockers with no pr_id apply to all PRs."""
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "open", "severity": "blocker", "title": "Global issue"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = check_open_items("PR-6", state_dir)
        assert result["status"] == "HOLD"


# ---------------------------------------------------------------------------
# check_cqs
# ---------------------------------------------------------------------------

class TestCheckCQS:

    def test_no_receipts_file(self, state_dir):
        result = check_cqs("PR-6", state_dir)
        assert result["status"] == "GO"
        assert result["cqs"] is None

    def test_receipt_above_threshold(self, state_dir):
        receipt = {"pr_id": "PR-6", "status": "success", "report_path": "/some/report.md"}
        (state_dir / "t0_receipts.ndjson").write_text(json.dumps(receipt) + "\n")
        result = check_cqs("PR-6", state_dir)
        assert result["status"] == "GO"
        assert result["cqs"] is not None
        assert result["cqs"] >= CQS_THRESHOLD

    def test_receipt_below_threshold(self, state_dir):
        receipt = {"pr_id": "PR-6", "status": "failed"}
        (state_dir / "t0_receipts.ndjson").write_text(json.dumps(receipt) + "\n")
        result = check_cqs("PR-6", state_dir)
        assert result["status"] == "HOLD"
        assert result["cqs"] is not None
        assert result["cqs"] < CQS_THRESHOLD

    def test_no_matching_pr(self, state_dir):
        receipt = {"pr_id": "PR-5", "status": "success"}
        (state_dir / "t0_receipts.ndjson").write_text(json.dumps(receipt) + "\n")
        result = check_cqs("PR-6", state_dir)
        assert result["status"] == "GO"
        assert result["cqs"] is None


# ---------------------------------------------------------------------------
# check_git_cleanliness
# ---------------------------------------------------------------------------

class TestCheckGitCleanliness:

    def test_clean_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(repo), capture_output=True)
        result = check_git_cleanliness(repo)
        assert result["status"] == "GO"
        assert result["has_conflicts"] is False

    def test_dirty_repo_still_go(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(repo), capture_output=True)
        (repo / "untracked.txt").write_text("hello")
        result = check_git_cleanliness(repo)
        assert result["status"] == "GO"
        assert result["dirty_files"] >= 1


# ---------------------------------------------------------------------------
# check_contract_verification
# ---------------------------------------------------------------------------

class TestCheckContractVerification:

    def test_no_dispatch(self, dispatch_dir, tmp_path, state_dir):
        result = check_contract_verification("PR-99", dispatch_dir, tmp_path, state_dir)
        assert result["status"] == "GO"
        assert result["verdict"] == "no_dispatch"

    def test_no_contract(self, dispatch_dir, tmp_path, state_dir):
        dispatch_content = "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: test-123\n\nSome work.\n"
        (dispatch_dir / "active" / "test-123.md").write_text(dispatch_content)
        result = check_contract_verification("PR-6", dispatch_dir, tmp_path, state_dir)
        assert result["status"] == "GO"
        assert result["verdict"] == "no_contract"

    def test_contract_pass(self, dispatch_dir, tmp_path, state_dir):
        target_file = tmp_path / "output.txt"
        target_file.write_text("hello world")
        dispatch_content = (
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: test-456\n\n"
            "## Contract\n"
            f"- file_exists: {target_file}\n"
        )
        (dispatch_dir / "active" / "test-456.md").write_text(dispatch_content)
        result = check_contract_verification("PR-6", dispatch_dir, tmp_path, state_dir)
        assert result["status"] == "GO"
        assert result["verdict"] == "pass"
        assert result["passed"] == 1

    def test_contract_fail(self, dispatch_dir, tmp_path, state_dir):
        dispatch_content = (
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: test-789\n\n"
            "## Contract\n"
            "- file_exists: /nonexistent/file.txt\n"
        )
        (dispatch_dir / "active" / "test-789.md").write_text(dispatch_content)
        result = check_contract_verification("PR-6", dispatch_dir, tmp_path, state_dir)
        assert result["status"] == "HOLD"
        assert result["verdict"] == "fail"
        assert result["failed"] == 1


# ---------------------------------------------------------------------------
# check_artifacts
# ---------------------------------------------------------------------------

class TestCheckArtifacts:

    def test_no_dispatch(self, dispatch_dir, tmp_path):
        result = check_artifacts("PR-99", dispatch_dir, tmp_path)
        assert result["status"] == "GO"

    def test_no_artifact_claims(self, dispatch_dir, tmp_path):
        dispatch_content = (
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: art-1\n\n"
            "## Contract\n"
            "- file_exists: scripts/foo.py\n"
        )
        (dispatch_dir / "active" / "art-1.md").write_text(dispatch_content)
        result = check_artifacts("PR-6", dispatch_dir, tmp_path)
        assert result["status"] == "GO"
        assert result["artifacts_checked"] == 0

    def test_pdf_artifact_pass(self, dispatch_dir, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")
        dispatch_content = (
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: art-2\n\n"
            "## Contract\n"
            f"- file_exists: {pdf}\n"
        )
        (dispatch_dir / "active" / "art-2.md").write_text(dispatch_content)
        result = check_artifacts("PR-6", dispatch_dir, tmp_path)
        assert result["status"] == "GO"
        assert result["artifacts_checked"] == 1

    def test_xlsx_artifact_missing(self, dispatch_dir, tmp_path):
        dispatch_content = (
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: art-3\n\n"
            "## Contract\n"
            "- file_exists: /nonexistent/report.xlsx\n"
        )
        (dispatch_dir / "active" / "art-3.md").write_text(dispatch_content)
        result = check_artifacts("PR-6", dispatch_dir, tmp_path)
        assert result["status"] == "HOLD"
        assert result["artifacts_failed"] == 1


# ---------------------------------------------------------------------------
# check_shell_syntax
# ---------------------------------------------------------------------------

class TestCheckShellSyntax:

    @patch("pre_merge_gate.get_changed_files")
    def test_no_shell_files(self, mock_gcf, tmp_path):
        mock_gcf.return_value = [tmp_path / "foo.py"]
        result = check_shell_syntax(tmp_path)
        assert result["status"] == "GO"
        assert result["files_checked"] == 0

    @patch("pre_merge_gate.get_changed_files")
    def test_valid_shell(self, mock_gcf, tmp_path):
        sh = tmp_path / "good.sh"
        sh.write_text("#!/bin/bash\necho hello\n")
        mock_gcf.return_value = [sh]
        result = check_shell_syntax(tmp_path)
        assert result["status"] == "GO"
        assert result["files_checked"] == 1

    @patch("pre_merge_gate.get_changed_files")
    def test_invalid_shell(self, mock_gcf, tmp_path):
        sh = tmp_path / "bad.sh"
        sh.write_text("#!/bin/bash\nif true; then\n")  # missing fi
        mock_gcf.return_value = [sh]
        result = check_shell_syntax(tmp_path)
        assert result["status"] == "HOLD"
        assert len(result["failures"]) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_is_artifact_pdf(self):
        assert _is_artifact_path("report.pdf") is True
        assert _is_artifact_path("REPORT.PDF") is True

    def test_is_artifact_xlsx(self):
        assert _is_artifact_path("data.xlsx") is True
        assert _is_artifact_path("data.xls") is True

    def test_is_not_artifact(self):
        assert _is_artifact_path("script.py") is False
        assert _is_artifact_path("readme.md") is False

    def test_find_dispatch_for_pr(self, dispatch_dir):
        dispatch_content = "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: find-test\n"
        (dispatch_dir / "active" / "find-test.md").write_text(dispatch_content)
        found = _find_dispatch_for_pr("PR-6", dispatch_dir)
        assert found is not None
        assert "find-test" in found.name

    def test_find_dispatch_for_pr_not_found(self, dispatch_dir):
        found = _find_dispatch_for_pr("PR-99", dispatch_dir)
        assert found is None


# ---------------------------------------------------------------------------
# Gate orchestrator
# ---------------------------------------------------------------------------

class TestRunGateChecks:

    def test_all_go_verdict(self, state_dir, dispatch_dir, tmp_path):
        """When all checks pass, verdict is GO."""
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=True,
        )
        assert result["verdict"] == "GO"
        assert result["hold_count"] == 0
        assert result["pr_id"] == "PR-6"

    def test_hold_on_blocker(self, state_dir, dispatch_dir, tmp_path):
        """When open items have a blocker, verdict is HOLD."""
        oi = {
            "schema_version": "1.0",
            "items": [
                {"id": "OI-001", "status": "open", "severity": "blocker", "title": "Blocks merge", "pr_id": "PR-6"},
            ],
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=True,
        )
        assert result["verdict"] == "HOLD"
        assert result["hold_count"] >= 1
        assert any(r["check"] == "open_items" for r in result["hold_reasons"])

    def test_checks_list_populated(self, state_dir, dispatch_dir, tmp_path):
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=True,
        )
        check_names = [c["check"] for c in result["checks"]]
        assert "open_items" in check_names
        assert "cqs_threshold" in check_names
        assert "git_cleanliness" in check_names
        assert "contract_verification" in check_names
        assert "quality_advisory" in check_names
        assert "pr_size" in check_names
        assert "artifact_verification" in check_names
        assert "shell_syntax" in check_names

    def test_pytest_included_when_not_skipped(self, state_dir, dispatch_dir, tmp_path):
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=False,
        )
        check_names = [c["check"] for c in result["checks"]]
        assert "pytest" in check_names

    def test_pytest_excluded_when_skipped(self, state_dir, dispatch_dir, tmp_path):
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=True,
        )
        check_names = [c["check"] for c in result["checks"]]
        assert "pytest" not in check_names


# ---------------------------------------------------------------------------
# Storage and formatting
# ---------------------------------------------------------------------------

class TestStorageAndFormat:

    def test_store_gate_result(self, state_dir):
        result = {
            "pr_id": "PR-6",
            "verdict": "GO",
            "checked_at": "2026-03-22T20:00:00Z",
            "checks": [],
        }
        path = store_gate_result(result, state_dir)
        assert path.exists()
        stored = json.loads(path.read_text())
        assert stored["verdict"] == "GO"

    def test_format_human_readable_go(self):
        result = {
            "pr_id": "PR-6",
            "verdict": "GO",
            "checked_at": "2026-03-22T20:00:00Z",
            "go_count": 8,
            "hold_count": 0,
            "checks": [
                {"check": "open_items", "status": "GO", "detail": "no blockers"},
            ],
            "hold_reasons": [],
        }
        output = format_human_readable(result)
        assert "GO" in output
        assert "PR-6" in output

    def test_format_human_readable_hold(self):
        result = {
            "pr_id": "PR-6",
            "verdict": "HOLD",
            "checked_at": "2026-03-22T20:00:00Z",
            "go_count": 7,
            "hold_count": 1,
            "checks": [
                {"check": "open_items", "status": "HOLD", "detail": "1 blocker"},
            ],
            "hold_reasons": [
                {"check": "open_items", "detail": "1 blocker"},
            ],
        }
        output = format_human_readable(result)
        assert "HOLD" in output
        assert "open_items" in output
