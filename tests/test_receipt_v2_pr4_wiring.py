#!/usr/bin/env python3
"""ADR-035 §9 PR-4 — end-to-end wiring tests.

PR-1 (receipt_verdict.compute_verdict) and PR-2 (the warning-destination
engine) landed as additive libraries, unit-tested in isolation
(test_receipt_verdict.py, test_warning_destination.py). This file proves the
WIRING: that both write paths — append_receipt_internals.payload
.append_receipt_payload (Path 2) and governance_emit.emit_dispatch_receipt
(Path 1, both its envelope and multi-provider sub-paths) — now stamp
verdict{}/warnings[]/verification{} on a REAL appended receipt, not just on
a receipt dict handed directly to the pure functions.

Covers: T3-T9 (end-to-end on both paths), T22 (validator rejection on both
paths), T23/T35 (doc-only invariant, end-to-end), T24 (envelope sub-path
verification extraction), T25 (multi-provider sub-path pending-report).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import append_receipt as ar  # noqa: E402 — registers the Path-2 facade
import governance_emit  # noqa: E402

from append_receipt_internals.common import AppendReceiptError  # noqa: E402
from append_receipt_internals.receipt_finalize import finalize_receipt_v2_fields  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_lines(receipts_path: Path) -> list:
    if not receipts_path.exists():
        return []
    return [json.loads(l) for l in receipts_path.read_text().splitlines() if l.strip()]


def _path2_append(receipt: Dict[str, Any], receipts_path: Path):
    """Path 2: append_receipt_payload, isolated (no project_id -> no central mirror)."""
    return ar.append_receipt_payload(
        receipt, receipts_file=str(receipts_path), skip_enrichment=True,
    )


def _path1_emit(receipts_path: Path, *, status: str, verification=None, dispatch_id="path1-test"):
    """Path 1: governance_emit.emit_dispatch_receipt."""
    return governance_emit.emit_dispatch_receipt(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-5",
        pr_id=None,
        status=status,
        completion_pct=100 if status == "success" else 0,
        risk=0.0,
        findings=[],
        duration_seconds=1.0,
        token_usage={"input": 1, "output": 1},
        cost_usd=None,
        state_dir=receipts_path.parent,
        verification=verification,
    )


def _base_path2_receipt(dispatch_id: str, **overrides: Any) -> Dict[str, Any]:
    receipt = {
        "timestamp": "2026-07-22T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "status": "done",
    }
    receipt.update(overrides)
    return receipt


# ---------------------------------------------------------------------------
# T3 — reject on hard-failure status, both paths
# ---------------------------------------------------------------------------


def test_t3_path2_reject_on_hard_failure_status(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt("t3-path2", status="failed")
    result = _path2_append(receipt, receipts_path)
    assert result.status == "appended"
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "reject"


def test_t3_path1_reject_on_hard_failure_status(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    _path1_emit(receipts_path, status="failure", dispatch_id="t3-path1")
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "reject"


# ---------------------------------------------------------------------------
# T4 — investigate: success status, no/incomplete verification evidence
# ---------------------------------------------------------------------------


def test_t4_path2_investigate_no_verification(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt("t4-path2", status="done")
    _path2_append(receipt, receipts_path)
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "investigate"


def test_t4_path1_investigate_no_verification(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    _path1_emit(receipts_path, status="success", dispatch_id="t4-path1")
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "investigate"
    assert line["verdict"]["evidence_complete"] is True  # method absent, not an "incomplete" method


# ---------------------------------------------------------------------------
# T5 — accept: success status, clean verification evidence
# ---------------------------------------------------------------------------


def test_t5_path2_accept_clean_verification(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t5-path2",
        status="done",
        verification={"method": "pytest", "tests_run": 12, "tests_passed": 12, "tests_failed": 0},
    )
    _path2_append(receipt, receipts_path)
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "accept"
    assert line["verdict"]["evidence_complete"] is True


def test_t5_path1_accept_clean_verification(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    _path1_emit(
        receipts_path,
        status="success",
        dispatch_id="t5-path1",
        verification={"method": "pytest", "tests_run": 5, "tests_passed": 5, "tests_failed": 0},
    )
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "accept"


# ---------------------------------------------------------------------------
# T6 / T22 — validator rejection on BOTH write paths (closes BLOCKING-3's
# bypass end-to-end: the shared primitive's validator, not just Path 2)
# ---------------------------------------------------------------------------


def test_t6_path2_dropped_null_reason_rejected(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    entry = {
        "code": "x", "severity": "warn", "message": "m",
        "destination": "dropped", "oi_id": None, "reason": None, "requires_tracking": False,
    }
    receipt = _base_path2_receipt("t6-path2", warnings=[entry])
    with pytest.raises(AppendReceiptError):
        _path2_append(receipt, receipts_path)
    assert not _read_lines(receipts_path)


def test_t6_path1_dropped_null_reason_rejected(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    entry = {
        "code": "x", "severity": "warn", "message": "m",
        "destination": "dropped", "oi_id": None, "reason": None, "requires_tracking": False,
    }
    with pytest.raises(RuntimeError):
        governance_emit.emit_dispatch_receipt(
            dispatch_id="t6-path1", terminal_id="T1", provider="claude",
            model="claude-sonnet-5", pr_id=None, status="success",
            completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
            token_usage={"input": 1, "output": 1}, cost_usd=None,
            state_dir=receipts_path.parent,
            verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
            warnings=[entry],
        )
    assert not _read_lines(receipts_path)


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"code": "x", "severity": "warn", "destination": "ignored", "oi_id": None, "reason": None, "requires_tracking": False},
        {"severity": "warn", "destination": "counted", "oi_id": None, "reason": None, "requires_tracking": False},
        {"code": "x", "destination": "counted", "oi_id": None, "reason": None, "requires_tracking": False},
        {"code": "x", "severity": "warn", "destination": "oi", "oi_id": None, "reason": None, "requires_tracking": True},
    ],
    ids=["illegal-destination", "missing-code", "missing-severity", "oi-missing-oi_id"],
)
def test_t22_path2_rejects_illegal_warnings(tmp_path, bad_entry):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt("t22-path2", warnings=[bad_entry])
    with pytest.raises(AppendReceiptError):
        _path2_append(receipt, receipts_path)
    assert not _read_lines(receipts_path)


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"code": "x", "severity": "warn", "destination": "ignored", "oi_id": None, "reason": None, "requires_tracking": False},
        {"severity": "warn", "destination": "counted", "oi_id": None, "reason": None, "requires_tracking": False},
        {"code": "x", "destination": "counted", "oi_id": None, "reason": None, "requires_tracking": False},
        {"code": "x", "severity": "warn", "destination": "oi", "oi_id": None, "reason": None, "requires_tracking": True},
    ],
    ids=["illegal-destination", "missing-code", "missing-severity", "oi-missing-oi_id"],
)
def test_t22_path1_rejects_illegal_warnings(tmp_path, bad_entry):
    """T22 on Path 1: emit_dispatch_receipt (the real Path-1 writer) with an
    illegal warnings[] entry — proves the validator now binds to Path 1 too,
    not just Path 2 (closes BLOCKING-3's bypass end-to-end)."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    with pytest.raises((AppendReceiptError, RuntimeError)):
        governance_emit.emit_dispatch_receipt(
            dispatch_id="t22-path1", terminal_id="T1", provider="claude",
            model="claude-sonnet-5", pr_id=None, status="success",
            completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
            token_usage={"input": 1, "output": 1}, cost_usd=None,
            state_dir=receipts_path.parent,
            verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
            warnings=[bad_entry],
        )
    assert not _read_lines(receipts_path)


# ---------------------------------------------------------------------------
# T7-T9 — warnings[] flow through the destination-assignment engine when
# driven through the shared primitive (finalize_receipt_v2_fields) that both
# write paths call — not just assign_destination() in isolation.
# ---------------------------------------------------------------------------


def _load_oim(tmp_path: Path):
    """Fresh, isolated open_items_manager bound to a per-test STATE_DIR —
    mirrors test_warning_destination.py's helper (real add_item_programmatic,
    not a mock)."""
    import importlib.util
    import os
    from unittest.mock import patch as _patch

    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    mod_name = f"open_items_manager_pr4wiring_{tmp_path.name}"
    with _patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS_DIR / "open_items_manager.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


def test_t7_finalize_promotes_recurring_warn_to_oi(tmp_path):
    oim = _load_oim(tmp_path)
    counter_path = tmp_path / "counts.json"

    def _fresh_receipt(dispatch_id):
        return {
            "dispatch_id": dispatch_id, "status": "done", "event_type": "task_complete",
            "timestamp": "2026-07-22T10:00:00Z",
            # A freshly-authored raw entry each "receipt" — no destination yet,
            # simulating three independent dispatches hitting the same check.
            "warnings": [{"code": "recurring_check", "severity": "warn", "message": "m"}],
        }

    receipt = None
    for i in range(3):
        receipt = _fresh_receipt(f"t7-finalize-{i}")
        finalize_receipt_v2_fields(receipt, counter_path=counter_path, open_items_manager_module=oim)

    assert receipt["warnings"][0]["destination"] == "oi"
    assert receipt["warnings"][0]["oi_id"] is not None
    assert receipt["open_items_created"] == 1
    # A promoted warn-severity entry (not blocker) doesn't force reject —
    # verdict still falls through to investigate on missing test evidence.
    assert receipt["verdict"]["decision"] == "investigate"


def test_t8_finalize_warn_below_threshold_is_counted(tmp_path):
    counter_path = tmp_path / "counts.json"
    receipt = {
        "dispatch_id": "t8-finalize", "status": "done", "event_type": "task_complete",
        "timestamp": "2026-07-22T10:00:00Z",
        "warnings": [{"code": "below_threshold_check", "severity": "warn", "message": "m"}],
    }

    class _NeverCalledOIM:
        def add_item_programmatic(self, **kwargs):
            raise AssertionError("must not touch the OI store below threshold")

    finalize_receipt_v2_fields(receipt, counter_path=counter_path, open_items_manager_module=_NeverCalledOIM())

    assert receipt["warnings"][0]["destination"] == "counted"
    assert receipt["open_items_created"] == 0


def test_t9_finalize_report_contract_invalid_counted_from_first_occurrence(tmp_path):
    counter_path = tmp_path / "counts.json"
    receipt = {
        "dispatch_id": "t9-finalize", "status": "done", "event_type": "task_complete",
        "timestamp": "2026-07-22T10:00:00Z",
        "warnings": [{"code": "report_contract_invalid", "severity": "warn", "message": "Summary missing"}],
    }

    finalize_receipt_v2_fields(receipt, counter_path=counter_path)

    assert receipt["warnings"][0]["destination"] == "counted"
    assert receipt["warnings"][0]["requires_tracking"] is False
    assert receipt["open_items_created"] == 0


# ---------------------------------------------------------------------------
# T23 / T35 — doc-only invariant, end-to-end through the real write path
# ---------------------------------------------------------------------------


def test_t23_path2_nondocs_path_forces_investigate(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t23-path2",
        status="done",
        verification={"method": "n/a"},
        provenance={"diff_summary": {"paths": ["docs/ADR.md", "scripts/lib/foo.py"]}},
    )
    _path2_append(receipt, receipts_path)
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "investigate"


def test_t35_path2_missing_paths_forces_investigate(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t35-path2",
        status="done",
        verification={"method": "n/a"},
    )
    _path2_append(receipt, receipts_path)
    line = _read_lines(receipts_path)[-1]
    assert line["verdict"]["decision"] == "investigate"


def test_t23_path1_nondocs_path_forces_investigate(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    governance_emit.emit_dispatch_receipt(
        dispatch_id="t23-path1", terminal_id="T1", provider="claude",
        model="claude-sonnet-5", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
        token_usage={"input": 1, "output": 1}, cost_usd=None,
        state_dir=receipts_path.parent,
        verification={"method": "n/a"},
    )
    line = _read_lines(receipts_path)[-1]
    # Path 1 stamps no provenance today (§3.1.1's invariant is fail-safe on
    # absence) -> paths is unproven doc-only-ness -> investigate.
    assert line["verdict"]["decision"] == "investigate"


# ---------------------------------------------------------------------------
# T24 — envelope sub-path: verification{} populated from the report already
# on disk, via the same extract_validation regex extractor Path 2 uses.
# ---------------------------------------------------------------------------


def test_t24_envelope_subpath_verification_from_report(tmp_path):
    import dispatch_envelope
    from dispatch_envelope import EnvelopeSpec, run_envelope

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir(parents=True)
    (data_dir / "unified_reports").mkdir(parents=True)
    spec = EnvelopeSpec(
        dispatch_id="t24-envelope",
        terminal_id="T1",
        provider="codex",
        model="gpt-5.2-codex",
        instruction="implement the feature",
        role="backend-developer",
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )

    codex_result = SimpleNamespace(
        returncode=0,
        completion_text="Implemented the feature.\n\n## Validation\n\n12 tests passed, 0 failed\n",
        timed_out=False,
        stopped_early=False,
        token_usage={"input_tokens": 100, "output_tokens": 50},
        error=None,
        event_writer_failures=0,
    )

    captured: Dict[str, Any] = {}
    real_emit = governance_emit.emit_dispatch_receipt

    def _capture(**kwargs):
        captured.update(kwargs)
        return real_emit(**kwargs)

    with patch("provider_spawns.codex_spawn.spawn_codex", return_value=codex_result), \
         patch("governance_emit.emit_dispatch_receipt", side_effect=_capture):
        run_envelope(spec, lane="codex")

    assert captured["verification"]["method"] == "pytest"
    assert captured["verification"]["tests_run"] == 12
    assert captured["verification"]["tests_passed"] == 12
    assert captured["verification"]["tests_failed"] == 0

    report_path = data_dir / "unified_reports" / "t24-envelope.md"
    assert report_path.exists()
    assert "## Validation" in report_path.read_text()

    line = _read_lines(state_dir / "t0_receipts.ndjson")[-1]
    assert line["verification"]["method"] == "pytest"
    assert line["verdict"]["decision"] == "accept"


def test_t24_envelope_subpath_no_report_degrades_to_unknown(tmp_path):
    """No report on disk (report emit failed) -> method='unknown', never a crash."""
    from dispatch_envelope import _verification_from_report

    result = _verification_from_report(None)
    assert result["method"] == "unknown"
    assert result["tests_run"] is None


# ---------------------------------------------------------------------------
# T25 — multi-provider sub-path: verification.method='pending-report',
# verdict='investigate', evidence_complete=False, and never rewritten by a
# later write for the same dispatch_id.
# ---------------------------------------------------------------------------


def test_t25_multi_provider_subpath_pending_report(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    (tmp_path / "state").mkdir()
    (tmp_path / "data").mkdir()

    import provider_dispatch

    args = argparse.Namespace(
        dispatch_id="t25-multiprovider",
        terminal_id="T1",
        instruction="do the thing",
        pr_id=None,
        mandate_id=None,
    )
    result = SimpleNamespace(
        completion_text="done",
        token_usage={"input_tokens": 10, "output_tokens": 5},
    )
    now = datetime.now(timezone.utc)

    provider_dispatch._emit_governance(args, "codex", "gpt-5.2-codex", result, now, now, "success")

    receipts_path = tmp_path / "state" / "t0_receipts.ndjson"
    lines = _read_lines(receipts_path)
    assert len(lines) == 1, "receipt-first ordering: exactly one write for this dispatch_id"
    line = lines[0]
    assert line["verification"]["method"] == "pending-report"
    assert line["verdict"]["decision"] == "investigate"
    assert line["verdict"]["evidence_complete"] is False

    # Report emission runs AFTER the receipt on this sub-path (ADR-035
    # §3.1.1) -- confirm it writes only the .md file, never a second
    # receipt line for the same dispatch_id (append-only, no backfill).
    report_path = tmp_path / "data" / "unified_reports" / "t25-multiprovider.md"
    assert report_path.exists()
    assert len(_read_lines(receipts_path)) == 1
