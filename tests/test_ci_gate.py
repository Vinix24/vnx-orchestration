#!/usr/bin/env python3
"""Tests for ci_gate — GitHub Actions CI audit gate.

Test matrix:
  Case A: all checks pass → status=pass, blocking=[], PASS
  Case B: 1 failed check → blocking has 1, status=fail, FAIL
  Case C: checks running → status=running, no PASS/FAIL yet (incomplete evidence)
  Case D: PR has no checks → status=pass, blocking=[], vacuous PASS
  Closure: verifier rejects ci_gate result with empty report_path or contract_hash
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Set up VNX environment variables for ci_gate tests."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    headless_reports_dir = reports_dir / "headless"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    headless_reports_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_HEADLESS_REPORTS_DIR", str(headless_reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")

    return {
        "project_root": project_root,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "headless_reports_dir": headless_reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_request_payload(pr_number=42, headless_reports_dir=None, **overrides):
    """Build a minimal ci_gate request payload."""
    if headless_reports_dir is None:
        headless_reports_dir = Path("/tmp")
    report_path = str(headless_reports_dir / f"20260428-120000-HEADLESS-ci_gate-pr-{pr_number}.md")
    payload = {
        "gate": "ci_gate",
        "status": "requested",
        "provider": "gh_cli",
        "branch": "feat/test",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": [],
        "requested_at": "2026-04-28T12:00:00Z",
        "report_path": report_path,
    }
    payload.update(overrides)
    return payload


def _make_mock_executor(gate_env):
    """Create a minimal GateExecutorMixin instance with paths set from gate_env."""
    from gate_executor import GateExecutorMixin

    class MockExecutor(GateExecutorMixin):
        requests_dir = gate_env["requests_dir"]
        results_dir = gate_env["results_dir"]
        state_dir = gate_env["state_dir"]
        reports_dir = gate_env["headless_reports_dir"]

    return MockExecutor()


def _gh_checks_response(checks):
    """Build a mock subprocess.CompletedProcess for gh pr checks."""
    return MagicMock(
        returncode=0,
        stdout=json.dumps(checks),
        stderr="",
    )


def _gh_head_sha_response(sha="abc1234def5678"):
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"headRefOid": sha}),
        stderr="",
    )


def _make_subprocess_run(checks_json_str, head_sha="abc1234def5678", checks_returncode=0):
    """Return a side_effect for subprocess.run that handles both gh calls."""
    def _run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "checks" in cmd_str:
            return MagicMock(returncode=checks_returncode, stdout=checks_json_str, stderr="")
        if "headRefOid" in cmd_str:
            return MagicMock(
                returncode=0, stdout=json.dumps({"headRefOid": head_sha}), stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")
    return _run


# ---------------------------------------------------------------------------
# Case A: all checks pass
# ---------------------------------------------------------------------------


def test_case_a_all_checks_pass(gate_env):
    """Case A: all checks COMPLETED/SUCCESS → status=pass, blocking=[], verdict PASS."""
    executor = _make_mock_executor(gate_env)
    pr_number = 42
    checks = [
        {"name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "ci/lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "pass"
    assert result["blocking_findings"] == []
    assert result["blocking_count"] == 0
    assert len(result["passed_checks"]) == 2
    assert result["contract_hash"] != ""
    assert result["report_path"] != ""
    # Report file was written
    assert Path(result["report_path"]).exists()
    # Result JSON was written
    result_file = gate_env["results_dir"] / f"pr-{pr_number}-ci_gate.json"
    assert result_file.exists()
    stored = json.loads(result_file.read_text())
    assert stored["status"] == "pass"
    assert stored["blocking_count"] == 0


# ---------------------------------------------------------------------------
# Case B: 1 failed check → blocking has 1, FAIL
# ---------------------------------------------------------------------------


def test_case_b_one_failed_check(gate_env):
    """Case B: 1 FAILURE check → blocking_findings has 1 entry, status=fail."""
    executor = _make_mock_executor(gate_env)
    pr_number = 43
    checks = [
        {"name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "ci/security", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "fail"
    assert result["blocking_count"] == 1
    assert len(result["blocking_findings"]) == 1
    assert result["blocking_findings"][0]["severity"] == "blocking"
    assert "ci/security" in result["blocking_findings"][0]["title"]
    assert result["failed_checks"] == ["ci/security"]
    # Report should still be written for fail verdict
    assert result["report_path"] != ""
    assert Path(result["report_path"]).exists()
    result_file = gate_env["results_dir"] / f"pr-{pr_number}-ci_gate.json"
    stored = json.loads(result_file.read_text())
    assert stored["status"] == "fail"
    assert stored["blocking_count"] == 1


# ---------------------------------------------------------------------------
# Case C: checks still running → status=running
# ---------------------------------------------------------------------------


def test_case_c_checks_running(gate_env):
    """Case C: checks IN_PROGRESS → status=running, no terminal verdict yet."""
    executor = _make_mock_executor(gate_env)
    pr_number = 44
    checks = [
        {"name": "ci/test", "status": "IN_PROGRESS", "conclusion": None},
        {"name": "ci/lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "running"
    # No terminal verdict: contract_hash and report_path are empty for running
    assert result["contract_hash"] == ""
    assert result["report_path"] == ""
    # Result JSON still written
    result_file = gate_env["results_dir"] / f"pr-{pr_number}-ci_gate.json"
    assert result_file.exists()
    stored = json.loads(result_file.read_text())
    assert stored["status"] == "running"


# ---------------------------------------------------------------------------
# Case D: PR has no checks → vacuous PASS
# ---------------------------------------------------------------------------


def test_case_d_no_checks_vacuous_pass(gate_env):
    """Case D: empty checks list → status=pass, no blocking (vacuous pass)."""
    executor = _make_mock_executor(gate_env)
    pr_number = 45
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        # gh returns empty array — no checks
        mock_sub.run.side_effect = _make_subprocess_run("[]")
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "pass"
    assert result["blocking_findings"] == []
    assert result["blocking_count"] == 0
    assert result["passed_checks"] == []
    assert "vacuous" in result["summary"]


def test_case_d_no_checks_gh_returncode_nonzero_no_checks_message(gate_env):
    """Case D variant: gh exits nonzero with 'no checks' message → vacuous pass."""
    executor = _make_mock_executor(gate_env)
    pr_number = 46
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    def _run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "checks" in cmd_str:
            return MagicMock(returncode=1, stdout="", stderr="no checks reported")
        return MagicMock(returncode=0, stdout=json.dumps({"headRefOid": "abc123"}), stderr="")

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _run
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "pass"
    assert result["blocking_count"] == 0


# ---------------------------------------------------------------------------
# Closure verifier: ci_gate integration
# ---------------------------------------------------------------------------


def test_closure_verifier_ci_gate_pass(gate_env):
    """Closure verifier accepts ci_gate with status=pass, no blocking, valid report."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-99"
    report_file = gate_env["headless_reports_dir"] / "test-ci_gate-pr-99.md"
    report_file.write_text("# ci_gate report\nStatus: PASS\n", encoding="utf-8")

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 99,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "abcd1234abcd1234",
        "report_path": str(report_file),
    }
    (results_dir / "pr-99-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    contract = ReviewContract(
        pr_id=pr_id,
        branch="feat/test",
        review_stack=["ci_gate"],
        risk_class="medium",
        changed_files=[],
        content_hash="",
    )
    checks = cv._validate_review_evidence(contract, results_dir)
    ci_check = next((c for c in checks if c.name == "gate_ci_gate"), None)
    assert ci_check is not None
    assert ci_check.status == "PASS", f"Expected PASS, got {ci_check.status}: {ci_check.detail}"


