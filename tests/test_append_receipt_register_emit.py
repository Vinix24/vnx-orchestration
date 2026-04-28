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


# ── Tests for dedup-aware _count_quality_violations ─────────────────────────

def test_count_quality_violations_dedup_same_key(ar):
    """Two items with identical (check_id, file_basename, symbol) collapse to 1."""
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "file": "src/foo.py", "symbol": "my_func", "severity": "blocker"},
                    {"check_id": "c1", "file": "src/foo.py", "symbol": "my_func", "severity": "blocker"},
                    {"check_id": "c2", "file": "src/bar.py", "symbol": "", "severity": "warn"},
                ]
            }
        }
    }
    assert ar._count_quality_violations(receipt) == 2


def test_count_quality_violations_five_with_two_deduped(ar):
    """5 violations: c1 in different dirs → 2 distinct keys; c3 same file twice → deduped.

    OI-1176 fix: full path used, so a/foo.py and b/foo.py are NOT collapsed.
    Result: 4 distinct keys (c1:a/foo.py:fn, c1:b/foo.py:fn, c2:a/foo.py:fn, c3:c/bar.py:).
    """
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "file": "a/foo.py", "symbol": "fn", "severity": "blocker"},
                    {"check_id": "c1", "file": "b/foo.py", "symbol": "fn", "severity": "blocker"},  # different path → NOT deduped
                    {"check_id": "c2", "file": "a/foo.py", "symbol": "fn", "severity": "warn"},
                    {"check_id": "c3", "file": "c/bar.py", "symbol": "", "severity": "info"},
                    {"check_id": "c3", "file": "c/bar.py", "symbol": "", "severity": "info"},  # identical path → deduped
                ]
            }
        }
    }
    assert ar._count_quality_violations(receipt) == 4


def test_count_quality_violations_different_checks_not_deduped(ar):
    """Same file + symbol but different check_ids are NOT deduped (separate findings)."""
    receipt = {
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "file": "src/foo.py", "symbol": "fn", "severity": "blocker"},
                    {"check_id": "c2", "file": "src/foo.py", "symbol": "fn", "severity": "warn"},
                    {"check_id": "c3", "file": "src/foo.py", "symbol": "fn", "severity": "info"},
                ]
            }
        }
    }
    assert ar._count_quality_violations(receipt) == 3


# ── Test: NDJSON persists dedup-aware open_items_created ────────────────────

def test_open_items_created_dedup_count_in_ndjson(tmp_path: Path):
    """5 violations: c1 different dirs (not deduped) + c3 same file twice (deduped) → open_items_created=4 in NDJSON."""
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-OI-DEDUP-001",
        "terminal": "T1",
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "file": "a/foo.py", "symbol": "fn", "severity": "blocker"},
                    {"check_id": "c1", "file": "b/foo.py", "symbol": "fn", "severity": "blocker"},
                    {"check_id": "c2", "file": "a/foo.py", "symbol": "fn", "severity": "warn"},
                    {"check_id": "c3", "file": "c/bar.py", "symbol": "", "severity": "info"},
                    {"check_id": "c3", "file": "c/bar.py", "symbol": "", "severity": "info"},
                ]
            }
        },
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
    assert len(lines) == 1
    persisted = json.loads(lines[0])
    assert persisted.get("open_items_created") == 4, (
        f"Expected 4 (5 violations: 4 distinct paths, 1 true duplicate collapsed), got {persisted.get('open_items_created')!r}"
    )


# ── Tests for _update_confidence_from_receipt status-aware logic ─────────────

def _run_confidence_update(ar_mod, receipt: dict, captured: list) -> None:
    """Run _update_confidence_from_receipt with a mock update_confidence_from_outcome."""
    import types as _types

    fake_persist = _types.ModuleType("intelligence_persist")

    def _mock_update(db_path, dispatch_id, terminal, outcome):
        captured.append({"dispatch_id": dispatch_id, "terminal": terminal, "outcome": outcome})

    fake_persist.update_confidence_from_outcome = _mock_update

    fake_state_dir = Path("/tmp/fake_state_dir_confidence_test")
    fake_db = fake_state_dir / "quality_intelligence.db"

    with patch.dict(sys.modules, {"intelligence_persist": fake_persist}), \
         patch.object(ar_mod, "resolve_state_dir", return_value=fake_state_dir), \
         patch("pathlib.Path.exists", return_value=True):
        ar_mod._update_confidence_from_receipt(receipt)


def test_confidence_task_complete_success_yields_success(ar):
    """task_complete + status=success → outcome='success'."""
    captured = []
    receipt = _make_receipt("task_complete", status="success", dispatch_id="CONF-001", terminal="T1")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "success"


