#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import review_gate_manager as rgm


@pytest.fixture
def review_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(data_dir / "unified_reports" / "headless"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


def test_request_reviews_queues_gemini_and_skips_unconfigured_optional(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    result = manager.request_reviews(
        pr_number=12,
        branch="feature/demo",
        review_stack=["gemini_review", "claude_github_optional"],
        risk_class="medium",
        changed_files=["docs/guide.md"],
        mode="per_pr",
    )

    requested = {item["gate"]: item for item in result["requested"]}
    assert requested["gemini_review"]["status"] == "requested"
    assert requested["gemini_review"]["report_path"].startswith(str((review_env / ".vnx-data" / "unified_reports").resolve()))
    assert requested["claude_github_optional"]["status"] == "not_configured"
    assert (manager.requests_dir / "pr-12-gemini_review.json").exists()


def test_codex_final_gate_blocks_when_required_but_not_available(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: None)
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    result = manager.request_reviews(
        pr_number=44,
        branch="feature/runtime-core",
        review_stack=["codex_gate"],
        risk_class="high",
        changed_files=["scripts/pr_queue_manager.py"],
        mode="final",
    )

    gate = result["requested"][0]
    assert gate["gate"] == "codex_gate"
    assert gate["status"] == "not_executable"
    assert gate["required"] is True


def test_record_result_persists_structured_review_output(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()
    report_file = review_env / ".vnx-data" / "unified_reports" / "manual-gemini-report.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# Gemini report\n", encoding="utf-8")
    report_path = str(report_file.resolve())

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=8,
        branch="feature/docs",
        status="pass",
        summary="No blocking findings",
        findings=[{"severity": "info", "title": "Minor wording"}],
        residual_risk="low",
        contract_hash="hash-123",
        report_path=report_path,
    )

    result_path = manager.results_dir / "pr-8-gemini_review.json"
    assert result_path.exists()
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["status"] == "pass"
    assert saved["findings"][0]["title"] == "Minor wording"
    assert payload["residual_risk"] == "low"
    assert saved["contract_hash"] == "hash-123"
    assert saved["report_path"] == report_path


def test_record_result_canonicalizes_relative_report_path(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    report_rel = ".vnx-data/unified_reports/review.md"
    report_file = review_env / ".vnx-data" / "unified_reports" / "review.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("# Review report\n", encoding="utf-8")
    payload = manager.record_result(
        gate="codex_gate",
        pr_number=9,
        branch="feature/docs",
        status="pass",
        summary="No blocking findings",
        contract_hash="hash-456",
        report_path=report_rel,
    )

    expected = str((review_env / report_rel).resolve())
    saved = json.loads((manager.results_dir / "pr-9-codex_gate.json").read_text(encoding="utf-8"))
    assert payload["report_path"] == expected
    assert saved["report_path"] == expected


def test_record_result_uses_request_report_path_when_report_path_omitted(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

    manager = rgm.ReviewGateManager()
    requested = manager.request_reviews(
        pr_number=17,
        branch="feature/runtime",
        review_stack=["gemini_review"],
        risk_class="high",
        changed_files=["scripts/runtime.py"],
        mode="per_pr",
    )["requested"][0]

    # Create the report file that the request reserved
    Path(requested["report_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(requested["report_path"]).write_text("# Gemini headless report\n", encoding="utf-8")

    payload = manager.record_result(
        gate="gemini_review",
        pr_number=17,
        branch="feature/runtime",
        status="pass",
        summary="No blocking findings",
        contract_hash="hash-789",
    )

    assert payload["report_path"] == requested["report_path"]


def test_record_result_rejects_pass_without_contract_hash(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    with pytest.raises(ValueError, match="contract_hash is required"):
        manager.record_result(
            gate="gemini_review",
            pr_number=21,
            branch="feature/docs",
            status="pass",
            summary="No blocking findings",
            report_path=str((review_env / ".vnx-data" / "unified_reports" / "gate.md").resolve()),
        )


def test_record_result_rejects_pass_without_report_path_or_request(review_env, monkeypatch):
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    manager = rgm.ReviewGateManager()

    with pytest.raises(ValueError, match="report_path is required"):
        manager.record_result(
            gate="codex_gate",
            pr_number=22,
            branch="feature/runtime",
            status="pass",
            summary="No blocking findings",
            contract_hash="hash-999",
        )


def test_changed_files_auto_computed_from_branch(review_env, monkeypatch):
    """When --changed-files is empty and --branch is set, git diff is invoked."""
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

    captured_changed_files: list = []

    def fake_request_reviews(self, pr_number, branch, review_stack, risk_class, changed_files, mode, dispatch_id=""):
        captured_changed_files.extend(changed_files)
        return {"requested": []}

    git_calls: list = []

    def fake_run(cmd, **kwargs):
        git_calls.append(cmd)

        class FakeProc:
            returncode = 0
            stdout = "scripts/foo.py\nscripts/bar.py\n"

        return FakeProc()

    monkeypatch.setattr(rgm.ReviewGateManager, "request_reviews", fake_request_reviews)
    monkeypatch.setattr(rgm.subprocess, "run", fake_run)

    argv = [
        "request",
        "--pr", "99",
        "--branch", "feature/auto-scope",
        "--changed-files", "",
    ]
    rgm.main(argv)

    assert any("git" in str(c) for c in git_calls), "git diff should have been called"
    assert "scripts/foo.py" in captured_changed_files
    assert "scripts/bar.py" in captured_changed_files


def test_changed_files_override_preserves_explicit(review_env, monkeypatch):
    """When --changed-files is provided, no auto-compute git call is made."""
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

    captured_changed_files: list = []

    def fake_request_reviews(self, pr_number, branch, review_stack, risk_class, changed_files, mode, dispatch_id=""):
        captured_changed_files.extend(changed_files)
        return {"requested": []}

    git_calls: list = []

    def fake_run(cmd, **kwargs):
        git_calls.append(cmd)

        class FakeProc:
            returncode = 0
            stdout = "should/not/appear.py\n"

        return FakeProc()

    monkeypatch.setattr(rgm.ReviewGateManager, "request_reviews", fake_request_reviews)
    monkeypatch.setattr(rgm.subprocess, "run", fake_run)

    argv = [
        "request",
        "--pr", "100",
        "--branch", "feature/explicit-files",
        "--changed-files", "a.py,b.py",
    ]
    rgm.main(argv)

    diff_calls = [c for c in git_calls if "diff" in c and "--name-only" in c]
    assert len(diff_calls) == 0, "git diff --name-only should NOT be called when --changed-files is explicit"
    assert "a.py" in captured_changed_files
    assert "b.py" in captured_changed_files
    assert "should/not/appear.py" not in captured_changed_files


def test_compute_changed_files_raises_value_error_best_effort(review_env, monkeypatch):
    """_compute_changed_files raises ValueError (best-effort mode) when both bases fail."""
    import subprocess as sp

    def fake_run_fail(cmd, **kwargs):
        raise sp.CalledProcessError(128, cmd)

    monkeypatch.setattr(rgm.subprocess, "run", fake_run_fail)

    with pytest.raises(ValueError, match="cannot auto-compute"):
        rgm._compute_changed_files("feature/broken-branch")


def test_compute_changed_files_strict_raises_runtime_error(review_env, monkeypatch):
    """_compute_changed_files raises RuntimeError in strict (review-gate) mode when both bases fail."""
    import subprocess as sp

    def fake_run_fail(cmd, **kwargs):
        raise sp.CalledProcessError(128, cmd)

    monkeypatch.setattr(rgm.subprocess, "run", fake_run_fail)

    with pytest.raises(RuntimeError, match="cannot auto-compute"):
        rgm._compute_changed_files("feature/broken-branch", strict=True)


def test_compute_changed_files_strict_returns_files_on_success(review_env, monkeypatch):
    """_compute_changed_files with strict=True returns file list when git succeeds."""
    import subprocess as sp

    class FakeProc:
        returncode = 0
        stdout = "scripts/foo.py\nscripts/bar.py\n"

    def fake_run_ok(cmd, **kwargs):
        return FakeProc()

    monkeypatch.setattr(rgm.subprocess, "run", fake_run_ok)

    files = rgm._compute_changed_files("feature/ok-branch", strict=True)
    assert files == ["scripts/foo.py", "scripts/bar.py"]


def test_main_handles_compute_failure_gracefully(review_env, monkeypatch):
    """main exits with code 2 and helpful message when auto-compute fails."""
    import subprocess as sp

    def fake_run_fail(cmd, **kwargs):
        raise sp.CalledProcessError(128, cmd)

    monkeypatch.setattr(rgm.subprocess, "run", fake_run_fail)
    monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *args, **kwargs: None)

    exit_code = rgm.main([
        "request", "--pr", "55", "--branch", "feature/broken-refs", "--changed-files", "",
    ])
    assert exit_code == 2


def test_compute_changed_files_fetch_called_before_diff(monkeypatch):
    """fetch origin/<branch> is the first git call; diff uses origin/main...origin/<branch>."""
    call_log = []

    class FakeProc:
        returncode = 0
        stdout = "scripts/fix.py\n"

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(rgm.subprocess, "run", fake_run)

    files = rgm._compute_changed_files("feature/my-branch")

    assert call_log[0] == ["git", "fetch", "origin", "feature/my-branch"], \
        "first call must be git fetch origin <branch>"
    diff_calls = [c for c in call_log if "diff" in c]
    assert diff_calls, "a diff call must follow the fetch"
    assert "origin/main...origin/feature/my-branch" in diff_calls[0], \
        "diff must use origin/main...origin/<branch>"
    assert files == ["scripts/fix.py"]


def test_compute_changed_files_fetch_failure_is_nonfatal(monkeypatch):
    """If git fetch fails, diff is still attempted and returns files on success."""
    import subprocess as sp

    fetch_raised = []

    class FakeProc:
        returncode = 0
        stdout = "scripts/fixed.py\n"

    def fake_run(cmd, **kwargs):
        if "fetch" in cmd:
            fetch_raised.append(True)
            raise sp.CalledProcessError(1, cmd)
        return FakeProc()

    monkeypatch.setattr(rgm.subprocess, "run", fake_run)

    files = rgm._compute_changed_files("feature/fetch-fail")
    assert fetch_raised, "fetch was attempted and failed"
    assert files == ["scripts/fixed.py"], "diff still succeeded after fetch failure"


def test_compute_changed_files_real_repo(tmp_path, monkeypatch):
    """Real git repo: auto-compute fetches and diffs against origin/main correctly
    when the branch is not checked out locally (simulates worktree with pinned main)."""
    import subprocess as sp

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def rungit(args, *, cwd):
        sp.run(["git"] + args, cwd=str(cwd), check=True, capture_output=True, env=git_env)

    # Set up origin repo
    origin = tmp_path / "origin"
    origin.mkdir()
    rungit(["init"], cwd=origin)
    rungit(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin)
    rungit(["config", "receive.denyCurrentBranch", "ignore"], cwd=origin)
    (origin / "base.py").write_text("x = 1\n")
    rungit(["add", "base.py"], cwd=origin)
    rungit(["commit", "-m", "initial"], cwd=origin)

    # Clone local before the feature branch exists — it has no origin/feature/test-scope
    local = tmp_path / "local"
    rungit(["clone", str(origin), str(local)], cwd=tmp_path)

    # Now add the feature branch to origin (after clone, so local doesn't have it)
    rungit(["checkout", "-b", "feature/test-scope"], cwd=origin)
    (origin / "changed.py").write_text("y = 2\n")
    rungit(["add", "changed.py"], cwd=origin)
    rungit(["commit", "-m", "feat: add changed"], cwd=origin)
    rungit(["checkout", "main"], cwd=origin)

    monkeypatch.chdir(local)

    files = rgm._compute_changed_files("feature/test-scope")
    assert "changed.py" in files
    assert "base.py" not in files