def test_closure_verifier_ci_gate_fail_one_blocking(gate_env):
    """Closure verifier rejects ci_gate with blocking_count > 0."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-100"
    report_file = gate_env["headless_reports_dir"] / "test-ci_gate-pr-100.md"
    report_file.write_text("# ci_gate report\n[BLOCKING] ci/test failed\n", encoding="utf-8")

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 100,
        "status": "fail",
        "blocking_count": 1,
        "advisory_count": 0,
        "blocking_findings": [{"severity": "blocking", "title": "ci/test", "description": "FAILURE"}],
        "advisory_findings": [],
        "contract_hash": "abcd1234abcd1234",
        "report_path": str(report_file),
    }
    (results_dir / "pr-100-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    contract = ReviewContract(
        pr_id=pr_id,
        branch="feat/test",
        review_stack=["ci_gate"],
        risk_class="medium",
        changed_files=[],
        content_hash="",
    )
    checks = cv._validate_review_evidence(contract, results_dir)
    ci_check = next((c for c in checks if c.name == "gate_ci_gate"), None)
    assert ci_check is not None
    assert ci_check.status == "FAIL"
    assert "blocking" in ci_check.detail.lower() or "1" in ci_check.detail


def test_closure_verifier_ci_gate_running_is_fail(gate_env):
    """Closure verifier rejects ci_gate with status=running (incomplete evidence)."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-101"

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 101,
        "status": "running",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "",
        "report_path": "",
    }
    (results_dir / "pr-101-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    contract = ReviewContract(
        pr_id=pr_id,
        branch="feat/test",
        review_stack=["ci_gate"],
        risk_class="medium",
        changed_files=[],
        content_hash="",
    )
    checks = cv._validate_review_evidence(contract, results_dir)
    ci_check = next((c for c in checks if c.name == "gate_ci_gate"), None)
    assert ci_check is not None
    assert ci_check.status == "FAIL"
    assert "running" in ci_check.detail.lower()


