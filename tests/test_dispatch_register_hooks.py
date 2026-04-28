"""Tests for dispatch_register lifecycle hook integration (Sprint 3 split 2/3).

Coverage:
  1.  _emit_dispatch_register: task_complete + success → dispatch_completed
  2.  _emit_dispatch_register: task_complete + failed → dispatch_failed
  3.  _emit_dispatch_register: task_failed → dispatch_failed
  4.  _emit_dispatch_register: task_timeout → dispatch_failed
  5.  _emit_dispatch_register: review_gate_request → gate_requested
  6.  _emit_dispatch_register: irrelevant event → register unchanged
  7.  _emit_dispatch_register: pr_number falls back to metadata.pr_number
  8.  _emit_dispatch_register runs BEFORE _maybe_trigger_state_rebuild
  9.  _maybe_trigger_state_rebuild triggers on review_gate_request
  10. Throttle marker is integer (not float) after Python write
  11. Bash CLI dispatch_promoted event writes register entry
  12. Throttle expiry: rebuild triggers when stale throttle marker present
  13. gate_artifacts gemini_review → NO register event (parser deferred)
  14. gate_artifacts codex_gate with blocking → gate_failed in register
  15. gate_recorder failure for gemini_review → NO register event (parser deferred)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import append_receipt
import dispatch_register
import gate_recorder
import gate_artifacts
import state_rebuild_trigger


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_register(monkeypatch, tmp_path):
    """Route dispatch_register I/O to an isolated tmp dir for every test."""
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    return state_dir


def _reg_events(state_dir: Path) -> list[dict]:
    reg = state_dir / "dispatch_register.ndjson"
    if not reg.exists():
        return []
    events = []
    for line in reg.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


# ---------------------------------------------------------------------------
# Tests 1-7: _emit_dispatch_register classification and field handling
# ---------------------------------------------------------------------------


def test_emit_task_complete_success_classifies_completed(isolated_register):
    receipt = {
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "D-EMIT-001",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_completed"
    assert events[0]["dispatch_id"] == "D-EMIT-001"


def test_emit_task_complete_failed_classifies_failed(isolated_register):
    receipt = {
        "event_type": "task_complete",
        "status": "failed",
        "dispatch_id": "D-EMIT-002",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_failed"


def test_emit_task_failed_classifies_failed(isolated_register):
    receipt = {
        "event_type": "task_failed",
        "dispatch_id": "D-EMIT-003",
        "terminal": "T2",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_failed"


def test_emit_task_timeout_classifies_failed(isolated_register):
    receipt = {
        "event_type": "task_timeout",
        "dispatch_id": "D-EMIT-004",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_failed"


def test_emit_review_gate_request_classifies_gate_requested(isolated_register):
    receipt = {
        "event_type": "review_gate_request",
        "dispatch_id": "D-EMIT-005",
        "terminal": "T3",
        "gate": "codex_gate",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "gate_requested"
    assert events[0].get("gate") == "codex_gate"


def test_emit_irrelevant_event_writes_nothing(isolated_register):
    receipt = {
        "event_type": "system_heartbeat",  # genuinely not register-worthy
        "dispatch_id": "D-EMIT-006",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    assert _reg_events(isolated_register) == []


def test_emit_pr_number_falls_back_to_metadata(isolated_register):
    receipt = {
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "D-EMIT-007",
        "terminal": "T1",
        "metadata": {"pr_number": 42},
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0].get("pr_number") == 42


# ---------------------------------------------------------------------------
# Test 8: ordering — emit before rebuild
# ---------------------------------------------------------------------------


def test_emit_called_before_rebuild(tmp_path):
    """_emit_dispatch_register must precede _maybe_trigger_state_rebuild in the hook chain."""
    call_order: list[str] = []

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = state_dir / "t0_receipts.ndjson"

    receipt = {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "D-ORDER-001",
        "terminal": "T1",
        "status": "success",
    }

    with (
        mock.patch.object(append_receipt, "_emit_dispatch_register",
                          side_effect=lambda r: call_order.append("emit") or True),
        mock.patch.object(append_receipt, "_maybe_trigger_state_rebuild", side_effect=lambda r: call_order.append("rebuild")),
        mock.patch.object(append_receipt, "_register_quality_open_items", return_value=0),
        mock.patch.object(append_receipt, "_update_confidence_from_receipt"),
    ):
        append_receipt.append_receipt_payload(receipt, receipts_file=str(receipts_file))

    assert call_order == ["emit", "rebuild"], f"Unexpected call order: {call_order}"


# ---------------------------------------------------------------------------
# Test 9: rebuild triggered by review_gate_request
# ---------------------------------------------------------------------------


def test_maybe_trigger_rebuild_on_review_gate_request(monkeypatch, tmp_path):
    """review_gate_request receipt must trigger the state rebuild Popen."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Stale throttle so the rebuild is not suppressed
    throttle = state_dir / ".last_state_rebuild_ts"
    throttle.write_text("1", encoding="utf-8")

    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: state_dir)

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _FakePopen)

    receipt = {"event_type": "review_gate_request", "dispatch_id": "D-GATE-001"}
    append_receipt._maybe_trigger_state_rebuild(receipt)

    assert len(popen_calls) == 1, "Popen should have been called once"
    assert "build_t0_state.py" in popen_calls[0][-1]


