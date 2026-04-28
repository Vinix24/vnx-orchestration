#!/usr/bin/env python3
"""Tests for state_mutation_receipt event type.

Coverage:
  1. emit_state_mutation writes receipt with event_type=state_mutation
  2. skip_enrichment=True skips _enrich_completion_receipt
  3. state_mutation receipt does NOT recursively trigger rebuild
  4. Integration: emit_state_mutation -> read back from ndjson file
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))

import append_receipt as ar
import state_mutation as sm
import build_t0_state as bts


def test_emit_state_mutation_receipt_shape(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    captured: list[dict] = []

    def _fake_append(receipt, *, skip_enrichment=False, **kwargs):
        captured.append({"receipt": receipt, "skip_enrichment": skip_enrichment})
        return ar.AppendResult(
            status="appended",
            receipts_file=Path(receipts_file),
            idempotency_key="test-key",
        )

    with mock.patch("append_receipt.append_receipt_payload", _fake_append):
        sm.emit_state_mutation(
            "t0_state.json",
            trigger="auto_rebuild",
            rebuild_seconds=1.23,
            size_bytes=4567,
        )

    assert len(captured) == 1
    r = captured[0]["receipt"]
    assert r["event_type"] == "state_mutation"
    assert r["file"] == "t0_state.json"
    assert r["trigger"] == "auto_rebuild"
    assert r["rebuild_seconds"] == 1.23
    assert r["size_bytes"] == 4567
    assert r["terminal"] == "T0"
    assert r["source"] == "vnx_state"
    assert captured[0]["skip_enrichment"] is True


def test_skip_enrichment_skips_enrich_completion_receipt(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "state_mutation",
        "terminal": "T0",
        "source": "vnx_state",
        "file": "t0_state.json",
        "trigger": "auto_rebuild",
    }

    with mock.patch("append_receipt._enrich_completion_receipt") as mock_enrich, \
         mock.patch("append_receipt._register_quality_open_items", return_value=0), \
         mock.patch("append_receipt._update_confidence_from_receipt"), \
         mock.patch("append_receipt._maybe_trigger_state_rebuild"):
        ar.append_receipt_payload(receipt, receipts_file=receipts_file, skip_enrichment=True)

    mock_enrich.assert_not_called()


def test_state_mutation_does_not_trigger_rebuild(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "state_mutation",
        "terminal": "T0",
        "source": "vnx_state",
        "file": "t0_state.json",
        "trigger": "auto_rebuild",
    }

    with mock.patch("append_receipt.subprocess.Popen") as mock_popen, \
         mock.patch("append_receipt._enrich_completion_receipt", side_effect=lambda r: r), \
         mock.patch("append_receipt._register_quality_open_items", return_value=0), \
         mock.patch("append_receipt._update_confidence_from_receipt"):
        ar.append_receipt_payload(receipt, receipts_file=receipts_file, skip_enrichment=True)

    mock_popen.assert_not_called()


def test_integration_emit_and_read_back(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = state_dir / "t0_receipts.ndjson"

    env_patch = {
        "PROJECT_ROOT": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(SCRIPTS_DIR.parent),
        "VNX_DATA_DIR_EXPLICIT": "1",
    }

    with mock.patch.dict(os.environ, env_patch), \
         mock.patch("append_receipt.resolve_state_dir", return_value=state_dir), \
         mock.patch("append_receipt._maybe_trigger_state_rebuild"):
        result = sm.emit_state_mutation(
            "t0_state.json",
            trigger="auto_rebuild",
            rebuild_seconds=0.5,
            section="feature_state",
        )

    assert result is not None
    assert result.status == "appended"

    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "state_mutation"
    assert parsed["file"] == "t0_state.json"
    assert parsed["trigger"] == "auto_rebuild"
    assert parsed["section"] == "feature_state"
    assert parsed["rebuild_seconds"] == 0.5


def test_build_failure_does_not_emit_state_mutation(tmp_path: Path) -> None:
    """main() must NOT emit state_mutation when build_t0_state raises (blocking fix)."""
    output_path = tmp_path / "t0_state.json"

    with mock.patch("sys.argv", ["build_t0_state.py", "--output", str(output_path)]), \
         mock.patch("build_t0_state.build_t0_state", side_effect=RuntimeError("simulated build failure")), \
         mock.patch("state_mutation.emit_state_mutation") as mock_emit:
        result = bts.main()

    assert result == 0
    mock_emit.assert_not_called()


def test_write_atomic_failure_does_not_emit_state_mutation(tmp_path: Path) -> None:
    """main() must NOT emit state_mutation when _write_atomic raises (blocking fix)."""
    output_path = tmp_path / "t0_state.json"
    fake_state: dict = {"schema_version": "2.0"}

    with mock.patch("sys.argv", ["build_t0_state.py", "--output", str(output_path)]), \
         mock.patch("build_t0_state.build_t0_state", return_value=fake_state), \
         mock.patch("build_t0_state._write_atomic", side_effect=OSError("disk full")), \
         mock.patch("state_mutation.emit_state_mutation") as mock_emit:
        result = bts.main()

    assert result == 0
    mock_emit.assert_not_called()


def test_state_mutation_excluded_from_recency_summary(tmp_path: Path) -> None:
    """_build_recent_receipts must filter out state_mutation events (advisory fix)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_path = state_dir / "t0_receipts.ndjson"

    events = [
        {"event_type": "task_complete", "terminal": "T1", "timestamp": "2026-04-28T10:00:00Z", "dispatch_id": "D1"},
        {"event_type": "state_mutation", "terminal": "T0", "timestamp": "2026-04-28T10:01:00Z"},
        {"event_type": "state_mutation", "terminal": "T0", "timestamp": "2026-04-28T10:02:00Z"},
        {"event_type": "review_gate_request", "terminal": "T3", "timestamp": "2026-04-28T10:03:00Z", "gate": "codex"},
        {"event_type": "state_mutation", "terminal": "T0", "timestamp": "2026-04-28T10:04:00Z"},
    ]
    receipts_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    result = bts._build_recent_receipts(state_dir, n=3)

    returned_types = [r.get("event_type") for r in result]
    assert "state_mutation" not in returned_types, f"state_mutation leaked into recency summary: {result}"
    assert "task_complete" in returned_types
    assert "review_gate_request" in returned_types