def test_confidence_task_complete_failed_yields_failure(ar):
    """task_complete + status=failed → outcome='failure' (the bug fix)."""
    captured = []
    receipt = _make_receipt("task_complete", status="failed", dispatch_id="CONF-002", terminal="T1")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "failure"


def test_confidence_task_complete_failure_variant_yields_failure(ar):
    """task_complete + status=failure → outcome='failure'."""
    captured = []
    receipt = _make_receipt("task_complete", status="failure", dispatch_id="CONF-003", terminal="T2")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "failure"


def test_confidence_task_complete_error_status_yields_failure(ar):
    """task_complete + status=error → outcome='failure'."""
    captured = []
    receipt = _make_receipt("task_complete", status="error", dispatch_id="CONF-004", terminal="T1")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "failure"


def test_confidence_task_complete_unknown_status_skips(ar):
    """task_complete + unknown status → no confidence update."""
    captured = []
    receipt = _make_receipt("task_complete", status="pending", dispatch_id="CONF-005", terminal="T1")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 0


def test_confidence_task_completed_alternate_form(ar):
    """task_completed (alternate form) + success → outcome='success'."""
    captured = []
    receipt = _make_receipt("task_completed", status="success", dispatch_id="CONF-006", terminal="T3")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "success"


def test_confidence_task_failed_always_failure(ar):
    """task_failed → outcome='failure' regardless of status field."""
    captured = []
    receipt = _make_receipt("task_failed", dispatch_id="CONF-007", terminal="T2")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "failure"


def test_confidence_non_task_event_skips(ar):
    """Non-task events don't update confidence."""
    captured = []
    receipt = _make_receipt("review_gate_request", dispatch_id="CONF-008", terminal="T0")
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 0


def test_confidence_missing_dispatch_id_skips(ar):
    """Missing dispatch_id → no confidence update."""
    captured = []
    receipt = {"event_type": "task_complete", "status": "success", "terminal": "T1"}
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 0


def test_confidence_legacy_event_field(ar):
    """Legacy 'event' field instead of 'event_type' is handled."""
    captured = []
    receipt = {
        "event": "task_complete",
        "status": "success",
        "dispatch_id": "CONF-010",
        "terminal": "T1",
    }
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "success"


def test_confidence_task_complete_empty_status_yields_success(ar):
    """task_complete with empty status ('' in SUCCESS_STATUSES) → outcome='success'."""
    captured = []
    receipt = _make_receipt("task_complete", dispatch_id="CONF-011", terminal="T1")
    # no status field → str(receipt.get("status", "")).lower() == ""
    _run_confidence_update(ar, receipt, captured)
    assert len(captured) == 1
    assert captured[0]["outcome"] == "success"


# ── Tests: CQS timing — open_items_created set AFTER enrichment ───────────────


def test_open_items_created_from_enrichment_generated_advisory(tmp_path: Path, ar):
    """Normal completion path: enrichment generates quality_advisory with violations.
    open_items_created in NDJSON must equal the real count, not 0.

    Regression guard for codex post-merge audit v2 (PR #281): pre-count ran before
    enrichment so advisory was empty → count was always 0.
    """
    receipts_file = tmp_path / "receipts.ndjson"
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-CQS-TIMING-001",
        "terminal": "T1",
        # No quality_advisory here — enrichment generates it
    }

    def fake_enrich(r, repo_root=None):
        enriched = dict(r)
        enriched["quality_advisory"] = {
            "t0_recommendation": {
                "open_items": [
                    {"check_id": "c1", "severity": "blocker", "item": "missing tests"},
                    {"check_id": "c2", "severity": "warn", "item": "low coverage"},
                ]
            }
        }
        return enriched

    with patch.object(ar, "_enrich_completion_receipt", side_effect=fake_enrich), \
         patch.object(ar, "_register_quality_open_items", return_value=0), \
         patch.object(ar, "_update_confidence_from_receipt"), \
         patch.object(ar, "_emit_dispatch_register", return_value=False), \
         patch.object(ar, "_maybe_trigger_state_rebuild"):
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_file))

    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    persisted = json.loads(lines[0])
    assert persisted.get("open_items_created") == 2, (
        f"Expected 2 (from enrichment-generated advisory), got {persisted.get('open_items_created')!r}. "
        "Bug: pre-count ran before enrichment so advisory was empty → count was 0."
    )


