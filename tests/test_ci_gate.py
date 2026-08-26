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
    """Case A: all checks bucket=pass → status=pass, blocking=[], verdict PASS."""
    executor = _make_mock_executor(gate_env)
    pr_number = 42
    checks = [
        {"name": "ci/test", "bucket": "pass", "state": "SUCCESS"},
        {"name": "ci/lint", "bucket": "pass", "state": "SUCCESS"},
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
    """Case B: 1 bucket=fail check → blocking_findings has 1 entry, status=fail."""
    executor = _make_mock_executor(gate_env)
    pr_number = 43
    checks = [
        {"name": "ci/test", "bucket": "pass", "state": "SUCCESS"},
        {"name": "ci/security", "bucket": "fail", "state": "FAILURE"},
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
    """Case C: one check bucket=pending → status=running, no terminal verdict yet."""
    executor = _make_mock_executor(gate_env)
    pr_number = 44
    checks = [
        {"name": "ci/test", "bucket": "pending", "state": "IN_PROGRESS"},
        {"name": "ci/lint", "bucket": "pass", "state": "SUCCESS"},
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
        # ADR-005: a result without a matching branch is stale evidence and
        # gets rejected by _find_gate_result, regardless of its other fields.
        "branch": "feat/test",
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
        # ADR-005: a result without a matching branch is stale evidence and
        # gets rejected by _find_gate_result, regardless of its other fields.
        "branch": "feat/test",
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
    # gate_status.is_pass() checks `status in FAIL_STATES` before it ever
    # inspects blocking_findings/blocking_count, so an explicit status="fail"
    # always reports as "status: fail" — the blocking_count in this fixture
    # never reaches the detail string. This was true before ADR-005 too; the
    # old "blocking"/"1" assertion never actually observed real behavior
    # because the branch bug always short-circuited to "no ci_gate result
    # found" first.
    assert "status: fail" in ci_check.detail


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
        # ADR-005: a result without a matching branch is stale evidence and
        # gets rejected by _find_gate_result, regardless of its other fields.
        "branch": "feat/test",
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
        # ADR-005: a result without a matching branch is stale evidence and
        # gets rejected by _find_gate_result, regardless of its other fields.
        "branch": "feat/test",
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
        # ADR-005: a result without a matching branch is stale evidence and
        # gets rejected by _find_gate_result, regardless of its other fields.
        "branch": "feat/test",
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


def test_closure_verifier_ci_gate_rejects_result_missing_branch_field(gate_env):
    """ADR-005 pin: a result with no ``branch`` field at all is stale evidence
    and must be rejected the same as a result for a different branch — even
    though every other field (status/contract_hash/report_path) is valid.
    Without this test the five tests above could regress back to writing
    branch-less fixtures and still pass, because they'd merely be asserting
    on whatever detail _find_gate_result happens to produce for "no match"."""
    import closure_verifier as cv
    from review_contract import ReviewContract

    results_dir = gate_env["results_dir"]
    pr_id = "PR-104"
    report_file = gate_env["headless_reports_dir"] / "test-ci_gate-pr-104.md"
    report_file.write_text("# ci_gate report\nStatus: PASS\n", encoding="utf-8")

    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 104,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "abcd1234abcd1234",
        "report_path": str(report_file),
        # Deliberately no "branch" field — this is the stale-evidence shape
        # ADR-005 exists to reject.
    }
    (results_dir / "pr-104-ci_gate.json").write_text(
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
    assert ci_check.detail == "no ci_gate result found"


# ---------------------------------------------------------------------------
# Contract hash determinism
# ---------------------------------------------------------------------------


def test_contract_hash_determinism(gate_env):
    """Contract hash is stable for same inputs across two executions."""
    executor = _make_mock_executor(gate_env)
    pr_number = 50
    head_sha = "deadbeef1234"
    checks = [{"name": "ci/test", "bucket": "pass", "state": "SUCCESS"}]

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
             patch("gate_executor.shutil.which", return_value="/usr/bin/gh"), \
             patch("gate_recorder.get_pr_head_sha", return_value=head_sha):
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


def test_default_review_stack_excludes_ci_gate_when_explicitly_disabled(monkeypatch):
    """ci_gate is excluded from DEFAULT_REVIEW_STACK when VNX_CI_GATE_REQUIRED=0.

    OI-1385: VNX_CI_GATE_REQUIRED's registry default flipped "0" -> "1" (its 5-read-site chain
    proved live on PR #1628, and the obligation-runner's bounded-pending handling for a
    temporarily-unavailable gate is confirmed on main). This pins the explicit-off path,
    replacing the old default-off pin below.
    """
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert "ci_gate" not in stack


def test_default_review_stack_includes_ci_gate_by_default(monkeypatch):
    """OI-1385: VNX_CI_GATE_REQUIRED now defaults to "1" (registry default, config_registry.py)
    -- ci_gate is in DEFAULT_REVIEW_STACK even with no env var set at all."""
    monkeypatch.delenv("VNX_CI_GATE_REQUIRED", raising=False)
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert "ci_gate" in stack


def test_default_review_stack_includes_ci_gate_when_required(monkeypatch):
    """ci_gate IS in DEFAULT_REVIEW_STACK when VNX_CI_GATE_REQUIRED=1."""
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert "ci_gate" in stack


def test_default_review_stack_control_case_gemini_codex_combo_unchanged(monkeypatch):
    """Control case (dispatch 20260823-beta2-e): with no config override, the
    existing gemini_review + codex_gate + claude_github_optional combination
    must come back byte-for-byte unchanged.

    VNX_CI_GATE_REQUIRED is pinned to "0" (not delenv'd) since OI-1385 flipped its
    registry default to "1": this test measures the BASE stack composition, not
    ci_gate's own default, so it must isolate that axis explicitly or it starts
    asserting a gemini/codex/claude_github_optional-only stack that no longer
    matches the wired default.
    """
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    monkeypatch.delenv("VNX_DEFAULT_REVIEW_STACK", raising=False)
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert stack == ["gemini_review", "codex_gate", "claude_github_optional"]


def test_default_review_stack_is_config_driven_not_hardcoded(monkeypatch):
    """OPERATOR-BESLUIT 23-08: kimi_gate/glm_gate must be reachable as the
    default review stack via config alone — no edit to
    _build_default_review_stack() required.

    VNX_CI_GATE_REQUIRED is pinned to "0" (not delenv'd) since OI-1385 flipped its
    registry default to "1": this test measures whether VNX_DEFAULT_REVIEW_STACK
    is honored verbatim, so ci_gate's independent default-on append must be
    isolated out or it would silently ride along on every stack this test builds.
    """
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "kimi_gate,glm_gate")
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert stack == ["kimi_gate", "glm_gate"]


def test_default_review_stack_config_override_still_appends_ci_gate(monkeypatch):
    """ci_gate stays a separate, always-last append gated by
    VNX_CI_GATE_REQUIRED regardless of what VNX_DEFAULT_REVIEW_STACK carries."""
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "kimi_gate,glm_gate")
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
    import review_gate_manager as rgm
    stack = rgm._build_default_review_stack()
    assert stack == ["kimi_gate", "glm_gate", "ci_gate"]


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


# ---------------------------------------------------------------------------
# Finding 1 regression: contract_hash compatibility
# ---------------------------------------------------------------------------


def test_contract_mode_uses_request_contract_hash(gate_env):
    """Finding 1: When request carries contract_hash, result propagates it unchanged.

    closure_verifier compares result.contract_hash to ReviewContract.content_hash.
    In contract-backed mode the request is created with content_hash, so the
    executor must forward it — not overwrite it with a sha256 of execution params.
    """
    executor = _make_mock_executor(gate_env)
    pr_number = 70
    contract_content_hash = "aabbccdd11223344"  # simulates ReviewContract.content_hash
    checks = [{"name": "ci/test", "bucket": "pass", "state": "SUCCESS"}]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
        contract_hash=contract_content_hash,
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="PR-70",
            request_payload=request_payload,
        )

    assert result["status"] == "pass"
    assert result["contract_hash"] == contract_content_hash, (
        "contract_hash in result must equal the contract's content_hash, "
        "not a sha256 of execution params"
    )


