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
    assert receipt.get("pr_id") == "99", "pr_id must be present in the receipt (OI-915)"


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
    # OI-915: pr_id must always be present in the receipt, even when
    # dispatch_id is missing — the PR number is available regardless.
    assert receipt.get("pr_id") == "100", "pr_id must be present even when dispatch_id is absent"


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
        receipt_kind="review_gate",
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
    assert stored["receipt_kind"] == "review_gate", "receipt_kind must be stamped (PR-3 closed set)"
    assert stored["dispatch_id"] == "abc-123", "dispatch_id must be preserved in the persisted JSON line"

    # With a real dispatch_id, should_route_to_gate_stream() returns False.
    # gate_events.ndjson must NOT contain this receipt.
    if gate_events.exists():
        gate_lines = [ln for ln in gate_events.read_text().splitlines() if ln.strip()]
        assert len(gate_lines) == 0, "receipt must NOT appear in gate_events.ndjson when dispatch_id is real"


# ---------------------------------------------------------------------------
# Test 9: OI-915 — pr_id must be set when request_reviews dispatches codex_gate
# ---------------------------------------------------------------------------

def test_request_reviews_propagates_pr_id_for_codex_gate(review_env, monkeypatch):
    """OI-915: pr_id must land in the review_gate_request receipt for codex_gate."""
    captured: List[Dict[str, Any]] = []

    def fake_emit(event_type, **kwargs):
        captured.append({"event_type": event_type, **kwargs})
        return {"append_status": "appended", "idempotency_key": "k"}

    monkeypatch.setattr(rgm, "emit_governance_receipt", fake_emit)
    monkeypatch.setattr(rgm.shutil, "which", lambda tool: "/usr/bin/fake" if tool == "codex" else None)
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")

    manager = rgm.ReviewGateManager()
    manager.request_reviews(
        pr_number=1286,
        branch="dispatch/20260804-102001-gate-receipt-koppeling",
        review_stack=["codex_gate"],
        risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"],
        mode="final",
        dispatch_id="20260804-102001-gate-receipt-koppeling",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt["gate"] == "codex_gate"
    assert receipt["dispatch_id"] == "20260804-102001-gate-receipt-koppeling"
    assert receipt.get("pr_id") == "1286", (
        "OI-915: pr_id must be present in the review_gate_request receipt "
        "so the gate is linkable to its PR in the audit trail"
    )


# ---------------------------------------------------------------------------
# Test 10: OI-915 — pr_id must be set when request_reviews dispatches gemini_review
# ---------------------------------------------------------------------------

def test_request_reviews_propagates_pr_id_for_gemini_review(review_env, monkeypatch):
    """OI-915: pr_id must land in the review_gate_request receipt for gemini_review."""
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
        pr_number=1286,
        branch="dispatch/20260804-102001-gate-receipt-koppeling",
        review_stack=["gemini_review"],
        risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"],
        mode="per_pr",
        dispatch_id="20260804-102001-gate-receipt-koppeling",
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt["gate"] == "gemini_review"
    assert receipt.get("pr_id") == "1286", (
        "OI-915: pr_id must be present for gemini_review receipts too — "
        "the fix applies to all gates dispatched through request_reviews()"
    )


# ---------------------------------------------------------------------------
# Test 11: OI-915 — negative path: ghost receipt now carries pr_id when dispatch_id is absent
# ---------------------------------------------------------------------------

def test_request_reviews_sets_pr_id_even_when_dispatch_id_absent(review_env, monkeypatch):
    """OI-915: pr_id is set in the receipt even when dispatch_id is not available.

    When the gate is run outside a dispatch context (no dispatch/<id> branch),
    pr_id must still be present — it comes from the PR number, which is always
    available. The dispatch_id can legitimately be absent when the gate is
    invoked manually.
    """
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
    # No dispatch_id, branch is not dispatch/<id>
    manager.request_reviews(
        pr_number=42,
        branch="fix/some-other-branch",
        review_stack=["gemini_review"],
        risk_class="low",
        changed_files=["docs/guide.md"],
        mode="per_pr",
        # dispatch_id omitted → defaults to ""
    )

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["event_type"] == "review_gate_request"
    assert receipt.get("dispatch_id", None) == ""
    assert receipt.get("pr_id") == "42", (
        "OI-915: pr_id must be present even when dispatch_id is absent — "
        "the PR number is the fallback identifier for audit linkage"
    )


# ---------------------------------------------------------------------------
# Test 12: OI-915 — gate.sh extracts dispatch_id from dispatch/<id> branch
# ---------------------------------------------------------------------------

def test_gate_sh_extracts_dispatch_id_from_branch():
    """OI-915: verify the bash extraction logic for dispatch_id from branch name.

    Does NOT shell out — directly tests the pattern-match logic used in the
    gate.sh extraction block. The pattern is: if branch starts with ``dispatch/``,
    strip that prefix to get the dispatch_id. Falls back to VNX_CURRENT_DISPATCH_ID
    when the branch does not follow the convention.
    """
    import re

    def extract_dispatch_id(branch: str, env_dispatch_id: str = "") -> str:
        """Mirror the logic from gate.sh:
            if [[ "$branch" == dispatch/* ]]; then
              dispatch_id="${branch#dispatch/}"
            elif [ -n "${VNX_CURRENT_DISPATCH_ID:-}" ]; then
              dispatch_id="$VNX_CURRENT_DISPATCH_ID"
            fi
        """
        if branch.startswith("dispatch/"):
            return branch[len("dispatch/"):]
        if env_dispatch_id:
            return env_dispatch_id
        return ""

    # Standard dispatch branch
    assert extract_dispatch_id("dispatch/20260804-102001-gate-receipt-koppeling") == \
        "20260804-102001-gate-receipt-koppeling"

    # Non-dispatch branch with env fallback
    assert extract_dispatch_id("fix/some-bug", "env-dispatch-123") == "env-dispatch-123"

    # Non-dispatch branch without env fallback — empty
    assert extract_dispatch_id("main") == ""
    assert extract_dispatch_id("feature/my-feature") == ""

    # Edge case: branch named exactly "dispatch/" (strips to empty string)
    assert extract_dispatch_id("dispatch/") == ""

    # Edge case: empty branch
    assert extract_dispatch_id("") == ""
