#!/usr/bin/env python3
"""Certification tests for deterministic gate execution (PR-2).

Validates end-to-end evidence chain: request → execute → report → result → contract_hash.
Goes beyond PR-1 unit tests by testing cross-artifact consistency, full lifecycle
state transitions, and integration-level failure scenarios.

Certification matrix per gate_pr2_gate_execution_certification:
  1. Real gate execution completes without manual intervention (simulated)
  2. Evidence chain verified: request → result → report → contract_hash
  3. Timeout/stall produces structured failure (not silent hang)
  4. Unavailable provider produces skip-rationale record
  5. Artifact stability: report + result + contract_hash all present or all absent
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_runner import GateRunner


@pytest.fixture
def cert_env(tmp_path, monkeypatch):
    """Full VNX environment for certification tests."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))

    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
        "audit_file": state_dir / "gate_execution_audit.ndjson",
    }


def _mock_successful_subprocess():
    """Create a mock subprocess that completes successfully with review output."""
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.stdout.read.return_value = (
        "## Review Summary\n\n"
        "No blocking findings. Code follows project conventions.\n\n"
        "### Findings\n"
        "- [advisory] Consider adding type hints to helper functions\n"
    )
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0
    mock_proc.pid = 99999
    return mock_proc


def _make_request(gate="gemini_review", pr_number=1, pr_id="", **overrides):
    """Build a complete request payload with contract_hash."""
    contract_content = f"Review contract for PR-{pr_number}"
    contract_hash = hashlib.sha256(contract_content.encode("utf-8")).hexdigest()[:16]
    payload = {
        "gate": gate,
        "status": "requested",
        "provider": "gemini_cli",
        "branch": "feature/test-branch",
        "pr_number": pr_number,
        "pr_id": pr_id,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/gate_runner.py", "scripts/lib/headless_adapter.py"],
        "requested_at": "2026-04-01T15:00:00Z",
        "report_path": "",
        "prompt": contract_content,
        "contract_hash": contract_hash,
    }
    payload.update(overrides)
    return payload, contract_content


# =============================================================================
# 1. End-to-End Evidence Chain (Simulated Real Execution)
# =============================================================================