def test_legacy_mode_contract_hash_is_sha256_of_execution_params(gate_env):
    """Finding 1 counterpart: legacy mode (no contract_hash in request) still produces sha256 hash."""
    executor = _make_mock_executor(gate_env)
    pr_number = 71
    head_sha = "cafebabe1234"
    checks = [{"name": "ci/test", "bucket": "pass", "state": "SUCCESS"}]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
        # No contract_hash key → legacy mode
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"), \
         patch("gate_recorder.get_pr_head_sha", return_value=head_sha):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks), head_sha=head_sha)
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    expected = hashlib.sha256(
        json.dumps(
            {"gate_name": "ci_gate", "head_sha": head_sha, "pr_number": pr_number},
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    assert result["contract_hash"] == expected


# ---------------------------------------------------------------------------
# Finding 2 regression: contract-scoped ci_gate request/result path
# ---------------------------------------------------------------------------


def test_request_ci_gate_with_contract_creates_contract_file(gate_env, monkeypatch):
    """Finding 2: request_ci_gate_with_contract writes {pr_slug}-ci_gate-contract.json."""
    import review_gate_manager as rgm
    from review_contract import ReviewContract

    manager = rgm.ReviewGateManager()

    contract = ReviewContract(
        pr_id="PR-72",
        branch="feat/test-72",
        risk_class="medium",
        review_stack=["ci_gate"],
        changed_files=[],
        content_hash="deadbeef12345678",
    )

    with patch("gate_request_handler.shutil.which", return_value=None):
        payload = manager.request_ci_gate_with_contract(
            contract=contract,
            pr_number=301,
        )

    request_file = manager.requests_dir / "pr72-ci_gate-contract.json"
    assert request_file.exists(), f"Contract request file missing: {request_file}"
    stored = json.loads(request_file.read_text())
    assert stored["pr_id"] == "PR-72"
    assert stored["pr_number"] == 301
    assert stored["contract_hash"] == "deadbeef12345678"
    assert stored["gate"] == "ci_gate"


def test_request_ci_gate_stamps_requirement_mismatch_onto_obligation(gate_env, monkeypatch):
    """OI-1462: when the vervuller's own resolution of VNX_CI_GATE_REQUIRED
    disagrees with what the obligation's writer (the eiser) stamped at
    registration time, that mismatch must land on the obligation record --
    never a silent skip."""
    import review_gate_manager as rgm
    from gate_obligations import obligation_path, register_obligation

    state_dir = gate_env["state_dir"]
    dispatch_id = "20260826-oi1462-mismatch"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="ci_gate", project_id="vnx-dev",
        gate_requirement_resolution={"VNX_CI_GATE_REQUIRED": True},
    )

    manager = rgm.ReviewGateManager()
    monkeypatch.setattr("config_runtime.get_bool", lambda key: False)

    with patch("gate_request_handler.shutil.which", return_value="/usr/bin/gh"):
        manager._request_ci_gate(
            pr_number=999, branch="feat/test", risk_class="medium",
            changed_files=[], mode="per_pr", dispatch_id=dispatch_id,
        )

    record = json.loads(obligation_path(state_dir, dispatch_id).read_text())
    mismatch = record["gate_requirement_mismatch"]
    assert mismatch["flag"] == "VNX_CI_GATE_REQUIRED"
    assert mismatch["writer_value"] is True
    assert mismatch["reader_value"] is False


def test_request_ci_gate_no_mismatch_when_resolutions_agree(gate_env, monkeypatch):
    """Control case: when both sides agree, no mismatch field is written."""
    import review_gate_manager as rgm
    from gate_obligations import obligation_path, register_obligation

    state_dir = gate_env["state_dir"]
    dispatch_id = "20260826-oi1462-agree"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="ci_gate", project_id="vnx-dev",
        gate_requirement_resolution={"VNX_CI_GATE_REQUIRED": False},
    )

    manager = rgm.ReviewGateManager()
    monkeypatch.setattr("config_runtime.get_bool", lambda key: False)

    with patch("gate_request_handler.shutil.which", return_value="/usr/bin/gh"):
        manager._request_ci_gate(
            pr_number=998, branch="feat/test", risk_class="medium",
            changed_files=[], mode="per_pr", dispatch_id=dispatch_id,
        )

    record = json.loads(obligation_path(state_dir, dispatch_id).read_text())
    assert "gate_requirement_mismatch" not in record