def test_open_items_created_zero_when_enrichment_adds_no_violations(tmp_path: Path, ar):
    """Normal completion path: enrichment generates empty open_items → open_items_created=0."""
    receipts_file = tmp_path / "receipts_clean.ndjson"
    receipt = {
        "timestamp": "2026-04-28T12:00:01Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-CQS-TIMING-002",
        "terminal": "T1",
    }

    def fake_enrich_clean(r, repo_root=None):
        enriched = dict(r)
        enriched["quality_advisory"] = {"t0_recommendation": {"open_items": []}}
        return enriched

    with patch.object(ar, "_enrich_completion_receipt", side_effect=fake_enrich_clean), \
         patch.object(ar, "_register_quality_open_items", return_value=0), \
         patch.object(ar, "_update_confidence_from_receipt"), \
         patch.object(ar, "_emit_dispatch_register", return_value=False), \
         patch.object(ar, "_maybe_trigger_state_rebuild"):
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_file))

    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    persisted = json.loads(lines[0])
    assert persisted.get("open_items_created", 0) == 0


def test_enrich_sets_open_items_created_internally(ar):
    """_enrich_completion_receipt sets open_items_created from the quality_advisory it generates.
    Ensures the CQS DB UPDATE sees the real count (not 0 from a missing field)."""
    receipt = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": "DISP-CQS-INTERNAL-001",
        "terminal": "T1",
        # No quality_advisory — the function generates it via generate_quality_advisory
    }

    # Advisory dict that generate_quality_advisory would produce with 3 violations.
    advisory_dict = {
        "version": "1.0",
        "t0_recommendation": {
            "open_items": [
                {"check_id": "c1", "file": "a.py", "symbol": "", "severity": "blocker"},
                {"check_id": "c2", "file": "b.py", "symbol": "", "severity": "warn"},
                {"check_id": "c3", "file": "c.py", "symbol": "", "severity": "info"},
            ]
        },
    }
    fake_advisory = MagicMock()
    fake_advisory.to_dict.return_value = advisory_dict

    with patch.object(ar, "_build_git_provenance", return_value={"git_ref": "HEAD"}), \
         patch.object(ar, "_build_session_metadata", return_value={"session_id": "s1"}), \
         patch.object(ar, "enrich_receipt_provenance"), \
         patch.object(ar, "validate_receipt_provenance", return_value=MagicMock(gaps=[])), \
         patch.object(ar, "collect_terminal_snapshot", return_value=MagicMock(to_dict=lambda: {})), \
         patch.object(ar, "get_changed_files", return_value=[Path("a.py"), Path("b.py"), Path("c.py")]), \
         patch.object(ar, "generate_quality_advisory", return_value=fake_advisory), \
         patch.object(ar, "_get_open_items_manager", return_value=MagicMock(count_items_closed_by_dispatch=lambda _: 0)), \
         patch.object(ar, "resolve_state_dir", return_value=Path("/tmp/fake_state")), \
         patch("pathlib.Path.exists", return_value=False):
        enriched = ar._enrich_completion_receipt(receipt)

    assert enriched.get("open_items_created") == 3, (
        f"Expected 3 (from generate_quality_advisory result), got {enriched.get('open_items_created')!r}. "
        "_enrich_completion_receipt must set open_items_created before the CQS DB write."
    )


# ── OI-1176: dedup key directory-aware ───────────────────────────────────────

def _make_quality_receipt(open_items: list) -> dict:
    return {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "DISP-DEDUP-001",
        "terminal": "T2",
        "quality_advisory": {
            "t0_recommendation": {
                "open_items": open_items,
            }
        },
    }


def test_count_quality_violations_different_dirs_not_collapsed(ar):
    """Same check_id+symbol but different directories → 2 distinct keys (OI-1176 fix)."""
    items = [
        {"check_id": "SEC-01", "file": "scripts/foo.py", "symbol": "unsafe_call", "severity": "blocker", "item": "A"},
        {"check_id": "SEC-01", "file": "tests/foo.py",   "symbol": "unsafe_call", "severity": "blocker", "item": "B"},
    ]
    receipt = _make_quality_receipt(items)
    count = ar._count_quality_violations(receipt)
    assert count == 2, f"Expected 2 distinct OIs for different directories, got {count}"


def test_count_quality_violations_same_file_collapsed(ar):
    """Same check_id+symbol+file → 1 key (dedup still works for true duplicates)."""
    items = [
        {"check_id": "SEC-01", "file": "scripts/foo.py", "symbol": "unsafe_call", "severity": "blocker", "item": "A"},
        {"check_id": "SEC-01", "file": "scripts/foo.py", "symbol": "unsafe_call", "severity": "blocker", "item": "A"},
    ]
    receipt = _make_quality_receipt(items)
    count = ar._count_quality_violations(receipt)
    assert count == 1, f"Expected 1 OI for identical file+check_id+symbol, got {count}"