# ---------------------------------------------------------------------------
# Test 10: throttle marker is integer
# ---------------------------------------------------------------------------


def test_throttle_marker_is_integer(monkeypatch, tmp_path):
    """Python side writes integer epoch to the throttle file (no decimal point)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    throttle = state_dir / ".last_state_rebuild_ts"
    throttle.write_text("1", encoding="utf-8")  # stale

    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: state_dir)
    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", lambda cmd, **kw: None)

    receipt = {"event_type": "task_complete", "dispatch_id": "D-THROTTLE-001"}
    append_receipt._maybe_trigger_state_rebuild(receipt)

    written = throttle.read_text(encoding="utf-8").strip()
    assert "." not in written, f"Throttle marker must be integer, got: {written!r}"
    assert written.isdigit(), f"Throttle marker must be digits only, got: {written!r}"


# ---------------------------------------------------------------------------
# Test 11: bash CLI writes dispatch_promoted to register
# ---------------------------------------------------------------------------


def test_bash_cli_dispatch_promoted_writes_register(isolated_register):
    """Calling the dispatch_register.py CLI (as the bash hook does) writes dispatch_promoted."""
    register_py = LIB_DIR / "dispatch_register.py"
    env = {
        **os.environ,
        "VNX_STATE_DIR": str(isolated_register),
        "VNX_DATA_DIR": str(isolated_register.parent),
        "VNX_DATA_DIR_EXPLICIT": "1",
    }
    result = subprocess.run(
        [sys.executable, str(register_py), "append", "dispatch_promoted",
         "dispatch_id=BASH-HOOK-001", "terminal=T1"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_promoted"
    assert events[0]["dispatch_id"] == "BASH-HOOK-001"


# ---------------------------------------------------------------------------
# Test 12: throttle expiry triggers rebuild
# ---------------------------------------------------------------------------


def test_throttle_expiry_triggers_rebuild(monkeypatch, tmp_path):
    """When throttle file is older than 30 s, rebuild Popen is called and marker refreshed."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Very old throttle
    throttle = state_dir / ".last_state_rebuild_ts"
    throttle.write_text("1000", encoding="utf-8")

    monkeypatch.setattr(state_rebuild_trigger, "_resolve_state_dir", lambda: state_dir)

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(state_rebuild_trigger.subprocess, "Popen", _FakePopen)

    receipt = {"event_type": "task_complete", "dispatch_id": "D-EXPIRE-001"}
    append_receipt._maybe_trigger_state_rebuild(receipt)

    assert popen_calls, "Popen not called despite stale throttle"
    new_ts = throttle.read_text(encoding="utf-8").strip()
    assert new_ts.isdigit()
    assert int(new_ts) > 1000


# ---------------------------------------------------------------------------
# Tests 13-14: gate_artifacts success path emits gate_passed / gate_failed
# ---------------------------------------------------------------------------


def _make_gate_request_payload(tmp_path: Path, pr_number: int = 1) -> dict:
    report_path = tmp_path / "reports" / "gate_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "gate": "gemini_review",
        "pr_number": pr_number,
        "pr_id": f"pr-{pr_number}",
        "branch": "feat/test",
        "report_path": str(report_path),
        "dispatch_id": f"D-GATE-{pr_number:03d}",
    }