def test_ci_gate_contract_result_discoverable_by_find_gate_result(gate_env):
    """Finding 2: ci_gate result written with pr_id='PR-73' is found by _find_gate_result('ci_gate','PR-73',...)."""
    import closure_verifier as cv

    results_dir = gate_env["results_dir"]
    pr_id = "PR-73"
    report_file = gate_env["headless_reports_dir"] / "test-ci_gate-pr-73.md"
    report_file.write_text("# ci_gate PASS\n", encoding="utf-8")

    # Simulate what _execute_ci_gate writes when called with pr_id="PR-73"
    result_data = {
        "gate": "ci_gate",
        "pr_id": pr_id,
        "pr_number": 301,
        "status": "pass",
        "blocking_count": 0,
        "advisory_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "deadbeef12345678",
        "report_path": str(report_file),
    }
    # Contract-scoped result file: {pr_slug}-ci_gate-contract.json
    (results_dir / "pr73-ci_gate-contract.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    found = cv._find_gate_result("ci_gate", pr_id, results_dir)
    assert found is not None, "_find_gate_result must locate contract-scoped ci_gate result"
    assert found["pr_id"] == pr_id
    assert found["status"] == "pass"


def test_legacy_numeric_pr_id_not_matched_by_canonical_pr_id(gate_env):
    """Finding 2 guard: legacy result with pr_id='301' is NOT matched when searching by 'PR-73'."""
    import closure_verifier as cv

    results_dir = gate_env["results_dir"]
    # Legacy result with numeric pr_id string
    result_data = {
        "gate": "ci_gate",
        "pr_id": "301",  # numeric string — legacy format
        "pr_number": 301,
        "status": "pass",
        "blocking_count": 0,
        "contract_hash": "somevalue",
        "report_path": "",
    }
    (results_dir / "pr-301-ci_gate.json").write_text(
        json.dumps(result_data), encoding="utf-8",
    )

    found = cv._find_gate_result("ci_gate", "PR-73", results_dir)
    assert found is None, (
        "Legacy result with pr_id='301' must NOT match canonical search for 'PR-73'"
    )


# ---------------------------------------------------------------------------
# Finding 3 regression: running verdict → request reset to requested
# ---------------------------------------------------------------------------


def test_running_verdict_resets_request_to_requested(gate_env):
    """Finding 3: when verdict=running, request status reverts to 'requested' for re-execution."""
    executor = _make_mock_executor(gate_env)
    pr_number = 80
    checks = [
        {"name": "ci/test", "bucket": "pending", "state": "IN_PROGRESS"},
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

    # Request file must have been reset to "requested", not "completed"
    request_file = gate_env["requests_dir"] / f"pr-{pr_number}-ci_gate.json"
    assert request_file.exists()
    stored_request = json.loads(request_file.read_text())
    assert stored_request["status"] == "requested", (
        "Request must be reset to 'requested' after running verdict so the gate "
        "can be re-executed once CI checks complete"
    )
    assert "completed_at" not in stored_request, (
        "completed_at must not be written when verdict is 'running'"
    )


def test_completed_verdict_marks_request_completed(gate_env):
    """Finding 3 complement: terminal verdicts (pass/fail) still mark request as completed."""
    executor = _make_mock_executor(gate_env)
    pr_number = 81
    checks = [{"name": "ci/test", "bucket": "pass", "state": "SUCCESS"}]
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(json.dumps(checks))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    request_file = gate_env["requests_dir"] / f"pr-{pr_number}-ci_gate.json"
    stored_request = json.loads(request_file.read_text())
    assert stored_request["status"] == "completed"
    assert "completed_at" in stored_request


# ---------------------------------------------------------------------------
# Finding 4 regression: CLI per-PR mode forwards --branch and --require-github-pr
# ---------------------------------------------------------------------------


def test_cli_per_pr_forwards_branch_and_require_github_pr(gate_env, tmp_path, monkeypatch):
    """Finding 4: --branch and --require-github-pr reach verify_pr_closure when --pr-id is set."""
    import closure_verifier as cv

    # Write a minimal FEATURE_PLAN.md
    feature_plan = tmp_path / "FEATURE_PLAN.md"
    feature_plan.write_text(
        "# Feature: F\n\n**Status**: Active\n**Risk-Class**: medium\n\n"
        "## PR-0: Thing\n**Track**: A\n**Priority**: P1\n**Skill**: @architect\n"
        "**Risk-Class**: medium\n**Merge-Policy**: human\n**Review-Stack**: codex_gate\n"
        "**Dependencies**: []\n\n`gate_pr0_thing`\n\n---\n"
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(tmp_path / "db"))

    captured: dict = {}

    def _fake_verify_pr_closure(**kwargs):
        captured.update(kwargs)
        return {
            "verdict": "fail",
            "mode": "per_pr",
            "pr_id": kwargs["pr_id"],
            "checks": [],
            "reconciled_state": None,
            "review_evidence": None,
        }

    monkeypatch.setattr(cv, "verify_pr_closure", _fake_verify_pr_closure)

    cv.main([
        "--feature-plan", str(feature_plan),
        "--pr-id", "PR-0",
        "--branch", "feat/test-branch",
        "--require-github-pr",
    ])

    assert captured.get("branch") == "feat/test-branch", (
        "--branch must be forwarded to verify_pr_closure"
    )
    assert captured.get("require_github_pr") is True, (
        "--require-github-pr must be forwarded to verify_pr_closure"
    )


# ---------------------------------------------------------------------------
# OI-1321: golden gh 2.76.2 JSON-schema fixtures.
#
# gh 2.76.2 (measured 2026-08-20) rejects `--json name,status,conclusion`
# outright: "Unknown JSON field: status" / "conclusion". The only fields it
# accepts are: bucket, completedAt, description, event, link, name,
# startedAt, state, workflow. `gh pr checks --help` documents `bucket` as
# categorizing the raw `state` into exactly one of: pass, fail, pending,
# skipping, cancel.
#
# These three fixtures are the LITERAL `gh pr checks <pr> --json
# name,state,bucket` output captured against real PRs in this repo on
# 2026-08-20, not hand-written approximations — a hand-written fixture
# could accidentally match whatever field names the parser happens to read,
# which is exactly the failure mode this dispatch exists to close.
# ---------------------------------------------------------------------------

# Captured from `gh pr checks 1617 --json name,state,bucket` — a merged PR,
# every check terminal and bucket=pass.
_GH_2_76_2_GOLDEN_ALL_PASS = [
    {"bucket": "pass", "name": "Profile B (snapshot integration)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Profile C (adoption smoke tests)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Trace Token Validation", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Dashboard TS Lint (fixture-gate + typecheck)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
    {"bucket": "pass", "name": "secret scan (gitleaks)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Gov-D3: Attestation signature gate (advisory)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Dispatch-ID Slug-Match Gate", "state": "SUCCESS"},
    {"bucket": "pass", "name": "docs/core/SUBSYSTEMS.md matches the live registry", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Repo-local state-pin gate (#1043 footgun)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Profile D (pip install smoke)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Profile A (doctor + core tests)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "vnx doctor smoke", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Lint Patterns (silent-except + atomic-write)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Anchor Immutability Check", "state": "SUCCESS"},
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
]

# Captured from `gh pr checks 1613 --json name,state,bucket` — one required
# check failed (bucket=fail), two skipped (bucket=skipping).
_GH_2_76_2_GOLDEN_ONE_FAIL = [
    {"bucket": "skipping", "name": "Profile B (snapshot integration)", "state": "SKIPPED"},
    {"bucket": "skipping", "name": "Profile C (adoption smoke tests)", "state": "SKIPPED"},
    {"bucket": "pass", "name": "secret scan (gitleaks)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Gov-D3: Attestation signature gate (advisory)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Trace Token Validation", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Lint Patterns (silent-except + atomic-write)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Anchor Immutability Check", "state": "SUCCESS"},
    {"bucket": "fail", "name": "Profile A (doctor + core tests)", "state": "FAILURE"},
    {"bucket": "pass", "name": "Dashboard TS Lint (fixture-gate + typecheck)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "vnx doctor smoke", "state": "SUCCESS"},
    {"bucket": "pass", "name": "docs/core/SUBSYSTEMS.md matches the live registry", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Repo-local state-pin gate (#1043 footgun)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Profile D (pip install smoke)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Dispatch-ID Slug-Match Gate", "state": "SUCCESS"},
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
]

# Captured from `gh pr checks 1619 --json name,state,bucket` while two
# workflows were still mid-run — bucket=pending, state=IN_PROGRESS.
_GH_2_76_2_GOLDEN_STILL_RUNNING = [
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Lint Patterns (silent-except + atomic-write)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Repo-local state-pin gate (#1043 footgun)", "state": "SUCCESS"},
    {"bucket": "pending", "name": "Profile A (doctor + core tests)", "state": "IN_PROGRESS"},
    {"bucket": "pass", "name": "Profile D (pip install smoke)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Trace Token Validation", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Anchor Immutability Check", "state": "SUCCESS"},
    {"bucket": "pending", "name": "Dashboard TS Lint (fixture-gate + typecheck)", "state": "IN_PROGRESS"},
    {"bucket": "pass", "name": "Gov-D3: Attestation signature gate (advisory)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "Dispatch-ID Slug-Match Gate", "state": "SUCCESS"},
    {"bucket": "pass", "name": "docs/core/SUBSYSTEMS.md matches the live registry", "state": "SUCCESS"},
    {"bucket": "pass", "name": "secret scan (gitleaks)", "state": "SUCCESS"},
    {"bucket": "pass", "name": "vnx doctor smoke", "state": "SUCCESS"},
    {"bucket": "pass", "name": "ADR-003: No Anthropic SDK Imports", "state": "SUCCESS"},
]


def test_golden_gh_2_76_2_fully_green_pr_gives_pass_with_evidence(gate_env):
    """A fully-green PR against the real gh 2.76.2 schema reaches verdict=pass
    with a non-empty contract_hash AND report_path, and the report file is
    actually on disk — the layer-2 regression this dispatch exists to close
    (a parser reading a vanished `status`/`conclusion` field silently stalls
    at verdict=running forever and never writes a report at all)."""
    executor = _make_mock_executor(gate_env)
    pr_number = 1617
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(
            json.dumps(_GH_2_76_2_GOLDEN_ALL_PASS)
        )
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "pass"
    assert result["contract_hash"] != "", "pass verdict must carry a non-empty contract_hash"
    assert result["report_path"] != "", "pass verdict must carry a non-empty report_path"
    assert Path(result["report_path"]).is_file(), "report must actually exist on disk"
    assert result["blocking_findings"] == []
    assert len(result["passed_checks"]) == len(_GH_2_76_2_GOLDEN_ALL_PASS)


def test_golden_gh_2_76_2_one_failed_check_gives_fail(gate_env):
    """A PR with one bucket=fail check against the real gh 2.76.2 schema
    reaches verdict=fail, with the failing check named in blocking_findings."""
    executor = _make_mock_executor(gate_env)
    pr_number = 1613
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(
            json.dumps(_GH_2_76_2_GOLDEN_ONE_FAIL)
        )
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "fail"
    assert result["blocking_count"] == 1
    assert result["failed_checks"] == ["Profile A (doctor + core tests)"]
    assert result["contract_hash"] != ""
    assert result["report_path"] != ""
    assert Path(result["report_path"]).is_file()
    # The two skipping-bucket checks land as advisory, not blocking.
    assert result["advisory_count"] == 2


def test_golden_gh_2_76_2_pending_check_gives_running_and_no_report(gate_env):
    """A PR with a bucket=pending check against the real gh 2.76.2 schema
    reaches verdict=running and writes NO report — this is exactly the
    silent layer-2 failure mode: before the fix, `status`/`conclusion` are
    always None under the new schema, so verdict is permanently stuck at
    running regardless of what the checks actually say."""
    executor = _make_mock_executor(gate_env)
    pr_number = 1619
    request_payload = _make_request_payload(
        pr_number=pr_number,
        headless_reports_dir=gate_env["headless_reports_dir"],
    )

    with patch("gate_executor.subprocess") as mock_sub, \
         patch("gate_executor.shutil.which", return_value="/usr/bin/gh"):
        mock_sub.run.side_effect = _make_subprocess_run(
            json.dumps(_GH_2_76_2_GOLDEN_STILL_RUNNING)
        )
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        result = executor._execute_ci_gate(
            gate="ci_gate", pr_number=pr_number, pr_id="",
            request_payload=request_payload,
        )

    assert result["status"] == "running"
    assert result["contract_hash"] == ""
    assert result["report_path"] == ""
    # No report file was written anywhere under the reports dir.
    assert list(gate_env["headless_reports_dir"].glob("*.md")) == []
