#!/usr/bin/env python3
"""Tests for _emit_dispatch_register and idempotency key fix in append_receipt.py."""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"


def _load_append_receipt():
    """Import append_receipt with a minimal stub environment."""
    env_patch = {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(VNX_ROOT / ".vnx-data"),
        "VNX_STATE_DIR": str(VNX_ROOT / ".vnx-data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    mod_name = "append_receipt_testmodule"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "append_receipt.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # Register before exec so @dataclass can resolve the module namespace
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


@pytest.fixture(scope="module")
def ar():
    return _load_append_receipt()


def _make_receipt(event_type: str, status: str = "", gate: str = "", **extra) -> dict:
    base = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": event_type,
        "dispatch_id": "DISP-TEST-001",
        "terminal": "T2",
    }
    if status:
        base["status"] = status
    if gate:
        base["gate"] = gate
    base.update(extra)
    return base


def _mock_append_event(captured: list):
    """Return a mock append_event that records calls and returns True."""
    def _inner(event, *, dispatch_id="", pr_number=None, feature_id="", terminal="", gate=""):
        captured.append({
            "event": event,
            "dispatch_id": dispatch_id,
            "pr_number": pr_number,
            "feature_id": feature_id,
            "terminal": terminal,
            "gate": gate,
        })
        return True
    return _inner


def _run_emit(ar_mod, receipt: dict, captured: list) -> bool:
    fake_register = types.ModuleType("dispatch_register")
    fake_register.append_event = _mock_append_event(captured)
    with patch.dict(sys.modules, {"dispatch_register": fake_register}):
        return ar_mod._emit_dispatch_register(receipt)


# ── Test 1: task_complete + success → dispatch_completed ──────────────────────

def test_task_complete_success_emits_dispatch_completed(ar):
    captured = []
    receipt = _make_receipt("task_complete", status="success")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert len(captured) == 1
    assert captured[0]["event"] == "dispatch_completed"


# ── Test 2: task_complete + failed → dispatch_failed ─────────────────────────

def test_task_complete_failed_emits_dispatch_failed(ar):
    captured = []
    receipt = _make_receipt("task_complete", status="failed")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_failed"


# ── Test 3: task_complete + status=failure (codex variant) → dispatch_failed ─

def test_task_complete_failure_variant_emits_dispatch_failed(ar):
    captured = []
    receipt = _make_receipt("task_complete", status="failure")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_failed"


# ── Test 4: task_complete + unknown status → NO register entry ────────────────

def test_task_complete_unknown_status_returns_false(ar):
    captured = []
    receipt = _make_receipt("task_complete", status="pending")
    result = _run_emit(ar, receipt, captured)
    assert result is False
    assert len(captured) == 0


# ── Test 5: task_failed → dispatch_failed ────────────────────────────────────

def test_task_failed_emits_dispatch_failed(ar):
    captured = []
    receipt = _make_receipt("task_failed")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_failed"


# ── Test 6: task_timeout → dispatch_failed ───────────────────────────────────

def test_task_timeout_emits_dispatch_failed(ar):
    captured = []
    receipt = _make_receipt("task_timeout")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_failed"


# ── Test 7: task_started → dispatch_started ──────────────────────────────────

def test_task_started_emits_dispatch_started(ar):
    captured = []
    receipt = _make_receipt("task_started")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_started"


# ── Test 8: review_gate_request + codex_gate → gate_requested ────────────────

def test_review_gate_request_codex_emits_gate_requested(ar):
    captured = []
    receipt = _make_receipt("review_gate_request", gate="codex_gate")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "gate_requested"
    assert captured[0]["gate"] == "codex_gate"


# ── Test 9: review_gate_request + gemini_review → NO register entry ──────────

def test_review_gate_request_gemini_returns_false(ar):
    captured = []
    receipt = _make_receipt("review_gate_request", gate="gemini_review")
    result = _run_emit(ar, receipt, captured)
    assert result is False
    assert len(captured) == 0


# ── Test 10: review_gate_request + claude_github_optional → NO register entry ─

def test_review_gate_request_claude_github_returns_false(ar):
    captured = []
    receipt = _make_receipt("review_gate_request", gate="claude_github_optional")
    result = _run_emit(ar, receipt, captured)
    assert result is False
    assert len(captured) == 0


# ── Test 11: legacy 'event' field (no event_type) still classified ────────────

def test_legacy_event_field_still_classified(ar):
    captured = []
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event": "task_complete",
        "dispatch_id": "DISP-LEGACY-001",
        "terminal": "T1",
        "status": "success",
    }
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["event"] == "dispatch_completed"


# ── Test 12: pr_number in metadata.pr_number propagated ──────────────────────

def test_pr_number_from_metadata_propagated(ar):
    captured = []
    receipt = _make_receipt(
        "task_complete",
        status="success",
        metadata={"pr_number": 42},
    )
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["pr_number"] == 42