def test_gate_artifacts_gemini_review_skips_register_emit(isolated_register, tmp_path):
    """materialize_artifacts for gemini_review must not emit any register event (parser deferred)."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    payload = _make_gate_request_payload(tmp_path, pr_number=10)
    stdout = "\n".join(["# Review", "Overall: LGTM", "No issues found."])

    result = gate_artifacts.materialize_artifacts(
        gate="gemini_review",
        pr_number=10,
        pr_id="pr-10",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.5,
        requests_dir=requests_dir,
        results_dir=results_dir,
        reports_dir=reports_dir,
    )

    assert result.get("status") == "completed"
    events = _reg_events(isolated_register)
    reg_events_for_gate = [e for e in events if e.get("gate") == "gemini_review"]
    assert len(reg_events_for_gate) == 0, (
        f"gemini_review must not emit register events (parser deferred), got: {reg_events_for_gate}"
    )


def test_gate_artifacts_blocking_emits_gate_failed(isolated_register, tmp_path):
    """materialize_artifacts on codex_gate with blocking findings emits gate_failed."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    payload = _make_gate_request_payload(tmp_path, pr_number=11)
    payload["gate"] = "codex_gate"
    payload["pr_id"] = "pr-11"
    # Codex stdout with a blocking finding
    stdout = (
        "## Findings\n"
        "- severity: error\n"
        "  message: null-deref in foo.py:42\n"
        "  file: foo.py\n"
        "  line: 42\n"
    )

    # Patch codex parser to return a blocking finding
    fake_findings = {
        "findings": [{"severity": "error", "message": "null-deref", "file": "foo.py", "line": 42}],
        "residual_risk": "null-deref present",
    }
    with mock.patch("gate_artifacts.parse_codex_findings", return_value=fake_findings):
        result = gate_artifacts.materialize_artifacts(
            gate="codex_gate",
            pr_number=11,
            pr_id="pr-11",
            stdout=stdout,
            request_payload=payload,
            duration_seconds=2.0,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
        )

    assert result.get("status") == "completed"
    events = _reg_events(isolated_register)
    reg_events_for_gate = [e for e in events if e.get("gate") == "codex_gate"]
    assert len(reg_events_for_gate) == 1
    assert reg_events_for_gate[0]["event"] == "gate_failed"


# ---------------------------------------------------------------------------
# Test 15: gate_recorder failure path emits gate_failed
# ---------------------------------------------------------------------------


def test_gate_recorder_gemini_failure_skips_register_emit(isolated_register, tmp_path):
    """record_failure for gemini_review must NOT emit to the register (parser deferred)."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    for d in (requests_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    request_payload = {
        "gate": "gemini_review",
        "pr_number": 20,
        "pr_id": "pr-20",
        "dispatch_id": "D-FAILURE-020",
        "report_path": str(tmp_path / "report.md"),
    }
    failure_result = {
        "reason": "timeout",
        "reason_detail": "gate stalled after 300 s",
        "duration_seconds": 300.0,
        "partial_output_lines": 5,
        "runner_pid": 12345,
    }

    gate_recorder.record_failure(
        gate="gemini_review",
        pr_number=20,
        pr_id="pr-20",
        result=failure_result,
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
    )

    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "gemini_review"]
    assert len(gate_events) == 0, (
        f"gemini_review record_failure must not emit register event (parser deferred), got: {gate_events}"
    )


# ---------------------------------------------------------------------------
# Test 16: status="failure" → dispatch_failed (BLOCKING fix 1)
# ---------------------------------------------------------------------------


def test_emit_task_complete_failure_status_classifies_failed(isolated_register):
    """task_complete with status='failure' must map to dispatch_failed (not dispatch_completed)."""
    receipt = {
        "event_type": "task_complete",
        "status": "failure",
        "dispatch_id": "D-EMIT-016",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_failed", (
        f"status='failure' must produce dispatch_failed, got {events[0]['event']!r}"
    )


# ---------------------------------------------------------------------------
# Tests 17-18: gate hook pr_id → pr_number parsing (BLOCKING fix 2)
# ---------------------------------------------------------------------------


def test_gate_artifacts_gemini_pr_id_numeric_skips_register_emit(isolated_register, tmp_path):
    """gate hook with gemini_review + pr_id='276' must complete but NOT emit a register event."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "gate_report.md"
    payload = {
        "gate": "gemini_review",
        "pr_number": None,
        "pr_id": "276",
        "branch": "feat/test",
        "report_path": str(report_path),
        "dispatch_id": "",
    }
    stdout = "\n".join(["# Review", "Overall: LGTM", "No issues found."])

    result = gate_artifacts.materialize_artifacts(
        gate="gemini_review",
        pr_number=None,
        pr_id="276",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.0,
        requests_dir=requests_dir,
        results_dir=results_dir,
        reports_dir=reports_dir,
    )

    assert result.get("status") == "completed"
    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "gemini_review"]
    assert len(gate_events) == 0, (
        f"gemini_review must not emit register events (parser deferred), got: {gate_events}"
    )


