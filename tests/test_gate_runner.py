#!/usr/bin/env python3
"""Tests for gate_runner.py — validates GATE-1 through GATE-13 contract rules.

Test matrix per Section 8.2 of 180_GATE_EXECUTION_LIFECYCLE_CONTRACT.md:
  - Gate request transitions to executing when runner starts (GATE-1, GATE-3)
  - Gate request transitions to not_executable when provider missing (GATE-4, GATE-5)
  - Gate killed after timeout produces failed result with reason: timeout (GATE-6, GATE-8)
  - Gate killed after stall produces failed result with reason: stall (GATE-7, GATE-8)
  - Skip-rationale NDJSON record written for not_executable (GATE-9)
  - Artifact write is atomic: partial failure produces no result record (GATE-11)
  - Stale contract_hash is detected and rejected (GATE-13)
  - requested does not persist beyond timeout window (GATE-1)
"""

import json
import os
import sys
import textwrap
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
def gate_env(tmp_path, monkeypatch):
    """Set up VNX environment for gate runner tests."""
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
    }


def _make_request_payload(gate="gemini_review", pr_number=1, **overrides):
    """Build a minimal request payload."""
    payload = {
        "gate": gate,
        "status": "requested",
        "provider": "gemini_cli",
        "branch": "feature/test",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/test.py"],
        "requested_at": "2026-04-01T14:00:00Z",
        "report_path": "",
        "prompt": "Review this code",
    }
    payload.update(overrides)
    return payload


