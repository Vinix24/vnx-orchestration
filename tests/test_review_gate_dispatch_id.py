#!/usr/bin/env python3
"""Tests: dispatch_id propagation in review_gate_request receipts (DRIFT-2 fix)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import review_gate_manager as rgm
import append_receipt
from governance_receipts import emit_governance_receipt as _emit_real
from review_contract import ReviewContract


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
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return project_root


@pytest.fixture(autouse=True)
def reset_warning_flag():
    """Reset the one-time warning sentinel between tests."""
    append_receipt._warned_review_gate_no_dispatch_id = False
    yield
    append_receipt._warned_review_gate_no_dispatch_id = False


# ---------------------------------------------------------------------------
# Test 1: dispatch_id propagates when provided
# ---------------------------------------------------------------------------

def test_request_reviews_propagates_dispatch_id_to_receipt(review_env, monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_emit(event_type, **kwargs):
        captured.append({"event_type": event_type, **kwargs})
        return {"append_status": "appended", "idempotency_key": "k"}

    monkeypatch.setattr(rgm, "emit_governance_receipt", fake_emit)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    manager.request_reviews(
        pr_number=99,
        branch="fix/test-branch",
        review_stack=["gemini_review"],
        risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"],
        mode="per_pr",
        dispatch_id="abc-123",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt["dispatch_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Test 2: backwards compatibility — no dispatch_id → empty string in receipt
# ---------------------------------------------------------------------------

def test_request_reviews_without_dispatch_id_preserves_backwards_compat(review_env, monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_emit(event_type, **kwargs):
        captured.append({"event_type": event_type, **kwargs})
        return {"append_status": "appended", "idempotency_key": "k"}

    monkeypatch.setattr(rgm, "emit_governance_receipt", fake_emit)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "gemini" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    # Omit dispatch_id entirely (default = "")
    manager.request_reviews(
        pr_number=100,
        branch="fix/no-dispatch-id",
        review_stack=["gemini_review"],
        risk_class="low",
        changed_files=["docs/guide.md"],
        mode="per_pr",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    # dispatch_id is passed as empty string — present but falsy, not absent
    assert receipt.get("dispatch_id", None) == ""


# ---------------------------------------------------------------------------
# Test 3: soft warning fires once on review_gate_request with empty dispatch_id
# ---------------------------------------------------------------------------

def test_append_receipt_warns_once_on_review_gate_request_missing_dispatch_id():
    receipt_missing = {
        "event_type": "review_gate_request",
        "gate": "gemini_review",
        "dispatch_id": "",
    }
    receipt_with_id = {
        "event_type": "review_gate_request",
        "gate": "gemini_review",
        "dispatch_id": "",
    }

    warn_calls: List[Dict[str, Any]] = []

    original_emit = append_receipt._emit

    def capturing_emit(level, code, **fields):
        if code == "review_gate_request_missing_dispatch_id":
            warn_calls.append({"level": level, "code": code, **fields})
        original_emit(level, code, **fields)

    with patch.object(append_receipt, "_emit", side_effect=capturing_emit):
        append_receipt._warn_if_review_gate_missing_dispatch_id(
            "review_gate_request", receipt_missing
        )
        # Second call: sentinel is set, should NOT emit again
        append_receipt._warn_if_review_gate_missing_dispatch_id(
            "review_gate_request", receipt_with_id
        )

    assert len(warn_calls) == 1, "Warning should fire exactly once per process run"
    assert warn_calls[0]["level"] == "WARN"
    assert warn_calls[0]["code"] == "review_gate_request_missing_dispatch_id"


# ---------------------------------------------------------------------------
# Test 4: soft warning does NOT fire when dispatch_id is present
# ---------------------------------------------------------------------------

def test_append_receipt_no_warning_when_dispatch_id_present():
    receipt = {
        "event_type": "review_gate_request",
        "gate": "gemini_review",
        "dispatch_id": "some-real-dispatch-id",
    }

    warn_calls: List[Dict[str, Any]] = []

    original_emit = append_receipt._emit

    def capturing_emit(level, code, **fields):
        if code == "review_gate_request_missing_dispatch_id":
            warn_calls.append({"level": level, "code": code, **fields})
        original_emit(level, code, **fields)

    with patch.object(append_receipt, "_emit", side_effect=capturing_emit):
        append_receipt._warn_if_review_gate_missing_dispatch_id(
            "review_gate_request", receipt
        )

    assert len(warn_calls) == 0, "No warning when dispatch_id is present"


# ---------------------------------------------------------------------------
# Test 5: soft warning does NOT fire for unrelated event types
# ---------------------------------------------------------------------------

def test_append_receipt_no_warning_for_other_event_types():
    receipt = {
        "event_type": "task_complete",
        "dispatch_id": "",
    }

    warn_calls: List[Dict[str, Any]] = []

    original_emit = append_receipt._emit

    def capturing_emit(level, code, **fields):
        if code == "review_gate_request_missing_dispatch_id":
            warn_calls.append({"level": level, "code": code, **fields})
        original_emit(level, code, **fields)

    with patch.object(append_receipt, "_emit", side_effect=capturing_emit):
        append_receipt._warn_if_review_gate_missing_dispatch_id(
            "task_complete", receipt
        )

    assert len(warn_calls) == 0, "Warning is review_gate_request-specific"


# ---------------------------------------------------------------------------
# Test 6: request_gemini_with_contract propagates dispatch_id to receipt
# ---------------------------------------------------------------------------

def test_request_gemini_with_contract_propagates_dispatch_id(review_env, monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_emit(event_type, **kwargs):
        captured.append({"event_type": event_type, **kwargs})
        return {"append_status": "appended", "idempotency_key": "k"}

    monkeypatch.setattr(rgm, "emit_governance_receipt", fake_emit)
    monkeypatch.setattr("gate_request_handler.render_gemini_prompt", lambda c: "mocked prompt")
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake")
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "1")

    contract = ReviewContract(
        pr_id="PR-99",
        branch="fix/test-contract",
        risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"],
        content_hash="deadbeef",
    )

    manager = rgm.ReviewGateManager()
    manager.request_gemini_with_contract(
        contract=contract,
        mode="per_pr",
        dispatch_id="contract-dispatch-gemini",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt.get("dispatch_id") == "contract-dispatch-gemini", (
        "dispatch_id must be forwarded to emit_governance_receipt in request_gemini_with_contract"
    )


# ---------------------------------------------------------------------------
# Test 7: request_claude_github_with_contract propagates dispatch_id to receipt
# ---------------------------------------------------------------------------

def test_request_claude_github_with_contract_propagates_dispatch_id(review_env, monkeypatch):
    captured: List[Dict[str, Any]] = []

    def fake_emit(event_type, **kwargs):
        captured.append({"event_type": event_type, **kwargs})
        return {"append_status": "appended", "idempotency_key": "k"}

    monkeypatch.setattr(rgm, "emit_governance_receipt", fake_emit)
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    contract = ReviewContract(
        pr_id="PR-99",
        branch="fix/test-contract",
        risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"],
        content_hash="deadbeef",
    )

    manager = rgm.ReviewGateManager()
    manager.request_claude_github_with_contract(
        contract=contract,
        mode="per_pr",
        dispatch_id="contract-dispatch-claude-gh",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt.get("dispatch_id") == "contract-dispatch-claude-gh", (
        "dispatch_id must be forwarded to emit_governance_receipt in request_claude_github_with_contract"
    )


# ---------------------------------------------------------------------------
# Test 8: Integration — real emit_governance_receipt + real append_receipt path
# ---------------------------------------------------------------------------

def test_emit_governance_receipt_with_dispatch_id_routes_to_t0_receipts(review_env, monkeypatch):
    """Integration: receipt with a real dispatch_id must land in t0_receipts.ndjson.

    Exercises the full path: emit_governance_receipt → append_receipt_payload → disk
    write → read-back. Covers Codex advisory (PR #274): prior tests mocked
    emit_governance_receipt and never exercised the real storage stream.
    """
    state_dir = review_env / ".vnx-data" / "state"
    t0_receipts = state_dir / "t0_receipts.ndjson"
    gate_events = state_dir / "gate_events.ndjson"

    _emit_real(
        "review_gate_request",
        dispatch_id="abc-123",
        gate="gemini_review",
        pr_id="99",
        branch="fix/test-branch",
    )

    assert t0_receipts.exists(), "t0_receipts.ndjson must be created by emit_governance_receipt"
    lines = [ln for ln in t0_receipts.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, "exactly one receipt line expected"
    stored = json.loads(lines[0])
    assert stored["event_type"] == "review_gate_request"
    assert stored["dispatch_id"] == "abc-123", "dispatch_id must be preserved in the persisted JSON line"

    # With a real dispatch_id, should_route_to_gate_stream() returns False.
    # gate_events.ndjson must NOT contain this receipt.
    if gate_events.exists():
        gate_lines = [ln for ln in gate_events.read_text().splitlines() if ln.strip()]
        assert len(gate_lines) == 0, "receipt must NOT appear in gate_events.ndjson when dispatch_id is real"