def test_gate_recorder_pr_id_numeric_resolves_pr_number(isolated_register, tmp_path):
    """record_failure with pr_id='276' and pr_number=None must write pr_number=276 to register."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    for d in (requests_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    request_payload = {
        "gate": "codex_gate",
        "pr_number": None,
        "pr_id": "276",
        "dispatch_id": "",
        "report_path": str(tmp_path / "report.md"),
    }
    failure_result = {
        "reason": "timeout",
        "reason_detail": "stalled",
        "duration_seconds": 60.0,
        "partial_output_lines": 0,
        "runner_pid": 99,
    }

    gate_recorder.record_failure(
        gate="codex_gate",
        pr_number=None,
        pr_id="276",
        result=failure_result,
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
    )

    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "codex_gate"]
    assert len(gate_events) == 1, f"Expected 1 gate event, got: {gate_events}"
    assert gate_events[0]["event"] == "gate_failed"
    assert gate_events[0].get("pr_number") == 276, (
        f"pr_id='276' must resolve to pr_number=276, got {gate_events[0].get('pr_number')!r}"
    )


def test_gate_artifacts_pr_id_non_numeric_does_not_crash(isolated_register, tmp_path):
    """gate hook with pr_id='abc' (non-numeric) and pr_number=None must not crash."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "gate_report.md"
    payload = {
        "gate": "gemini_review",
        "pr_number": None,
        "pr_id": "abc-branch",
        "branch": "feat/test",
        "report_path": str(report_path),
        "dispatch_id": "D-NONNUM-018",
    }
    stdout = "\n".join(["# Review", "Overall: LGTM", "No issues."])

    result = gate_artifacts.materialize_artifacts(
        gate="gemini_review",
        pr_number=None,
        pr_id="abc-branch",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.0,
        requests_dir=requests_dir,
        results_dir=results_dir,
        reports_dir=reports_dir,
    )

    # Must complete without raising; register event may use dispatch_id fallback
    assert result.get("status") == "completed"


# ---------------------------------------------------------------------------
# Test 19: gate hook fires Popen rebuild after register write (ADVISORY fix)
# ---------------------------------------------------------------------------


def test_gate_artifacts_triggers_state_rebuild(isolated_register, tmp_path, monkeypatch):
    """materialize_artifacts must reach the SUCCESS path and call maybe_trigger_state_rebuild."""
    rebuild_calls: list = []

    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "gate_report.md"
    payload = {
        "gate": "gemini_review",
        "pr_number": 30,
        "pr_id": "pr-30",
        "branch": "feat/test",
        "report_path": str(report_path),
        "dispatch_id": "D-REBUILD-019",
    }
    # 4 non-empty lines — satisfies gate_artifacts._validate_content (requires >= 3)
    stdout = (
        "# Gemini Review\n\n"
        "The implementation follows established patterns correctly.\n"
        "No security issues identified.\n"
        "Approved: LGTM."
    )

    with mock.patch("state_rebuild_trigger.maybe_trigger_state_rebuild", side_effect=lambda: rebuild_calls.append(1) or True), \
         mock.patch.object(gate_recorder, "record_failure_simple", wraps=gate_recorder.record_failure_simple) as mock_failure:
        result = gate_artifacts.materialize_artifacts(
            gate="gemini_review",
            pr_number=30,
            pr_id="pr-30",
            stdout=stdout,
            request_payload=payload,
            duration_seconds=0.5,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
        )

    # Success path: status must be "completed" — failure path returns status="failure"
    assert result.get("status") == "completed", f"Expected success path, got status={result.get('status')!r}"
    # record_failure_simple must NOT have been called on the success path
    mock_failure.assert_not_called()
    assert len(rebuild_calls) >= 1, "maybe_trigger_state_rebuild must be called after register write"


# ---------------------------------------------------------------------------
# Tests 20-22: non-numeric pr_id fallback to feature_id (BLOCKING fix 2)
# ---------------------------------------------------------------------------