class TestEndToEndEvidenceChain:
    """Certify that the full request → execute → report → result chain works."""

    def test_full_lifecycle_produces_all_artifacts(self, cert_env, monkeypatch):
        """Complete lifecycle: request → executing → completed with all 3 artifacts."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "20260401-150000-HEADLESS-gemini_review-pr-1.md")
        payload, contract_content = _make_request(report_path=report_path)
        contract_hash = payload["contract_hash"]

        # Write initial request
        req_file = cert_env["requests_dir"] / "pr-1-gemini_review.json"
        req_file.write_text(json.dumps(payload), encoding="utf-8")

        mock_proc = _mock_successful_subprocess()
        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        # ARTIFACT 1: Result record exists with terminal status
        assert result["status"] == "completed"
        result_file = cert_env["results_dir"] / "pr-1-gemini_review.json"
        assert result_file.exists()
        saved_result = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved_result["status"] == "completed"

        # ARTIFACT 2: Normalized report exists and is non-empty
        report_file = Path(report_path)
        assert report_file.exists()
        assert report_file.stat().st_size > 0
        report_content = report_file.read_text(encoding="utf-8")
        assert "gemini_review" in report_content

        # ARTIFACT 3: Contract hash preserved through chain
        assert saved_result["contract_hash"] == contract_hash

        # LINKAGE: report_path in result points to actual report
        assert saved_result["report_path"] == report_path
        assert Path(saved_result["report_path"]).exists()

        # LINKAGE: recorded_at timestamp exists
        assert "recorded_at" in saved_result

    def test_request_state_transitions_through_lifecycle(self, cert_env, monkeypatch):
        """Verify request record state transitions: requested → executing → completed."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "lifecycle-report.md")
        payload, _ = _make_request(report_path=report_path)

        req_file = cert_env["requests_dir"] / "pr-1-gemini_review.json"
        req_file.write_text(json.dumps(payload), encoding="utf-8")

        # Capture state at each point
        states_seen = []

        original_persist = runner._persist_request

        def tracking_persist(gate, pl, **kwargs):
            states_seen.append(pl.get("status"))
            original_persist(gate, pl, **kwargs)

        runner._persist_request = tracking_persist

        mock_proc = _mock_successful_subprocess()
        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])):
            runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        # Must see executing then completed (requested is the initial state before run)
        assert "executing" in states_seen
        assert "completed" in states_seen
        # executing must come before completed
        exec_idx = states_seen.index("executing")
        comp_idx = states_seen.index("completed")
        assert exec_idx < comp_idx

    def test_evidence_chain_with_pr_id_format(self, cert_env, monkeypatch):
        """Verify evidence chain works with PR-ID format (contract-based path)."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "pr-id-report.md")
        payload, _ = _make_request(pr_number=None, pr_id="PR-5", report_path=report_path)

        mock_proc = _mock_successful_subprocess()
        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])):
            result = runner.run(
                gate="gemini_review", request_payload=payload,
                pr_id="PR-5",
            )

        assert result["status"] == "completed"
        # Contract-based path uses pr_id slug
        result_file = cert_env["results_dir"] / "pr5-gemini_review-contract.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["pr_id"] == "PR-5"


# =============================================================================
# 2. Timeout/Stall Produces Structured Failure
# =============================================================================


class TestTimeoutStallStructuredFailure:
    """Certify that timeout/stall kills produce complete structured failure records."""

    def test_timeout_failure_has_all_required_fields(self, cert_env, monkeypatch):
        """Timeout failure record must have all fields needed for T0 reasoning."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "1")
        monkeypatch.setenv("VNX_GEMINI_STALL_THRESHOLD", "300")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "timeout-cert.md")
        payload, _ = _make_request(report_path=report_path)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        real_mono = time.monotonic
        call_count = [0]
        base = [real_mono()]

        def fake_mono():
            call_count[0] += 1
            return base[0] + (call_count[0] * 0.5)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.time.monotonic", side_effect=fake_mono):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        assert result["status"] == "failed"
        assert result["reason"] in ("timeout", "stall")

        # Structured failure fields for T0
        assert "reason_detail" in result
        assert "duration_seconds" in result
        assert "runner_pid" in result
        assert "required_reruns" in result
        assert result["required_reruns"] == ["gemini_review"]
        assert "residual_risk" in result
        assert result["report_path"] == ""  # No report on failure

        # Result file written to disk
        result_file = cert_env["results_dir"] / "pr-1-gemini_review.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["status"] == "failed"

        # Request updated to failed
        req_file = cert_env["requests_dir"] / "pr-1-gemini_review.json"
        assert req_file.exists()
        req = json.loads(req_file.read_text(encoding="utf-8"))
        assert req["status"] == "failed"

    def test_stall_failure_distinct_from_timeout(self, cert_env, monkeypatch):
        """Stall failure must be distinguishable from timeout in the result."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "300")
        monkeypatch.setenv("VNX_GEMINI_STALL_THRESHOLD", "1")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "stall-cert.md")
        payload, _ = _make_request(report_path=report_path, pr_number=2)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 54321
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        real_mono = time.monotonic
        call_count = [0]
        base = [real_mono()]

        def fake_mono():
            call_count[0] += 1
            return base[0] + call_count[0]

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.time.monotonic", side_effect=fake_mono):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=2)

        assert result["status"] == "failed"
        assert result["reason"] == "stall"
        assert "stall threshold" in result["reason_detail"]

    def test_nonzero_exit_code_produces_failure(self, cert_env, monkeypatch):
        """Subprocess exit != 0 must produce structured failure, not silent pass."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "exit-code-cert.md")
        payload, _ = _make_request(report_path=report_path, pr_number=3)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.read.return_value = "partial output"
        mock_proc.stderr.read.return_value = "error: auth failed"
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        mock_proc.pid = 77777

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=3)

        assert result["status"] == "failed"
        assert result["reason"] == "exit_nonzero"
        assert "code 1" in result["reason_detail"]


