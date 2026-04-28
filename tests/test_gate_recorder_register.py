"""Tests for gate_recorder.record_failure → dispatch_register emit (codex_gate only)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_recorder import record_failure


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def recorder_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)
    # Redirect register writes to tmp_path
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {
        "state_dir": state_dir,
        "requests_dir": requests_dir,
        "results_dir": results_dir,
    }


def _make_result():
    return {
        "reason": "timeout",
        "reason_detail": "Gate timed out after 300s",
        "duration_seconds": 300.0,
        "partial_output_lines": 5,
        "runner_pid": os.getpid(),
    }


def _make_payload(gate="codex_gate", pr_number=42, pr_id="", dispatch_id="test-dispatch-pr4b4"):
    base = {
        "gate": gate,
        "status": "requested",
        "branch": "feat/test",
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
    }
    if pr_id:
        base["pr_id"] = pr_id
    return base


def _read_register_events(env) -> list[dict]:
    reg = env["state_dir"] / "dispatch_register.ndjson"
    if not reg.exists():
        return []
    return [json.loads(ln) for ln in reg.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGateRecorderRegisterEmit:

    def test_record_failure_codex_timeout_no_register_emit(self, recorder_env):
        """record_failure for codex_gate with reason=timeout must NOT emit to register.

        Timeouts are infrastructure failures, not gate verdicts. Emitting gate_failed
        would abuse the 'gate completed with blocking findings' semantic.
        """
        payload = _make_payload(gate="codex_gate", pr_number=99)
        result = record_failure(
            gate="codex_gate",
            pr_number=99,
            pr_id="",
            result=_make_result(),  # reason="timeout"
            request_payload=payload,
            requests_dir=recorder_env["requests_dir"],
            results_dir=recorder_env["results_dir"],
        )

        assert result["status"] == "failed"
        events = _read_register_events(recorder_env)
        assert events == [], f"Expected no register events for timeout; got: {events}"

    def test_record_failure_codex_non_execution_reason_emits_gate_failed(self, recorder_env):
        """record_failure for codex_gate with a non-execution reason emits gate_failed.

        When the reason is not an infrastructure failure (e.g. an explicit review
        verdict that triggered record_failure), gate_failed must be emitted.
        """
        payload = _make_payload(gate="codex_gate", pr_number=99)
        result = record_failure(
            gate="codex_gate",
            pr_number=99,
            pr_id="",
            result={
                "reason": "review_verdict_blocked",
                "reason_detail": "Codex explicitly blocked the gate",
                "duration_seconds": 12.0,
                "partial_output_lines": 8,
                "runner_pid": os.getpid(),
            },
            request_payload=payload,
            requests_dir=recorder_env["requests_dir"],
            results_dir=recorder_env["results_dir"],
        )

        assert result["status"] == "failed"
        events = _read_register_events(recorder_env)
        assert len(events) == 1
        assert events[0]["event"] == "gate_failed"
        assert events[0]["gate"] == "codex_gate"

    def test_record_failure_gemini_no_register_entry(self, recorder_env):
        """record_failure for gemini_review must NOT emit any register event."""
        payload = _make_payload(gate="gemini_review", pr_number=99)
        result = record_failure(
            gate="gemini_review",
            pr_number=99,
            pr_id="",
            result=_make_result(),
            request_payload=payload,
            requests_dir=recorder_env["requests_dir"],
            results_dir=recorder_env["results_dir"],
        )

        assert result["status"] == "failed"
        events = _read_register_events(recorder_env)
        assert events == []

    @pytest.mark.parametrize("reason", [
        "subprocess_error",
        "auth_error",
        "binary_not_found",
        "network_error",
        "validation_failed",
        "empty_review_content",
        "artifact_materialization_failed",
    ])
    def test_record_failure_execution_reasons_no_register_emit(self, recorder_env, reason):
        """record_failure with any execution-level reason must NOT emit gate_failed.

        These are infrastructure failures, not semantic gate verdicts. Emitting
        gate_failed would abuse the 'gate completed with blocking findings' contract.
        """
        payload = _make_payload(gate="codex_gate", pr_number=77)
        result_dict = _make_result()
        result_dict["reason"] = reason
        result_dict["reason_detail"] = f"Execution failure: {reason}"

        out = record_failure(
            gate="codex_gate",
            pr_number=77,
            pr_id="",
            result=result_dict,
            request_payload=payload,
            requests_dir=recorder_env["requests_dir"],
            results_dir=recorder_env["results_dir"],
        )

        assert out["status"] == "failed"
        events = _read_register_events(recorder_env)
        assert events == [], (
            f"reason={reason!r} must not emit gate_failed; got: {events}"
        )
