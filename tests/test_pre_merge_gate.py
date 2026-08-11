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

import pre_merge_gate
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
    check_net_deletion,
    check_ci_workflow,
    run_gate_checks,
    store_gate_result,
    format_human_readable,
    _find_dispatch_for_pr,
    _resolve_dispatch_id_for_pr,
    _is_artifact_path,
    _resolve_ci_workflow_name,
    CQS_THRESHOLD,
    DELETION_FILE_WARN,
    DELETION_FILE_HOLD,
    PR_SIZE_WARN,
    PR_SIZE_HOLD,
    SKIPPED_UNVERIFIED,
    DEFAULT_CI_WORKFLOW_NAME,
    CI_WORKFLOW_NAME_ENV_VAR,
)

VNX_ROOT = Path(__file__).resolve().parent.parent


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


def _stub_pr_size_go(monkeypatch):
    """Stub check_pr_size to a fixed GO result.

    check_pr_size now resolves a real git merge-base (OI-838); tests in this
    module that exercise run_gate_checks orchestration/wiring (not PR-size
    logic itself) use a bare non-git tmp_path project_root, so the real
    implementation would correctly HOLD with "could not resolve a merge-base"
    — noise for tests that aren't about PR size. See TestCheckPRSize for the
    real git-backed coverage.
    """
    monkeypatch.setattr(
        pre_merge_gate, "check_pr_size",
        lambda project_root, **kw: {
            "check": "pr_size", "status": "GO", "detail": "stubbed for this test",
            "lines_added": 0, "lines_removed": 0, "lines_changed": 0,
        },
    )


def _stub_ci_workflow_go(monkeypatch):
    """Stub check_ci_workflow to a fixed GO result.

    Like _stub_pr_size_go above: check_ci_workflow now correctly returns
    SKIPPED_UNVERIFIED (OI-1140) against a bare non-git tmp_path, since HEAD
    can't be resolved there. Tests in this module that exercise run_gate_checks
    orchestration/wiring (not ci_workflow logic itself) stub it to GO so that
    unrelated behavior isn't blocked by noise from this check. See
    TestCheckCIWorkflow for the real coverage of ci_workflow's own logic.
    """
    monkeypatch.setattr(
        pre_merge_gate, "check_ci_workflow",
        lambda project_root, **kw: {
            "check": "ci_workflow", "status": "GO", "detail": "stubbed for this test",
            "ci_conclusion": "success", "ci_ran_on_sha": True,
        },
    )


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
# check_net_deletion
# ---------------------------------------------------------------------------