# =============================================================================
# 3. Unavailable Provider Skip-Rationale
# =============================================================================


class TestSkipRationaleCompleteness:
    """Certify that unavailable providers produce complete, auditable skip records."""

    def test_skip_record_has_provider_check_detail(self, cert_env, monkeypatch):
        """Skip-rationale must include binary_found, env_flag, env_value."""
        monkeypatch.setattr("shutil.which", lambda b: None)
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        payload, _ = _make_request(gate="codex_gate")
        runner.run(gate="codex_gate", request_payload=payload, pr_number=1)

        audit_file = cert_env["audit_file"]
        assert audit_file.exists()
        record = json.loads(audit_file.read_text(encoding="utf-8").strip().split("\n")[-1])

        assert record["event_type"] == "gate_skip_rationale"
        assert record["gate"] == "codex_gate"
        assert record["reason"] == "provider_not_installed"
        assert "provider_check" in record

        check = record["provider_check"]
        assert check["binary_name"] == "codex"
        assert check["binary_found"] is False
        assert check["env_flag"] == "VNX_CODEX_HEADLESS_ENABLED"
        assert check["env_value"] == "0"
        assert "compensating_action" in record
        assert "timestamp" in record

    def test_skip_rationale_for_each_gate_type(self, cert_env, monkeypatch):
        """Every gate type produces skip record with correct binary and env flag."""
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        gates = ["gemini_review", "codex_gate", "claude_github_optional"]
        expected_binaries = ["gemini", "codex", "gh"]
        expected_env_flags = [
            "VNX_GEMINI_REVIEW_ENABLED",
            "VNX_CODEX_HEADLESS_ENABLED",
            "VNX_CLAUDE_GITHUB_REVIEW_ENABLED",
        ]

        for i, gate in enumerate(gates):
            payload, _ = _make_request(gate=gate, pr_number=10 + i)
            runner.run(gate=gate, request_payload=payload, pr_number=10 + i)

        audit_file = cert_env["audit_file"]
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["gate"] == gates[i]
            assert record["provider_check"]["binary_name"] == expected_binaries[i]
            assert record["provider_check"]["env_flag"] == expected_env_flags[i]

    def test_not_executable_result_and_request_both_written(self, cert_env, monkeypatch):
        """Both request and result records updated on not_executable."""
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        payload, _ = _make_request(gate="gemini_review", pr_number=20)
        runner.run(gate="gemini_review", request_payload=payload, pr_number=20)

        req_file = cert_env["requests_dir"] / "pr-20-gemini_review.json"
        result_file = cert_env["results_dir"] / "pr-20-gemini_review.json"

        assert req_file.exists()
        assert result_file.exists()

        req = json.loads(req_file.read_text(encoding="utf-8"))
        res = json.loads(result_file.read_text(encoding="utf-8"))

        assert req["status"] == "not_executable"
        assert req["reason"] == "provider_not_installed"
        assert "resolved_at" in req

        assert res["status"] == "not_executable"
        assert res["reason"] == "provider_not_installed"
        assert res["residual_risk"] != ""
        assert res["report_path"] == ""  # No report for not_executable


# =============================================================================
# 4. Artifact Stability (All-or-Nothing)
# =============================================================================


