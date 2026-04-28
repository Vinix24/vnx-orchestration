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
  13. gate_artifacts success with no blocking → gate_passed in register
  14. gate_artifacts success with blocking → gate_failed in register
  15. gate_recorder failure path emits gate_failed (not just success-materialization)
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
        "event_type": "task_started",
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
        mock.patch.object(append_receipt, "_emit_dispatch_register", side_effect=lambda r: call_order.append("emit")),
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

    monkeypatch.setattr(append_receipt, "resolve_state_dir", lambda f: state_dir)

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(append_receipt.subprocess, "Popen", _FakePopen)

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

    monkeypatch.setattr(append_receipt, "resolve_state_dir", lambda f: state_dir)
    monkeypatch.setattr(append_receipt.subprocess, "Popen", lambda cmd, **kw: None)

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

    monkeypatch.setattr(append_receipt, "resolve_state_dir", lambda f: state_dir)

    popen_calls: list = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)

    monkeypatch.setattr(append_receipt.subprocess, "Popen", _FakePopen)

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


def test_gate_artifacts_no_blocking_emits_gate_passed(isolated_register, tmp_path):
    """materialize_artifacts with no blocking findings emits gate_passed."""
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
    assert len(reg_events_for_gate) == 1
    assert reg_events_for_gate[0]["event"] == "gate_passed"


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


def test_gate_recorder_failure_path_emits_gate_failed(isolated_register, tmp_path):
    """record_failure (execution failure path) must emit gate_failed to the register."""
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
    assert len(gate_events) == 1, f"Expected 1 gate event, got: {gate_events}"
    assert gate_events[0]["event"] == "gate_failed"
    assert gate_events[0].get("dispatch_id") == "D-FAILURE-020"