class TestCheckNetDeletion:

    def _make_git_repo(self, tmp_path: Path) -> Path:
        """Create a minimal git repo with one commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True,
        )
        (repo / "base.py").write_text("# base\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), capture_output=True,
        )
        return repo

    def test_no_deletions(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        (repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add file"],
            cwd=str(repo), capture_output=True,
        )
        result = check_net_deletion(repo)
        assert result["status"] == "GO"
        assert result["deleted_count"] == 0
        assert result["deleted_files"] == []

    def test_below_warn_threshold(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        for i in range(DELETION_FILE_WARN - 1):
            (repo / f"file_{i}.py").write_text(f"# file {i}\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add files"],
            cwd=str(repo), capture_output=True,
        )
        # Delete them in a second commit
        for i in range(DELETION_FILE_WARN - 1):
            (repo / f"file_{i}.py").unlink()
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "delete files"],
            cwd=str(repo), capture_output=True,
        )
        result = check_net_deletion(repo)
        assert result["status"] == "GO"
        assert result["deleted_count"] == DELETION_FILE_WARN - 1

    def test_at_warn_threshold_is_go(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        for i in range(DELETION_FILE_WARN):
            (repo / f"file_{i}.py").write_text(f"# file {i}\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add files"],
            cwd=str(repo), capture_output=True,
        )
        for i in range(DELETION_FILE_WARN):
            (repo / f"file_{i}.py").unlink()
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "delete files"],
            cwd=str(repo), capture_output=True,
        )
        result = check_net_deletion(repo)
        assert result["status"] == "GO"
        assert result["deleted_count"] == DELETION_FILE_WARN
        assert str(DELETION_FILE_WARN) in result["detail"]

    def test_at_hold_threshold_is_hold(self, tmp_path):
        repo = self._make_git_repo(tmp_path)
        for i in range(DELETION_FILE_HOLD):
            (repo / f"file_{i}.py").write_text(f"# file {i}\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add files"],
            cwd=str(repo), capture_output=True,
        )
        for i in range(DELETION_FILE_HOLD):
            (repo / f"file_{i}.py").unlink()
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "delete files"],
            cwd=str(repo), capture_output=True,
        )
        result = check_net_deletion(repo)
        assert result["status"] == "HOLD"
        assert result["deleted_count"] == DELETION_FILE_HOLD
        assert len(result["deleted_files"]) == DELETION_FILE_HOLD

    def test_hold_on_mass_deletion(self, tmp_path):
        """More than HOLD threshold files deleted triggers HOLD."""
        count = DELETION_FILE_HOLD + 3
        repo = self._make_git_repo(tmp_path)
        for i in range(count):
            (repo / f"file_{i}.py").write_text(f"# file {i}\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add files"],
            cwd=str(repo), capture_output=True,
        )
        for i in range(count):
            (repo / f"file_{i}.py").unlink()
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "mass delete"],
            cwd=str(repo), capture_output=True,
        )
        result = check_net_deletion(repo)
        assert result["status"] == "HOLD"
        assert result["deleted_count"] == count

    @patch("subprocess.run")
    def test_git_failure_is_go(self, mock_run, tmp_path):
        """If git command fails, check degrades gracefully to GO."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        mock_run.return_value = mock_result
        result = check_net_deletion(tmp_path)
        assert result["status"] == "GO"
        assert result["deleted_count"] is None


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

    def test_resolve_dispatch_id_for_pr(self, dispatch_dir):
        content = "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: gate-findings-fabric-test\n"
        (dispatch_dir / "active" / "d.md").write_text(content)
        assert _resolve_dispatch_id_for_pr("PR-6", dispatch_dir) == "gate-findings-fabric-test"

    def test_resolve_dispatch_id_for_pr_no_dispatch_file(self, dispatch_dir):
        assert _resolve_dispatch_id_for_pr("PR-99", dispatch_dir) is None


# ---------------------------------------------------------------------------
# Gate -> fabric wiring (gate_findings_bridge)
# ---------------------------------------------------------------------------