def test_emit_dispatch_register_non_numeric_pr_id_uses_feature_id(isolated_register):
    """_emit_dispatch_register with pr_id='PR-6' + no dispatch_id must write feature_id='PR-6'."""
    receipt = {
        "event_type": "review_gate_request",
        "pr_id": "PR-6",
        "dispatch_id": "",
        "terminal": "T3",
        "gate": "codex_gate",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 event, got: {events}"
    assert events[0]["event"] == "gate_requested"
    assert events[0].get("feature_id") == "PR-6", (
        f"Non-numeric pr_id='PR-6' must become feature_id='PR-6', got {events[0].get('feature_id')!r}"
    )
    assert events[0].get("pr_number") is None


def test_gate_artifacts_gemini_non_numeric_pr_id_skips_register_emit(isolated_register, tmp_path):
    """gate hook with gemini_review + pr_id='PR-6' must complete but NOT emit a register event."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "gate_report.md"
    payload = {
        "gate": "gemini_review",
        "pr_number": None,
        "pr_id": "PR-6",
        "branch": "feat/contract",
        "report_path": str(report_path),
        "dispatch_id": "",
    }
    stdout = "# Review\nOverall: LGTM\nNo issues found."

    result = gate_artifacts.materialize_artifacts(
        gate="gemini_review",
        pr_number=None,
        pr_id="PR-6",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.0,
        requests_dir=requests_dir,
        results_dir=results_dir,
        reports_dir=reports_dir,
    )

    assert result.get("status") == "completed"
    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "gemini_review"]
    assert len(gate_events) == 0, (
        f"gemini_review must not emit register events (parser deferred), got: {gate_events}"
    )


def test_gate_recorder_non_numeric_pr_id_uses_feature_id(isolated_register, tmp_path):
    """record_failure with pr_id='PR-6', dispatch_id='', pr_number=None → feature_id='PR-6' in register."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    for d in (requests_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    request_payload = {
        "gate": "codex_gate",
        "pr_number": None,
        "pr_id": "PR-6",
        "dispatch_id": "",
        "report_path": str(tmp_path / "report.md"),
    }
    failure_result = {
        "reason": "timeout",
        "reason_detail": "stalled after 300 s",
        "duration_seconds": 300.0,
        "partial_output_lines": 0,
        "runner_pid": 42,
    }

    gate_recorder.record_failure(
        gate="codex_gate",
        pr_number=None,
        pr_id="PR-6",
        result=failure_result,
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
    )

    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "codex_gate"]
    assert len(gate_events) == 1, f"Expected 1 gate event, got: {gate_events}"
    assert gate_events[0]["event"] == "gate_failed"
    assert gate_events[0].get("feature_id") == "PR-6", (
        f"Non-numeric pr_id='PR-6' must become feature_id='PR-6', got {gate_events[0].get('feature_id')!r}"
    )
    assert gate_events[0].get("pr_number") is None


# ---------------------------------------------------------------------------
# Tests 23-24: legacy 'event' field fallback in _emit_dispatch_register
# ---------------------------------------------------------------------------


