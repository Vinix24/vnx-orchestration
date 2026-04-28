"""GATE-11 atomicity: orphan report cleanup on validation failure.

Verifies that materialize_artifacts deletes the report file before recording
a failed result when _validate_report_file or _validate_content fails.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from gate_artifacts import materialize_artifacts


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": requests_dir,
        "results_dir": results_dir,
    }


def _run(env, stdout="Review line one.\nReview line two.\nReview line three.\n", gate="gemini_review"):
    report_file = env["reports_dir"] / "test-report.md"
    payload = {
        "gate": gate,
        "status": "requested",
        "provider": "gemini",
        "branch": "feat/test",
        "pr_number": 42,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/foo.py"],
        "requested_at": "20260428T100000Z",
        "prompt": "Review this code",
        "dispatch_id": "test-atomicity-dispatch",
        "report_path": str(report_file),
    }
    result = materialize_artifacts(
        gate=gate,
        pr_number=42,
        pr_id="",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.0,
        requests_dir=env["requests_dir"],
        results_dir=env["results_dir"],
        reports_dir=env["reports_dir"],
    )
    return result, report_file


class TestGate11Atomicity:

    def test_sparse_stdout_no_orphan_report(self, env):
        """empty_review_content failure must delete report before recording failure."""
        sparse_stdout = "Only one line."
        result, report_file = _run(env, stdout=sparse_stdout)

        assert result["status"] == "failed", f"Expected failed, got: {result}"
        assert result["reason"] == "empty_review_content"
        assert not report_file.exists(), (
            "GATE-11 violation: orphan report file left on disk after validation failure"
        )

    def test_validate_report_file_failure_no_orphan(self, env):
        """_validate_report_file returning an error must delete report before recording failure."""
        result, report_file = _run(env)

        # Force _validate_report_file to fail by patching it to always error
        report_file2 = env["reports_dir"] / "test-report2.md"
        payload = {
            "gate": "gemini_review",
            "status": "requested",
            "provider": "gemini",
            "branch": "feat/test",
            "pr_number": 99,
            "review_mode": "per_pr",
            "risk_class": "medium",
            "changed_files": [],
            "requested_at": "20260428T100000Z",
            "prompt": "Review this code",
            "dispatch_id": "test-atomicity-dispatch2",
            "report_path": str(report_file2),
        }
        good_stdout = "Line one.\nLine two.\nLine three.\n"

        with patch("gate_artifacts._validate_report_file", return_value="Report file is empty or missing after write"):
            result2 = materialize_artifacts(
                gate="gemini_review",
                pr_number=99,
                pr_id="",
                stdout=good_stdout,
                request_payload=payload,
                duration_seconds=1.0,
                requests_dir=env["requests_dir"],
                results_dir=env["results_dir"],
                reports_dir=env["reports_dir"],
            )

        assert result2["status"] == "failed"
        assert not report_file2.exists(), (
            "GATE-11 violation: orphan report left after _validate_report_file failure"
        )

    def test_validation_pass_report_kept(self, env):
        """When validation succeeds, the report file must remain on disk."""
        good_stdout = "Line one.\nLine two.\nLine three.\n"
        result, report_file = _run(env, stdout=good_stdout)

        assert result["status"] == "completed"
        assert report_file.exists(), "Report file must exist after successful materialization"

    def test_sparse_stdout_failure_reason_codes(self, env):
        """Confirm reason/reason_detail are populated correctly for sparse output."""
        result, _ = _run(env, stdout="Short.")

        assert result["reason"] == "empty_review_content"
        assert "substantive line" in result.get("reason_detail", "")