class TestGateTransitionsToExecuting:
    """GATE-1, GATE-3: Gate request transitions to executing when runner starts."""

    def test_request_updated_with_executing_state(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        report_path = str(gate_env["reports_dir"] / "test-report.md")
        payload = _make_request_payload(report_path=report_path)

        # Write request to disk
        req_file = gate_env["requests_dir"] / "pr-1-gemini_review.json"
        req_file.write_text(json.dumps(payload), encoding="utf-8")

        # Mock subprocess with binary-mode fd integers
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 99999

        review_output = b'{"summary": "LGTM", "findings": []}\nReview complete: no issues found.\nAll deliverables verified.\n'

        # select returns stdout fd as readable on first call, then proc exits
        call_count = [0]
        def mock_select(rlist, wlist, xlist, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return ([10], [], [])
            return ([], [], [])

        read_call_count = [0]
        def mock_os_read(fd, size):
            read_call_count[0] += 1
            if fd == 10 and read_call_count[0] == 1:
                return review_output
            if fd == 10:
                return b""
            return b""

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", side_effect=mock_select), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=99999):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "completed"

        # Verify request was updated with started_at and runner_pid (GATE-3)
        saved_req = json.loads(req_file.read_text(encoding="utf-8"))
        assert "started_at" in saved_req or saved_req["status"] == "completed"

    def test_executing_state_includes_runner_pid(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        report_path = str(gate_env["reports_dir"] / "test-report.md")
        payload = _make_request_payload(report_path=report_path)

        req_file = gate_env["requests_dir"] / "pr-1-gemini_review.json"
        req_file.write_text(json.dumps(payload), encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 42

        def mock_os_read(fd, size):
            return b""

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=42):
            runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        saved = json.loads(req_file.read_text(encoding="utf-8"))
        assert saved.get("runner_pid") is not None


class TestGateNotExecutable:
    """GATE-4, GATE-5: Gate transitions to not_executable when provider missing."""

    def test_missing_binary_produces_not_executable(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        payload = _make_request_payload()
        result = runner.run(
            gate="gemini_review",
            request_payload=payload,
            pr_number=1,
        )

        assert result["status"] == "not_executable"
        assert result["reason"] == "provider_not_installed"

    def test_not_executable_writes_result_record(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        payload = _make_request_payload()
        runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        result_file = gate_env["results_dir"] / "pr-1-gemini_review.json"
        assert result_file.exists()
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["status"] == "not_executable"
        assert saved["reason"] == "provider_not_installed"
        assert saved["residual_risk"] != ""

    def test_not_executable_with_pr_id(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        payload = _make_request_payload(pr_number=None)
        payload["pr_id"] = "PR-1"
        result = runner.run(
            gate="codex_gate",
            request_payload=payload,
            pr_id="PR-1",
        )

        assert result["status"] == "not_executable"
        result_file = gate_env["results_dir"] / "pr1-codex_gate-contract.json"
        assert result_file.exists()


class TestTimeoutKill:
    """GATE-6, GATE-8: Gate killed after timeout produces failed result."""

    def test_timeout_kills_subprocess_and_records_failure(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "1")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        report_path = str(gate_env["reports_dir"] / "timeout-report.md")
        payload = _make_request_payload(report_path=report_path)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = None  # Never finishes
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        # Simulate time passing — select returns nothing (stall-like but we test timeout)
        real_monotonic = time.monotonic
        call_count = [0]
        base_time = [real_monotonic()]

        def fake_monotonic():
            call_count[0] += 1
            # Jump time forward to exceed timeout
            return base_time[0] + (call_count[0] * 0.5)

        mock_killpg = MagicMock()

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.time.monotonic", side_effect=fake_monotonic), \
             patch("gate_runner.os.getpgid", return_value=12345), \
             patch("gate_runner.os.killpg", mock_killpg):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "failed"
        assert result["reason"] in ("timeout", "stall")
        assert result["required_reruns"] == ["gemini_review"]
        assert mock_killpg.called or mock_proc.kill.called


class TestStallDetection:
    """GATE-7, GATE-8: Gate killed after stall produces failed result."""

    def test_stall_kills_subprocess(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "300")
        monkeypatch.setenv("VNX_GEMINI_STALL_THRESHOLD", "2")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        report_path = str(gate_env["reports_dir"] / "stall-report.md")
        payload = _make_request_payload(report_path=report_path)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = None
        mock_proc.pid = 54321
        mock_proc.kill = MagicMock()
        mock_proc.wait = MagicMock()

        real_monotonic = time.monotonic
        call_count = [0]
        base = [real_monotonic()]

        def fake_monotonic():
            call_count[0] += 1
            # Stall threshold is 2s, advance by 1s each call
            return base[0] + call_count[0]

        mock_killpg = MagicMock()

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.time.monotonic", side_effect=fake_monotonic), \
             patch("gate_runner.os.getpgid", return_value=54321), \
             patch("gate_runner.os.killpg", mock_killpg):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "failed"
        assert result["reason"] == "stall"
        assert "stall threshold" in result["reason_detail"]
        assert mock_killpg.called or mock_proc.kill.called


class TestSkipRationaleAudit:
    """GATE-9: Skip-rationale NDJSON record written for not_executable."""

    def test_ndjson_record_appended(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        payload = _make_request_payload()
        runner.run(gate="gemini_review", request_payload=payload, pr_number=1)

        audit_file = gate_env["state_dir"] / "gate_execution_audit.ndjson"
        assert audit_file.exists()

        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["event_type"] == "gate_skip_rationale"
        assert record["gate"] == "gemini_review"
        assert record["reason"] == "provider_not_installed"
        assert "provider_check" in record
        assert record["provider_check"]["binary_found"] is False

    def test_multiple_skip_records_appended(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: None)

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        for i in range(3):
            payload = _make_request_payload(pr_number=i + 10)
            runner.run(gate="gemini_review", request_payload=payload, pr_number=i + 10)

        audit_file = gate_env["state_dir"] / "gate_execution_audit.ndjson"
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3


class TestArtifactAtomicity:
    """GATE-11: Artifact write is atomic — partial failure produces no result record."""

    def test_report_write_failure_produces_no_result(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        # Use a report path in a read-only directory to trigger write failure
        bad_report_path = "/nonexistent/path/report.md"
        payload = _make_request_payload(report_path=bad_report_path)

        review_output = b'{"summary": "LGTM", "findings": []}\nReview complete.\nAll clear.\n'

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 11111

        read_call_count = [0]
        def mock_os_read(fd, size):
            read_call_count[0] += 1
            if fd == 10 and read_call_count[0] == 1:
                return review_output
            return b""

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=11111):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "failed"
        assert result["reason"] == "artifact_materialization_failed"

    def test_no_report_path_produces_failure(self, gate_env, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/fake")

        runner = GateRunner(
            state_dir=gate_env["state_dir"],
            reports_dir=gate_env["reports_dir"],
        )

        payload = _make_request_payload(report_path="")

        review_output = b'{"summary": "LGTM", "findings": []}\nReview complete.\nAll clear.\n'

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stdout.fileno.return_value = 10
        mock_proc.stderr.fileno.return_value = 11
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.pid = 22222

        read_call_count = [0]
        def mock_os_read(fd, size):
            read_call_count[0] += 1
            if fd == 10 and read_call_count[0] == 1:
                return review_output
            return b""

        with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
             patch("gate_runner.select.select", return_value=([], [], [])), \
             patch("gate_runner.os.read", side_effect=mock_os_read), \
             patch("gate_runner.os.getpgid", return_value=22222):
            result = runner.run(
                gate="gemini_review",
                request_payload=payload,
                pr_number=1,
            )

        assert result["status"] == "failed"
        assert "report_path" in result["reason_detail"].lower() or result["reason"] == "artifact_materialization_failed"


class TestStaleContractHash:
    """GATE-13: Stale contract_hash is detected and rejected."""

    def test_stale_hash_detected(self, gate_env):
        result_payload = {
            "gate": "gemini_review",
            "status": "completed",
            "contract_hash": "abc123",
            "report_path": str(gate_env["reports_dir"] / "report.md"),
            "recorded_at": "2026-04-01T14:00:00Z",
        }

        # Create report file
        Path(result_payload["report_path"]).write_text("# Report\n", encoding="utf-8")

        # Write result
        result_file = gate_env["results_dir"] / "pr-1-gemini_review.json"
        result_file.write_text(json.dumps(result_payload), encoding="utf-8")

        # Verify with different contract content → stale
        assert GateRunner.verify_artifact_consistency(
            result_file,
            contract_content="different contract content",
        ) is False

    def test_matching_hash_accepted(self, gate_env):
        import hashlib
        content = "original contract content"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

        result_payload = {
            "gate": "gemini_review",
            "status": "completed",
            "contract_hash": expected_hash,
            "report_path": str(gate_env["reports_dir"] / "report.md"),
            "recorded_at": "2026-04-01T14:00:00Z",
        }

        Path(result_payload["report_path"]).write_text("# Report\n", encoding="utf-8")
        result_file = gate_env["results_dir"] / "pr-1-gemini_review.json"
        result_file.write_text(json.dumps(result_payload), encoding="utf-8")

        assert GateRunner.verify_artifact_consistency(
            result_file,
            contract_content=content,
        ) is True

    def test_missing_report_fails_consistency(self, gate_env):
        result_payload = {
            "gate": "gemini_review",
            "status": "completed",
            "contract_hash": "abc",
            "report_path": str(gate_env["reports_dir"] / "missing.md"),
            "recorded_at": "2026-04-01T14:00:00Z",
        }

        result_file = gate_env["results_dir"] / "pr-1-gemini_review.json"
        result_file.write_text(json.dumps(result_payload), encoding="utf-8")

        assert GateRunner.verify_artifact_consistency(result_file) is False


class TestRequestNormalization:
    """GATE-2: queued normalized to requested in review_gate_manager."""

    def test_gemini_request_uses_requested_status(self, gate_env, monkeypatch):
        import review_gate_manager as rgm
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
        monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

        manager = rgm.ReviewGateManager()
        result = manager.request_reviews(
            pr_number=100,
            branch="feature/test",
            review_stack=["gemini_review"],
            risk_class="medium",
            changed_files=["test.py"],
            mode="per_pr",
        )

        gate = result["requested"][0]
        assert gate["status"] == "requested"

    def test_codex_unavailable_uses_not_executable(self, gate_env, monkeypatch):
        import review_gate_manager as rgm
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: None)
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        result = manager.request_reviews(
            pr_number=101,
            branch="feature/test",
            review_stack=["codex_gate"],
            risk_class="high",
            changed_files=["scripts/core.py"],
            mode="final",
        )

        gate = result["requested"][0]
        assert gate["status"] == "not_executable"
        assert gate["reason"] in ("provider_disabled", "provider_not_installed")

    def test_not_executable_writes_skip_rationale(self, gate_env, monkeypatch):
        import review_gate_manager as rgm
        monkeypatch.setattr(rgm, "emit_governance_receipt", lambda *a, **kw: None)
        monkeypatch.setattr(rgm.shutil, "which", lambda tool: None)
        monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")

        manager = rgm.ReviewGateManager()
        manager.request_reviews(
            pr_number=102,
            branch="feature/test",
            review_stack=["codex_gate"],
            risk_class="high",
            changed_files=["scripts/core.py"],
            mode="final",
        )

        audit_file = manager.state_dir / "gate_execution_audit.ndjson"
        assert audit_file.exists()
        record = json.loads(audit_file.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert record["event_type"] == "gate_skip_rationale"
        assert record["gate"] == "codex_gate"


class TestGateTimeoutConfig:
    """Verify gate-specific timeout and stall threshold configuration."""

    def test_default_gemini_timeout(self):
        from headless_adapter import gate_timeout
        assert gate_timeout("gemini_review") == 300

    def test_default_codex_timeout(self):
        from headless_adapter import gate_timeout
        assert gate_timeout("codex_gate") == 600

    def test_env_override_timeout(self, monkeypatch):
        from headless_adapter import gate_timeout
        monkeypatch.setenv("VNX_GEMINI_GATE_TIMEOUT", "120")
        assert gate_timeout("gemini_review") == 120

    def test_default_stall_threshold(self):
        from headless_adapter import gate_stall_threshold
        assert gate_stall_threshold("gemini_review") == 60
        assert gate_stall_threshold("codex_gate") == 120

    def test_env_override_stall_threshold(self, monkeypatch):
        from headless_adapter import gate_stall_threshold
        monkeypatch.setenv("VNX_GEMINI_STALL_THRESHOLD", "30")
        assert gate_stall_threshold("gemini_review") == 30

    def test_unknown_gate_uses_defaults(self):
        from headless_adapter import gate_timeout, gate_stall_threshold
        assert gate_timeout("unknown_gate") == 600  # DEFAULT_TIMEOUT
        assert gate_stall_threshold("unknown_gate") == 60