def test_closure_verifier_ci_gate_rejects_empty_report_path(gate_env):
    """Closure verifier rejects ci_gate pass result with missing report_path."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-102"

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 102,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "abcd1234abcd1234",
        "report_path": "",  # empty — should be rejected
    }
    (results_dir / "pr-102-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    contract = ReviewContract(
        pr_id=pr_id,
        branch="feat/test",
        review_stack=["ci_gate"],
        risk_class="medium",
        changed_files=[],
        content_hash="",
    )
    checks = cv._validate_review_evidence(contract, results_dir)
    ci_check = next((c for c in checks if c.name == "gate_ci_gate"), None)
    assert ci_check is not None
    assert ci_check.status == "FAIL"
    assert "report_path" in ci_check.detail


def test_closure_verifier_ci_gate_rejects_empty_contract_hash(gate_env):
    """Closure verifier rejects ci_gate pass result with missing contract_hash."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-103"
    report_file = gate_env["headless_reports_dir"] / "test-ci_gate-pr-103.md"
    report_file.write_text("# ci_gate report\nStatus: PASS\n", encoding="utf-8")

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 103,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "",  # empty — should be rejected
        "report_path": str(report_file),
    }
    (results_dir / "pr-103-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    contract = ReviewContract(
        pr_id=pr_id,
        branch="feat/test",
        review_stack=["ci_gate"],
        risk_class="medium",
        changed_files=[],
        content_hash="",
    )
    checks = cv._validate_review_evidence(contract, results_dir)
    ci_check = next((c for c in checks if c.name == "gate_ci_gate"), None)
    assert ci_check is not None
    assert ci_check.status == "FAIL"
    assert "contract_hash" in ci_check.detail


# ---------------------------------------------------------------------------
# Contract hash determinism
# ---------------------------------------------------------------------------


def test_contract_hash_determinism(gate_env):
    """Contract hash is stable for same inputs across two executions."""
    executor = _make_mock_executor(gate_env)
    pr_number = 50
    head_sha = "deadbeef1234"
    checks = [{"name": "ci/test", "status": "COMPLETED", "conclusion": "SUCCESS"}]

    def _run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "checks" in cmd_str:
            return MagicMock(returncode=0, stdout=json.dumps(checks), stderr="")
        if "headRefOid" in cmd_str:
            return MagicMock(returncode=0, stdout=json.dumps({"headRefOid": head_sha}), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    hashes = []
    for _ in range(2):
        payload = _make_request_payload(
            pr_number=pr_number,
            headless_reports_dir=gate_env["headless_reports_dir"],
        )
        with patch("gate_executor.subprocess") as mock_sub, \
             patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
            mock_sub.run.side_effect = _run
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            result = executor._execute_ci_gate(
                gate="ci_gate", pr_number=pr_number, pr_id="",
                request_payload=payload,
            )
        hashes.append(result["contract_hash"])

    assert hashes[0] == hashes[1], "contract_hash must be deterministic"
    expected = hashlib.sha256(
        json.dumps({"gate_name": "ci_gate", "head_sha": head_sha, "pr_number": pr_number}, sort_keys=True).encode()
    ).hexdigest()[:16]
    assert hashes[0] == expected


# ---------------------------------------------------------------------------
# DEFAULT_REVIEW_STACK env-gating
# ---------------------------------------------------------------------------


def test_default_review_stack_excludes_ci_gate_by_default(monkeypatch):
    """ci_gate is NOT in DEFAULT_REVIEW_STACK unless VNX_CI_GATE_REQUIRED=1."""
    monkeypatch.delenv("VNX_CI_GATE_REQUIRED", raising=False)
    # Force re-evaluation by importing the builder directly
    import importlib
    import review_gate_manager as rgm
    # Temporarily patch the env and call the builder
    stack = rgm._build_default_review_stack()
    assert "ci_gate" not in stack


def test_default_review_stack_includes_ci_gate_when_required(monkeypatch):
    """ci_gate IS in DEFAULT_REVIEW_STACK when VNX_CI_GATE_REQUIRED=1."""
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert "ci_gate" in stack


# ---------------------------------------------------------------------------
# gh not available → not_executable
# ---------------------------------------------------------------------------


def test_gh_not_available_returns_not_executable(gate_env):
    """When gh binary is missing, _execute_ci_gate returns not_executable."""
    executor = _make_mock_executor(gate_env)
    request_payload = _make_request_payload(
        pr_number=60,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.shutil.which", return_value=None):
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=60, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "not_executable"
    assert result["reason"] == "provider_not_installed"
