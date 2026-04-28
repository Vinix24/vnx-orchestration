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