def test_idempotency_key_differs_for_different_files() -> None:
    """Same-second state_mutations for different files must produce different idempotency keys."""
    ts = "2026-04-28T10:00:00Z"
    receipt_a = {
        "timestamp": ts,
        "event_type": "state_mutation",
        "terminal": "T0",
        "source": "vnx_state",
        "file": "t0_state.json",
        "trigger": "auto_rebuild",
    }
    receipt_b = {
        "timestamp": ts,
        "event_type": "state_mutation",
        "terminal": "T0",
        "source": "vnx_state",
        "file": "t0_brief.json",
        "trigger": "auto_rebuild",
    }

    key_a = ar._compute_idempotency_key(receipt_a, "state_mutation")
    key_b = ar._compute_idempotency_key(receipt_b, "state_mutation")

    assert key_a != key_b, f"Expected different idempotency keys for different files, got same: {key_a}"


def test_non_state_mutation_idempotency_key_unchanged() -> None:
    """Adding file/trigger/section fields must not change idempotency keys for other event types."""
    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "task_complete",
        "terminal": "T1",
        "source": "pytest",
        "dispatch_id": "DISP-001",
    }

    key = ar._compute_idempotency_key(receipt, "task_complete")
    assert isinstance(key, str) and len(key) == 64

    receipt_with_extra = dict(receipt)
    del receipt_with_extra["dispatch_id"]
    receipt_with_extra["dispatch_id"] = "DISP-001"
    key2 = ar._compute_idempotency_key(receipt_with_extra, "task_complete")
    assert key == key2, "Idempotency key changed for non-state-mutation receipt"


def test_emit_state_mutation_timestamp_has_microseconds() -> None:
    """emit_state_mutation must produce microsecond-precision timestamps."""
    captured: list[dict] = []

    def _fake_append(receipt, *, skip_enrichment=False, **kwargs):
        captured.append(receipt)
        return ar.AppendResult(status="appended", receipts_file=Path("/dev/null"), idempotency_key="k")

    with mock.patch("append_receipt.append_receipt_payload", _fake_append):
        sm.emit_state_mutation("t0_state.json", trigger="auto_rebuild")

    assert len(captured) == 1
    ts = captured[0]["timestamp"]
    assert "." in ts, f"Expected microsecond-precision timestamp (with '.'), got: {ts}"


def test_same_timestamp_different_files_both_persisted(tmp_path: Path) -> None:
    """Two state_mutations with same mocked timestamp but different files must both be persisted."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = state_dir / "t0_receipts.ndjson"

    ts = "2026-04-28T10:00:00Z"

    env_patch = {
        "PROJECT_ROOT": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(SCRIPTS_DIR.parent),
        "VNX_DATA_DIR_EXPLICIT": "1",
    }

    with mock.patch.dict(os.environ, env_patch), \
         mock.patch("append_receipt.resolve_state_dir", return_value=state_dir), \
         mock.patch("append_receipt._maybe_trigger_state_rebuild"), \
         mock.patch("state_mutation._utc_now_iso", return_value=ts):
        r1 = sm.emit_state_mutation("t0_state.json", trigger="auto_rebuild")
        r2 = sm.emit_state_mutation("t0_brief.json", trigger="auto_rebuild")

    assert r1 is not None and r1.status == "appended", f"First emit failed: {r1}"
    assert r2 is not None and r2.status == "appended", f"Second emit was dropped as duplicate: {r2}"

    lines = [l for l in receipts_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, f"Expected both receipts persisted, got {len(lines)}: {lines}"
    files = {json.loads(l)["file"] for l in lines}
    assert files == {"t0_state.json", "t0_brief.json"}
