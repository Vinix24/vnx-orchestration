"""Tests for dispatch_cleanup.py — OI-1072 governed cleanup for stale bundles."""

import json
import os
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_cleanup as dc  # noqa: E402


def _make_pending_dir(tmp_path: Path) -> Path:
    """Create a minimal pending/ directory structure for testing."""
    pending = tmp_path / "dispatches" / "pending"
    pending.mkdir(parents=True)
    return pending


def _make_bundle(pending: Path, dispatch_id: str, *, with_spec: bool = True,
                 with_instruction: bool = True, age_days: float = 0.0) -> Path:
    """Create a test bundle directory with optional spec and instruction."""
    bundle = pending / dispatch_id
    bundle.mkdir(parents=True)
    if with_spec:
        spec = {"dispatch_id": dispatch_id, "project_id": "test",
                "role": "backend-developer", "gate": "test-gate",
                "target_slot": "T1"}
        (bundle / "dispatch-spec.json").write_text(json.dumps(spec))
    if with_instruction:
        (bundle / "instruction.md").write_text("# Test instruction\n")
    if age_days > 0:
        import time
        old_time = time.time() - (age_days * 86400)
        os.utime(str(bundle), (old_time, old_time))
    return bundle


def _make_receipt(state_dir: Path, dispatch_id: str) -> None:
    """Write a minimal receipt to t0_receipts.ndjson."""
    receipt_file = state_dir / "t0_receipts.ndjson"
    receipt = json.dumps({
        "dispatch_id": dispatch_id,
        "timestamp": "2026-08-10T00:00:00Z",
        "event_type": "task_complete",
        "status": "success",
        "project_id": "test",
    })
    with open(receipt_file, "a") as f:
        f.write(receipt + "\n")


# ── scan_pending ─────────────────────────────────────────────────────────

def test_scan_empty_pending(tmp_path):
    pending = _make_pending_dir(tmp_path)
    entries = dc.scan_pending(tmp_path, tmp_path / "state")
    assert entries == []


def test_scan_skips_non_directory(tmp_path):
    pending = _make_pending_dir(tmp_path)
    (pending / "not-a-bundle.txt").write_text("hello")
    entries = dc.scan_pending(tmp_path, tmp_path / "state")
    assert entries == []


def test_scan_detects_bundle_with_receipt(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_bundle(pending, "20260810-000000-test-feature-A", age_days=10)
    _make_receipt(state_dir, "20260810-000000-test-feature-A")

    entries = dc.scan_pending(tmp_path, state_dir)
    assert len(entries) == 1
    e = entries[0]
    assert e.dispatch_id == "20260810-000000-test-feature-A"
    assert e.has_receipt is True
    assert e.classification == "receipt-found"
    assert e.action == "move-to-completed"


def test_scan_stale_no_receipt(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_bundle(pending, "20260801-000000-old-stale-feature-A", age_days=10)

    entries = dc.scan_pending(tmp_path, state_dir)
    assert len(entries) == 1
    e = entries[0]
    assert e.has_receipt is False
    assert e.age_days >= 7
    assert e.classification == "stale-no-receipt"
    assert e.action == "move-to-abandoned"


def test_scan_recent_no_receipt_skipped(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_bundle(pending, "20260809-000000-recent-feature-A", age_days=1)

    entries = dc.scan_pending(tmp_path, state_dir)
    assert len(entries) == 1
    e = entries[0]
    assert e.has_receipt is False
    assert e.age_days < 7
    assert e.classification == "recent-no-receipt"
    assert e.action == "skip"


def test_scan_multiple_bundles(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    _make_bundle(pending, "dispatch-old-A", age_days=14)
    _make_bundle(pending, "dispatch-recent-B", age_days=2)
    _make_receipt(state_dir, "dispatch-old-A")
    _make_receipt(state_dir, "dispatch-recent-B")

    entries = dc.scan_pending(tmp_path, state_dir)
    assert len(entries) == 2
    actions = {e.dispatch_id: e.action for e in entries}
    assert actions["dispatch-old-A"] == "move-to-completed"
    assert actions["dispatch-recent-B"] == "move-to-completed"


# ── execute_cleanup ──────────────────────────────────────────────────────

def test_execute_cleanup_dry_run_moves_nothing(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_bundle(pending, "dispatch-old-A", age_days=14)
    _make_receipt(state_dir, "dispatch-old-A")

    entries = dc.scan_pending(tmp_path, state_dir)
    report = dc.execute_cleanup(entries, tmp_path, dry_run=True)

    assert report.dry_run is True
    # Bundle should still exist (dry-run)
    assert pending.exists()
    assert (pending / "dispatch-old-A").exists()
    # completed/ should NOT have been created
    assert not (tmp_path / "dispatches" / "completed").exists()


def test_execute_cleanup_apply_moves_bundles(tmp_path):
    pending = _make_pending_dir(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    _make_bundle(pending, "dispatch-old-A", age_days=14)
    _make_receipt(state_dir, "dispatch-old-A")
    _make_bundle(pending, "dispatch-stale-B", age_days=30)

    entries = dc.scan_pending(tmp_path, state_dir)
    report = dc.execute_cleanup(entries, tmp_path, dry_run=False)

    assert report.dry_run is False
    # completed/ should have the receipt-found bundle
    completed = tmp_path / "dispatches" / "completed"
    assert completed.exists()
    assert (completed / "dispatch-old-A").exists()
    # abandoned/ should have the stale-no-receipt bundle
    abandoned = tmp_path / "dispatches" / "abandoned"
    assert abandoned.exists()
    assert (abandoned / "dispatch-stale-B").exists()
    # pending/ should be empty of bundles
    remaining = list(pending.iterdir())
    assert len(remaining) == 0


# ── format_report ────────────────────────────────────────────────────────

def test_format_report_dry_run():
    entries = [
        dc.BundleEntry(
            dispatch_id="test-1", bundle_dir="/tmp/test-1",
            age_days=30, has_receipt=True, has_instruction=True, has_spec=True,
            classification="receipt-found", action="move-to-completed",
        )
    ]
    report = dc.CleanupReport(entries=entries, dry_run=True, timestamp="2026-08-10T00:00:00Z")
    output = dc.format_report(report)
    assert "DRY-RUN" in output
    assert "test-1" in output
    assert "move-to-completed" in output


def test_format_json_output():
    entries = [
        dc.BundleEntry(
            dispatch_id="test-1", bundle_dir="/tmp/test-1",
            age_days=30, has_receipt=True, has_instruction=True, has_spec=True,
            classification="receipt-found", action="move-to-completed",
        )
    ]
    report = dc.CleanupReport(entries=entries, dry_run=True, timestamp="2026-08-10T00:00:00Z")
    output = dc.format_json(report)
    data = json.loads(output)
    assert data["total"] == 1
    assert data["dry_run"] is True
    assert data["entries"][0]["dispatch_id"] == "test-1"