def test_emit_legacy_event_field_triggers_register_entry(isolated_register):
    """Receipt with legacy 'event' key (no event_type) must trigger a register entry."""
    receipt = {
        "event": "task_complete",
        "status": "success",
        "dispatch_id": "D-LEGACY-001",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 register entry, got: {events}"
    assert events[0]["event"] == "dispatch_completed", (
        f"Legacy 'event' field must produce dispatch_completed, got {events[0]['event']!r}"
    )
    assert events[0]["dispatch_id"] == "D-LEGACY-001"


def test_emit_canonical_event_type_field_still_works(isolated_register):
    """Canonical 'event_type' key must still produce a register entry (regression guard)."""
    receipt = {
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "D-CANON-001",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_completed"
    assert events[0]["dispatch_id"] == "D-CANON-001"


# ---------------------------------------------------------------------------
# Tests 25-26: status defaulting guards (ADVISORY 1 — Codex PR #278 round 5)
# ---------------------------------------------------------------------------


def test_emit_task_complete_unknown_status_skips_emit(isolated_register):
    """task_complete with status='unknown' must NOT emit to register (malformed receipt guard)."""
    receipt = {
        "event_type": "task_complete",
        "status": "unknown",
        "dispatch_id": "D-UNKNOWN-001",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 0, (
        f"task_complete with status='unknown' must not emit, got: {events}"
    )


def test_emit_task_complete_empty_status_emits_completed(isolated_register):
    """task_complete with status='' (empty) must map to dispatch_completed (legacy convention)."""
    receipt = {
        "event_type": "task_complete",
        "status": "",
        "dispatch_id": "D-EMPTY-001",
        "terminal": "T1",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1
    assert events[0]["event"] == "dispatch_completed"
    assert events[0]["dispatch_id"] == "D-EMPTY-001"


# ---------------------------------------------------------------------------
# Test 27: claude_github_optional must not emit register events (parser deferred)
# ---------------------------------------------------------------------------


def test_gate_artifacts_claude_github_optional_skips_register_emit(isolated_register, tmp_path):
    """materialize_artifacts claude_github_optional must not emit any register event (parser unimplemented)."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / "gate_report.md"
    payload = {
        "gate": "claude_github_optional",
        "pr_number": 60,
        "pr_id": "pr-60",
        "branch": "feat/test",
        "report_path": str(report_path),
        "dispatch_id": "D-CLAUDE-GH-060",
    }
    stdout = (
        "# Claude GitHub Review\n\n"
        "Looks good overall.\n"
        "Minor suggestions noted."
    )

    result = gate_artifacts.materialize_artifacts(
        gate="claude_github_optional",
        pr_number=60,
        pr_id="pr-60",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.0,
        requests_dir=requests_dir,
        results_dir=results_dir,
        reports_dir=reports_dir,
    )

    assert result.get("status") == "completed"
    events = _reg_events(isolated_register)
    gate_events = [e for e in events if e.get("gate") == "claude_github_optional"]
    assert len(gate_events) == 0, (
        f"claude_github_optional must not emit register event (parser unimplemented), got: {gate_events}"
    )


# ---------------------------------------------------------------------------
# Tests 28-30: symmetric defer — review_gate_request only emits for codex_gate
# ---------------------------------------------------------------------------


def test_emit_review_gate_request_gemini_skips_register(isolated_register):
    """review_gate_request for gemini_review must NOT write to register (symmetric defer with fixup #6)."""
    receipt = {
        "event_type": "review_gate_request",
        "dispatch_id": "D-SYMM-028",
        "terminal": "T3",
        "gate": "gemini_review",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 0, (
        f"gemini_review review_gate_request must not emit register event (deferred), got: {events}"
    )


def test_emit_review_gate_request_codex_gate_emits_gate_requested(isolated_register):
    """review_gate_request for codex_gate must emit gate_requested (only gate with full lifecycle)."""
    receipt = {
        "event_type": "review_gate_request",
        "dispatch_id": "D-SYMM-029",
        "terminal": "T3",
        "gate": "codex_gate",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 register event for codex_gate, got: {events}"
    assert events[0]["event"] == "gate_requested"
    assert events[0].get("gate") == "codex_gate"


def test_emit_review_gate_request_claude_github_optional_skips_register(isolated_register):
    """review_gate_request for claude_github_optional must NOT write to register (symmetric defer with fixup #6)."""
    receipt = {
        "event_type": "review_gate_request",
        "dispatch_id": "D-SYMM-030",
        "terminal": "T3",
        "gate": "claude_github_optional",
    }
    append_receipt._emit_dispatch_register(receipt)
    events = _reg_events(isolated_register)
    assert len(events) == 0, (
        f"claude_github_optional review_gate_request must not emit register event (deferred), got: {events}"
    )


# ---------------------------------------------------------------------------
# Tests 31-33: task_started / task_start / dispatch_start → dispatch_started
# (ADVISORY fix: VALID_EVENTS includes dispatch_started; map caller event types)
# ---------------------------------------------------------------------------


def test_emit_task_started_maps_to_dispatch_started(isolated_register):
    """task_started event must map to dispatch_started in register (ADVISORY fix)."""
    receipt = {
        "event_type": "task_started",
        "dispatch_id": "D-STARTED-031",
        "terminal": "T1",
    }
    result = append_receipt._emit_dispatch_register(receipt)
    assert result is True
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 dispatch_started event, got: {events}"
    assert events[0]["event"] == "dispatch_started"
    assert events[0]["dispatch_id"] == "D-STARTED-031"


def test_emit_task_start_maps_to_dispatch_started(isolated_register):
    """task_start event must map to dispatch_started in register (ADVISORY fix)."""
    receipt = {
        "event_type": "task_start",
        "dispatch_id": "D-STARTED-032",
        "terminal": "T2",
    }
    result = append_receipt._emit_dispatch_register(receipt)
    assert result is True
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 dispatch_started event, got: {events}"
    assert events[0]["event"] == "dispatch_started"


def test_emit_dispatch_start_maps_to_dispatch_started(isolated_register):
    """dispatch_start event must map to dispatch_started in register (ADVISORY fix)."""
    receipt = {
        "event_type": "dispatch_start",
        "dispatch_id": "D-STARTED-033",
        "terminal": "T1",
    }
    result = append_receipt._emit_dispatch_register(receipt)
    assert result is True
    events = _reg_events(isolated_register)
    assert len(events) == 1, f"Expected 1 dispatch_started event, got: {events}"
    assert events[0]["event"] == "dispatch_started"


# ---------------------------------------------------------------------------
# Test 34: persisted ndjson receipt carries open_items_created (BLOCKING 1 fix)
# ---------------------------------------------------------------------------


def test_receipt_persisted_with_open_items_count(isolated_register, tmp_path):
    """Persisted ndjson line must carry open_items_created field (BLOCKING 1 fix).

    Before the fix: open_items_created was set after the ndjson write, so the
    persisted receipt had the field absent. After the fix: pre-computed before write.
    """
    receipts_file = tmp_path / "receipts.ndjson"

    receipt = {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "D-PERSIST-OI-034",
        "terminal": "T1",
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "chk-1", "file": "foo.py", "severity": "warning", "item": "issue 1"},
                    {"check_id": "chk-2", "file": "bar.py", "severity": "warning", "item": "issue 2"},
                ],
            },
        },
    }

    with (
        # Prevent _enrich_completion_receipt from overwriting quality_advisory with real repo data
        mock.patch.object(append_receipt, "_enrich_completion_receipt", side_effect=lambda r: r),
        mock.patch.object(append_receipt, "_register_quality_open_items", return_value=2),
        mock.patch.object(append_receipt, "_update_confidence_from_receipt"),
        mock.patch.object(append_receipt, "_maybe_trigger_state_rebuild"),
    ):
        result = append_receipt.append_receipt_payload(receipt, receipts_file=str(receipts_file))

    assert result.status == "appended"

    lines = [ln.strip() for ln in receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    persisted = json.loads(lines[0])

    assert "open_items_created" in persisted, (
        "Persisted receipt must contain open_items_created (BLOCKING 1 regression guard)"
    )
    assert persisted["open_items_created"] == 2, (
        f"Expected open_items_created=2, got {persisted['open_items_created']!r}"
    )


# ---------------------------------------------------------------------------
# Test 35: register written before ndjson commit (BLOCKING 2 fix)
# ---------------------------------------------------------------------------


def test_register_written_before_ndjson_commit(isolated_register, tmp_path):
    """_emit_dispatch_register must be called before ndjson write (BLOCKING 2 fix).

    Verified by spying on _emit_dispatch_register: at the moment it is called,
    the ndjson file must not yet exist (or be empty).
    """
    receipts_file = tmp_path / "receipts.ndjson"

    receipt = {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "D-BLOCKING2-035",
        "terminal": "T1",
    }

    call_observations: list = []
    real_emit = append_receipt._emit_dispatch_register  # capture before patch

    def emit_spy(r):
        ndjson_written = receipts_file.exists() and bool(receipts_file.read_text(encoding="utf-8").strip())
        call_observations.append(ndjson_written)
        return real_emit(r)

    with (
        mock.patch.object(append_receipt, "_emit_dispatch_register", side_effect=emit_spy),
        mock.patch.object(append_receipt, "_register_quality_open_items", return_value=0),
        mock.patch.object(append_receipt, "_update_confidence_from_receipt"),
        mock.patch.object(append_receipt, "_maybe_trigger_state_rebuild"),
    ):
        result = append_receipt.append_receipt_payload(receipt, receipts_file=str(receipts_file))

    assert result.status == "appended"
    assert len(call_observations) == 1, f"Expected 1 emit call, got {len(call_observations)}"
    assert not call_observations[0], (
        "ndjson must NOT contain data when _emit_dispatch_register is called (BLOCKING 2 regression guard)"
    )