class TestArtifactStability:
    """Certify all-or-nothing materialization: 3 artifacts present or 0."""

    def test_successful_execution_has_all_three_artifacts(self, cert_env, monkeypatch):
        """Completed gate must have: report file, result JSON, contract_hash."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "artifact-cert-report.md")
        payload, contract_content = _make_request(report_path=report_path, pr_number=30)

        mock_proc = _mock_successful_subprocess()
        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=30)

        assert result["status"] == "completed"

        # All 3 artifacts present
        assert Path(result["report_path"]).exists()
        assert result["contract_hash"] != ""
        result_file = cert_env["results_dir"] / "pr-30-gemini_review.json"
        assert result_file.exists()

        # Consistency check passes
        assert GateRunner.verify_artifact_consistency(
            result_file, contract_content=contract_content,
        ) is True

    def test_failed_execution_has_no_orphan_report(self, cert_env, monkeypatch):
        """Failed gate must not leave an orphan report without a completed result."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "1")
        monkeypatch.setenv("VNX_GEMINI_STALL_THRESHOLD", "300")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "orphan-test-report.md")
        payload, _ = _make_request(report_path=report_path, pr_number=31)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 88888
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        real_mono = time.monotonic
        call_count = [0]
        base = [real_mono()]

        def fake_mono():
            call_count[0] += 1
            return base[0] + (call_count[0] * 0.5)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.time.monotonic", side_effect=fake_mono):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=31)

        assert result["status"] == "failed"
        # No report should exist for a timeout failure
        assert not Path(report_path).exists()
        # Result file exists but status is failed
        result_file = cert_env["results_dir"] / "pr-31-gemini_review.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["status"] == "failed"
        assert saved["report_path"] == ""

    def test_result_rollback_on_write_failure(self, cert_env, monkeypatch):
        """If result JSON write fails after report write, report must be cleaned up.

        Finding: _record_failure also writes to results dir, so when results dir
        is fully unwritable, the failure record itself propagates the OSError.
        This test validates the rollback path within _materialize_artifacts
        by making only the first result write fail (the completed record),
        while allowing the subsequent failure record write.
        """
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        report_path = str(cert_env["reports_dir"] / "rollback-test.md")
        payload, _ = _make_request(report_path=report_path, pr_number=32)

        mock_proc = _mock_successful_subprocess()

        # Track write_text calls to fail only the first result write (the
        # completed record), but allow the second write (the failure record).
        original_write = Path.write_text
        result_write_count = [0]

        def selective_write(self_path, content, **kwargs):
            if "results" in str(self_path) and "pr-32" in str(self_path):
                result_write_count[0] += 1
                if result_write_count[0] == 1:
                    raise OSError("Permission denied")
            return original_write(self_path, content, **kwargs)

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch.object(Path, "write_text", selective_write):
            result = runner.run(gate="gemini_review", request_payload=payload, pr_number=32)

        assert result["status"] == "failed"
        assert result["reason"] == "artifact_materialization_failed"
        # Report should be cleaned up (rolled back) — GATE-11
        assert not Path(report_path).exists()


# =============================================================================
# 5. Cross-Gate Consistency
# =============================================================================


class TestCrossGateConsistency:
    """Certify that multiple gates for the same PR produce independent, consistent evidence."""

    def test_multiple_gates_same_pr_independent(self, cert_env, monkeypatch):
        """Two gates for the same PR produce separate, non-interfering artifacts."""
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=cert_env["state_dir"],
            reports_dir=cert_env["reports_dir"],
        )

        gates_results = {}
        for gate in ["gemini_review", "codex_gate"]:
            report_path = str(cert_env["reports_dir"] / f"cross-{gate}.md")
            payload, _ = _make_request(gate=gate, report_path=report_path, pr_number=40)

            mock_proc = _mock_successful_subprocess()
            with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
                 patch("gate_runner.select.select", return_value=([], [], [])):
                result = runner.run(gate=gate, request_payload=payload, pr_number=40)

            gates_results[gate] = result

        # Both completed independently
        assert gates_results["gemini_review"]["status"] == "completed"
        assert gates_results["codex_gate"]["status"] == "completed"

        # Separate result files
        gemini_result = cert_env["results_dir"] / "pr-40-gemini_review.json"
        codex_result = cert_env["results_dir"] / "pr-40-codex_gate.json"
        assert gemini_result.exists()
        assert codex_result.exists()

        # Separate report files
        gemini_report = json.loads(gemini_result.read_text())["report_path"]
        codex_report = json.loads(codex_result.read_text())["report_path"]
        assert gemini_report != codex_report
        assert Path(gemini_report).exists()
        assert Path(codex_report).exists()