class TestFabricFindingWiring:
    """run_gate_checks must call the bridge best-effort, keyed on the resolved
    dispatch_id, without ever changing its own verdict."""

    def test_hold_records_finding_when_dispatch_resolved(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch
    ):
        (dispatch_dir / "active" / "d.md").write_text(
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: d-hold\n"
        )
        oi = {"schema_version": "1.0", "items": [
            {"id": "OI-001", "status": "open", "severity": "blocker", "title": "x", "pr_id": "PR-6"},
        ]}
        (state_dir / "open_items.json").write_text(json.dumps(oi))

        captured = {}
        monkeypatch.setattr(
            pre_merge_gate, "record_gate_finding",
            lambda state_dir, **kw: captured.update(kw) or True,
        )
        result = run_gate_checks(
            pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True,
        )
        assert result["verdict"] == "HOLD"
        assert captured["dispatch_id"] == "d-hold"
        assert captured["gate_name"] == "pre_merge_gate"
        assert captured["pr_ref"] == "PR-6"
        assert "open_items" in captured["summary"]

    def test_go_resolves_finding_when_dispatch_resolved(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch
    ):
        _stub_pr_size_go(monkeypatch)
        _stub_ci_workflow_go(monkeypatch)
        (dispatch_dir / "active" / "d.md").write_text(
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: d-go\n"
        )
        captured = {}
        monkeypatch.setattr(
            pre_merge_gate, "resolve_gate_finding",
            lambda state_dir, **kw: captured.update(kw) or True,
        )
        result = run_gate_checks(
            pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True,
        )
        assert result["verdict"] == "GO"
        assert captured["dispatch_id"] == "d-go"
        assert captured["gate_name"] == "pre_merge_gate"

    def test_no_dispatch_file_skips_bridge_entirely(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch
    ):
        """No dispatch file for this PR -> unresolved dispatch_id -> bridge never called."""
        calls = {"n": 0}
        monkeypatch.setattr(
            pre_merge_gate, "record_gate_finding",
            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
        )
        monkeypatch.setattr(
            pre_merge_gate, "resolve_gate_finding",
            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
        )
        run_gate_checks(
            pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True,
        )
        assert calls["n"] == 0

    def test_bridge_failure_never_changes_verdict(
        self, state_dir, dispatch_dir, tmp_path, monkeypatch
    ):
        """Defense in depth: even if the (already best-effort) bridge itself regressed and
        raised, run_gate_checks must still return its verdict rather than crash."""
        _stub_pr_size_go(monkeypatch)
        _stub_ci_workflow_go(monkeypatch)
        (dispatch_dir / "active" / "d.md").write_text(
            "# Dispatch\n\n**PR**: PR-6\nDispatch-ID: d-boom\n"
        )

        def _boom(*a, **k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(pre_merge_gate, "resolve_gate_finding", _boom)
        result = run_gate_checks(
            pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
            dispatch_dir=dispatch_dir, skip_pytest=True,
        )
        assert result["verdict"] == "GO"


# ---------------------------------------------------------------------------
# Gate orchestrator
# ---------------------------------------------------------------------------

class TestRunGateChecks:

    def test_all_go_verdict(self, state_dir, dispatch_dir, tmp_path, monkeypatch):
        """When all checks pass, verdict is GO."""
        _stub_pr_size_go(monkeypatch)
        _stub_ci_workflow_go(monkeypatch)
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

    def test_hold_on_unverifiable_ci(self, state_dir, dispatch_dir, tmp_path, monkeypatch):
        """OI-1140: SKIPPED_UNVERIFIED from ci_workflow blocks the verdict —
        it must never read as permission to merge, same as a real HOLD.
        """
        _stub_pr_size_go(monkeypatch)
        result = run_gate_checks(
            pr_id="PR-6",
            project_root=tmp_path,
            state_dir=state_dir,
            dispatch_dir=dispatch_dir,
            skip_pytest=True,
        )
        assert result["verdict"] == "HOLD"
        assert result["skipped_unverified_count"] >= 1
        ci_check = next(c for c in result["checks"] if c["check"] == "ci_workflow")
        assert ci_check["status"] == SKIPPED_UNVERIFIED
        assert any(r["check"] == "ci_workflow" for r in result["hold_reasons"])

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
        assert "net_deletion" in check_names
        assert "ci_workflow" in check_names

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


# ---------------------------------------------------------------------------
# check_pr_size (OI-838): must measure the merge-base range, never the
# working tree or an unrelated later commit on the base branch.
# ---------------------------------------------------------------------------

class TestCheckPRSize:
    def test_reports_lines_matching_real_merged_pr(self):
        """Regression pin for OI-838: measured against its own merge-base, the
        reported count for a known merged PR (#1246) matches
        `gh pr view 1246 --json additions,deletions` (110 additions, 0
        deletions) — verified via `gh` against this exact commit pair.
        """
        result = check_pr_size(VNX_ROOT, base_ref="9e0813e2", head_ref="5ab8fe8c")
        assert result["status"] == "GO"
        assert result["lines_added"] == 110
        assert result["lines_removed"] == 0
        assert result["lines_changed"] == 110

    def test_unresolvable_base_fails_loud_not_silent_go(self, tmp_path):
        """No base branch resolvable locally -> HOLD with a clear reason, never a
        silent GO carrying a wrong (working-tree-derived) line count."""
        unresolvable = MagicMock(returncode=128, stdout="", stderr="fatal: bad revision")
        with patch("pre_merge_gate.subprocess.run", return_value=unresolvable):
            result = check_pr_size(tmp_path)
        assert result["status"] == "HOLD"
        assert result["lines_changed"] is None
        assert "merge-base" in result["detail"]

    def test_diff_failure_after_resolved_base_fails_loud(self, tmp_path):
        """merge-base resolves but the numstat diff itself fails -> HOLD, not a
        silently wrong number."""
        merge_base_ok = MagicMock(returncode=0, stdout="abc1234\n")
        diff_fail = MagicMock(returncode=128, stdout="", stderr="fatal: bad object")
        with patch("pre_merge_gate.subprocess.run", side_effect=[merge_base_ok, diff_fail]):
            result = check_pr_size(tmp_path, base_ref="origin/main")
        assert result["status"] == "HOLD"
        assert result["lines_changed"] is None

    def test_threshold_hold_uses_merge_base_range(self, tmp_path):
        merge_base_ok = MagicMock(returncode=0, stdout="abc1234\n")
        big_diff = MagicMock(returncode=0, stdout=f"{PR_SIZE_HOLD + 1}\t0\tfile.py\n")
        with patch("pre_merge_gate.subprocess.run", side_effect=[merge_base_ok, big_diff]):
            result = check_pr_size(tmp_path, base_ref="origin/main")
        assert result["status"] == "HOLD"
        assert result["lines_changed"] == PR_SIZE_HOLD + 1

    def test_small_diff_is_go(self, tmp_path):
        merge_base_ok = MagicMock(returncode=0, stdout="abc1234\n")
        small_diff = MagicMock(returncode=0, stdout="5\t2\tfile.py\n")
        with patch("pre_merge_gate.subprocess.run", side_effect=[merge_base_ok, small_diff]):
            result = check_pr_size(tmp_path, base_ref="origin/main")
        assert result["status"] == "GO"
        assert result["lines_changed"] == 7

    def test_falls_back_from_origin_main_to_origin_master(self, tmp_path):
        """When origin/main isn't resolvable but origin/master is, the master
        merge-base is used instead of failing."""
        main_unresolvable = MagicMock(returncode=128, stdout="", stderr="unknown revision")
        master_ok = MagicMock(returncode=0, stdout="def5678\n")
        diff_ok = MagicMock(returncode=0, stdout="3\t1\tfile.py\n")
        with patch(
            "pre_merge_gate.subprocess.run",
            side_effect=[main_unresolvable, master_ok, diff_ok],
        ):
            result = check_pr_size(tmp_path)
        assert result["status"] == "GO"
        assert result["lines_changed"] == 4
        assert "origin/master" in result["detail"]


# ---------------------------------------------------------------------------
# check_ci_workflow — OI-931
# ---------------------------------------------------------------------------

class TestCheckCIWorkflow:
    """OI-931: three-way CI state — never ran, ran+failed, ran+succeeded.

    check_ci_workflow makes three subprocess calls in order:
      1. git rev-parse HEAD       → head_sha
      2. git rev-parse --abbrev-ref HEAD → branch
      3. gh run list ...          → CI workflow runs

    Each test provides three MagicMock results matching that order.
    """

    @staticmethod
    def _git_head_mock(sha: str) -> MagicMock:
        return MagicMock(returncode=0, stdout=sha + "\n", stderr="")

    @staticmethod
    def _git_branch_mock(branch: str = "feature-branch") -> MagicMock:
        return MagicMock(returncode=0, stdout=branch + "\n", stderr="")

    @staticmethod
    def _gh_run_output(conclusion, head_sha, status="completed", database_id=12345):
        """Build gh run list JSON output for one run."""
        runs = [{
            "conclusion": conclusion,
            "headSha": head_sha,
            "status": status,
            "databaseId": database_id,
        }]
        return MagicMock(returncode=0, stdout=json.dumps(runs), stderr="")

    @staticmethod
    def _gh_empty_output():
        """gh run list returns no runs."""
        return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")

    def test_ci_succeeded(self, tmp_path):
        """Workflow ran on HEAD with conclusion=success → GO."""
        sha = "a" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("success", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "GO"
        assert result["ci_conclusion"] == "success"
        assert result["ci_ran_on_sha"] is True

    def test_ci_failed(self, tmp_path):
        """Workflow ran on HEAD but conclusion=failure → HOLD."""
        sha = "b" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("failure", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "HOLD"
        assert result["ci_conclusion"] == "failure"
        assert result["ci_ran_on_sha"] is True
        assert "must be 'success'" in result["detail"]

    def test_ci_never_ran(self, tmp_path):
        """No runs exist on the branch at all → HOLD with distinct message."""
        sha = "c" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_empty_output(),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "HOLD"
        assert result["ci_conclusion"] is None
        assert result["ci_ran_on_sha"] is False
        assert "NEVER run" in result["detail"]

    def test_ci_ran_on_different_sha(self, tmp_path):
        """Runs exist on the branch but not on HEAD → HOLD with SHA-mismatch detail."""
        sha = "d" * 40
        different_sha = "e" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("success", different_sha, database_id=99999),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "HOLD"
        assert result["ci_ran_on_sha"] is False
        assert result["ci_head_sha"] == sha
        assert "NOT run on HEAD" in result["detail"]
        assert result["ci_latest_run_sha"] == different_sha[:12]

    def test_ci_conclusion_cancelled_is_hold(self, tmp_path):
        """Cancelled workflow conclusion is not 'success' → HOLD."""
        sha = "f" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("cancelled", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "HOLD"
        assert result["ci_conclusion"] == "cancelled"

    def test_gh_not_available_is_unverified(self, tmp_path):
        """OI-1140: gh CLI missing -> SKIPPED_UNVERIFIED, never GO."""
        sha = "g" * 40
        git_head = self._git_head_mock(sha)
        git_branch = self._git_branch_mock()
        mocks = [git_head, git_branch, FileNotFoundError("gh not found")]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "not available" in result["detail"]
        assert result["ci_conclusion"] is None

    def test_gh_run_list_fails_is_unverified(self, tmp_path):
        """OI-1140: gh run list non-zero exit -> SKIPPED_UNVERIFIED, never GO."""
        sha = "h" * 40
        gh_fail = MagicMock(returncode=1, stdout="", stderr="HTTP 403")
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            gh_fail,
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "gh run list failed" in result["detail"]

    def test_gh_no_such_workflow_is_unverified(self, tmp_path):
        """OI-1140: consumer repo's workflow doesn't match the configured name
        (measured by the seocrawler-v2 operator: `gh` reports "could not find
        any workflows named VNX CI") -> SKIPPED_UNVERIFIED, never GO.
        """
        sha = "s" * 40
        gh_fail = MagicMock(
            returncode=1, stdout="",
            stderr="could not find any workflows named VNX CI",
        )
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            gh_fail,
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "could not find any workflows" in result["detail"]

    def test_gh_output_unparseable_is_unverified(self, tmp_path):
        """OI-1140: gh emits non-JSON output -> SKIPPED_UNVERIFIED, never GO.

        A 5th silent-GO path found while auditing the ones the dispatch
        listed explicitly — same shape, same fix.
        """
        sha = "t" * 40
        gh_bad_json = MagicMock(returncode=0, stdout="not json", stderr="")
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            gh_bad_json,
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "unparseable" in result["detail"]

    def test_git_head_fails_is_unverified(self, tmp_path):
        """OI-1140: git rev-parse HEAD fails -> SKIPPED_UNVERIFIED, never GO."""
        git_fail = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("pre_merge_gate.subprocess.run", side_effect=[git_fail]):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "could not resolve HEAD SHA" in result["detail"]

    def test_git_branch_fails_is_unverified(self, tmp_path):
        """OI-1140: git rev-parse --abbrev-ref HEAD fails -> SKIPPED_UNVERIFIED, never GO."""
        sha = "u" * 40
        branch_fail = MagicMock(returncode=128, stdout="", stderr="fatal: ambiguous")
        mocks = [self._git_head_mock(sha), branch_fail]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks):
            result = check_ci_workflow(tmp_path)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert "could not resolve branch name" in result["detail"]

    def test_ci_succeeded_uses_configured_workflow_name(self, tmp_path):
        """The real-green path still returns GO unchanged, and the gh call
        uses the resolved workflow name (default: DEFAULT_CI_WORKFLOW_NAME).
        """
        sha = "v" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("success", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks) as mock_run:
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == DEFAULT_CI_WORKFLOW_NAME

    def test_workflow_name_explicit_argument_wins(self, tmp_path):
        """An explicit workflow_name argument is used verbatim, not the default."""
        sha = "w" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("success", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks) as mock_run:
            result = check_ci_workflow(tmp_path, workflow_name="CI/CD Pipeline")
        assert result["status"] == "GO"
        assert "CI/CD Pipeline" in result["detail"]
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"

    def test_workflow_name_env_var_override(self, tmp_path, monkeypatch):
        """VNX_CI_WORKFLOW_NAME env var is used when no explicit argument is given."""
        monkeypatch.setenv(CI_WORKFLOW_NAME_ENV_VAR, "CI/CD Pipeline")
        sha = "x" * 40
        mocks = [
            self._git_head_mock(sha),
            self._git_branch_mock(),
            self._gh_run_output("success", sha),
        ]
        with patch("pre_merge_gate.subprocess.run", side_effect=mocks) as mock_run:
            result = check_ci_workflow(tmp_path)
        assert result["status"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"


class TestResolveCIWorkflowName:
    """_resolve_ci_workflow_name(): explicit arg > env var > default."""

    def test_explicit_argument_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(CI_WORKFLOW_NAME_ENV_VAR, "From Env")
        assert _resolve_ci_workflow_name("From Argument") == "From Argument"

    def test_env_var_used_when_no_argument(self, monkeypatch):
        monkeypatch.setenv(CI_WORKFLOW_NAME_ENV_VAR, "From Env")
        assert _resolve_ci_workflow_name(None) == "From Env"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv(CI_WORKFLOW_NAME_ENV_VAR, raising=False)
        assert _resolve_ci_workflow_name(None) == DEFAULT_CI_WORKFLOW_NAME


# ---------------------------------------------------------------------------
# check_pytest — sibling of the OI-1140 ci_workflow bug: the pytest binary
# being unavailable is a tooling failure, not "no tests to run" (tests_found
# stays True), so it must not silently report GO either.
# ---------------------------------------------------------------------------

class TestCheckPytest:

    def test_pytest_not_available_is_unverified(self, project_root):
        (project_root / "tests" / "test_something.py").write_text("def test_x(): assert True\n")
        with patch("pre_merge_gate.subprocess.run", side_effect=FileNotFoundError("pytest not found")):
            result = check_pytest(project_root)
        assert result["status"] == SKIPPED_UNVERIFIED
        assert result["status"] != "GO"
        assert result["tests_found"] is True

    def test_no_tests_dir_is_still_go(self, tmp_path):
        """Contrast case: genuinely nothing to run stays GO, unchanged."""
        result = check_pytest(tmp_path)
        assert result["status"] == "GO"
        assert result["tests_found"] is False