# ── Test 13: non-numeric pr_number defaults to None ──────────────────────────

def test_non_numeric_pr_number_defaults_to_none(ar):
    captured = []
    receipt = _make_receipt("task_complete", status="success", pr_number="not-a-number")
    result = _run_emit(ar, receipt, captured)
    assert result is True
    assert captured[0]["pr_number"] is None


# ── Test 14: exception inside append_event returns False, never raises ─────────

def test_exception_in_append_event_returns_false_never_raises(ar):
    def _boom(*args, **kwargs):
        raise RuntimeError("register exploded")

    fake_register = types.ModuleType("dispatch_register")
    fake_register.append_event = _boom

    receipt = _make_receipt("task_complete", status="success")
    with patch.dict(sys.modules, {"dispatch_register": fake_register}):
        result = ar._emit_dispatch_register(receipt)
    assert result is False


# ── Test 15: idempotency fix — two review_gate_request with different gates ───
#   Both must produce distinct idempotency keys and persist in t0_receipts.ndjson

import subprocess


def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(tmp_path / "data")
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    return env


def _append_via_subprocess(tmp_path: Path, receipt: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "append_receipt.py")],
        input=json.dumps(receipt),
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )


def test_two_gate_receipts_same_dispatch_both_persist(tmp_path: Path):
    base = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "review_gate_request",
        "dispatch_id": "DISP-GATE-FAN-001",
        "terminal": "T0",
    }
    codex_receipt = {**base, "gate": "codex_gate"}
    gemini_receipt = {**base, "gate": "gemini_review"}

    r1 = _append_via_subprocess(tmp_path, codex_receipt)
    r2 = _append_via_subprocess(tmp_path, gemini_receipt)

    assert r1.returncode == 0, f"codex gate append failed: {r1.stderr}"
    assert r2.returncode == 0, f"gemini gate append failed: {r2.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, (
        f"Expected 2 receipts (one per gate), got {len(lines)}. "
        "Idempotency key likely still colliding on dispatch_id alone."
    )
    gates = {json.loads(l).get("gate") for l in lines}
    assert "codex_gate" in gates
    assert "gemini_review" in gates


# ── Tests for _count_quality_violations ─────────────────────────────────────

def test_count_quality_violations_with_items(ar):
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "severity": "blocker", "item": "bad thing"},
                    {"check_id": "c2", "severity": "warn", "item": "risky thing"},
                    {"check_id": "c3", "severity": "info", "item": "note"},
                ]
            }
        }
    }
    assert ar._count_quality_violations(receipt) == 3


def test_count_quality_violations_empty_open_items(ar):
    receipt = {"quality_advisory": {"t0_recommendation": {"open_items": []}}}
    assert ar._count_quality_violations(receipt) == 0


def test_count_quality_violations_missing_t0_recommendation(ar):
    receipt = {"quality_advisory": {"version": "1.0"}}
    assert ar._count_quality_violations(receipt) == 0


def test_count_quality_violations_missing_advisory(ar):
    assert ar._count_quality_violations({}) == 0


def test_count_quality_violations_null_advisory(ar):
    assert ar._count_quality_violations({"quality_advisory": None}) == 0


# ── Test: open_items_created embedded in NDJSON BEFORE _register runs ────────

def test_open_items_created_embedded_in_ndjson(tmp_path: Path):
    """Receipt with pre-populated quality_advisory must persist open_items_created
    in the NDJSON line, not as 0 (the pre-existing bug)."""
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-OI-PRECOUNT-001",
        "terminal": "T1",
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "qa_check_1", "severity": "blocker", "item": "missing tests"},
                    {"check_id": "qa_check_2", "severity": "warn", "item": "low coverage"},
                ]
            }
        },
    }
    # --skip-enrichment prevents _enrich_completion_receipt from overwriting the
    # injected quality_advisory, so _count_quality_violations sees 2 items.
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "append_receipt.py"), "--skip-enrichment"],
        input=json.dumps(receipt),
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )
    assert result.returncode == 0, f"append failed: {result.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1

    persisted = json.loads(lines[0])
    assert persisted.get("open_items_created") == 2, (
        f"open_items_created should be 2 (pre-computed before write), got {persisted.get('open_items_created')!r}. "
        "Pre-existing bug: count was set after ndjson write so it persisted as 0."
    )


def test_open_items_created_zero_when_no_violations(tmp_path: Path):
    """Receipt with no open_items must persist open_items_created == 0."""
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-OI-PRECOUNT-002",
        "terminal": "T1",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "append_receipt.py"), "--skip-enrichment"],
        input=json.dumps(receipt),
        capture_output=True,
        text=True,
        env=_build_env(tmp_path),
    )
    assert result.returncode == 0, f"append failed: {result.stderr}"

    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    persisted = json.loads(lines[0])
    assert persisted.get("open_items_created", 0) == 0
