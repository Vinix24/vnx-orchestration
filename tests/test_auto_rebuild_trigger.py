#!/usr/bin/env python3
"""Tests for auto-rebuild trigger in append_receipt.py.

Coverage:
  1. Completion event triggers rebuild (subprocess.Popen called)
  2. Non-completion event does NOT trigger rebuild
  3. Throttle prevents second rebuild within 30s
  4. Rebuild failure does not break receipt append
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


def _minimal_receipt(event_type: str = "task_complete", dispatch_id: str = "DISP-001") -> dict:
    return {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": event_type,
        "terminal": "T1",
        "source": "pytest",
        "dispatch_id": dispatch_id,
    }


def test_completion_event_triggers_rebuild(tmp_path: Path) -> None:
    receipt = _minimal_receipt("task_complete")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen") as mock_popen:
        ar._maybe_trigger_state_rebuild(receipt)

    mock_popen.assert_called_once()
    call_kwargs = mock_popen.call_args
    assert call_kwargs[0][0] == ["python3", "scripts/build_t0_state.py"]
    assert call_kwargs[1].get("start_new_session") is True


def test_non_completion_event_does_not_trigger_rebuild(tmp_path: Path) -> None:
    receipt = _minimal_receipt("task_started")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen") as mock_popen:
        ar._maybe_trigger_state_rebuild(receipt)

    mock_popen.assert_not_called()


def test_dispatch_promoted_event_triggers_rebuild(tmp_path: Path) -> None:
    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "dispatch_promoted",
        "terminal": "T0",
        "source": "pytest",
    }

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen") as mock_popen:
        ar._maybe_trigger_state_rebuild(receipt)

    mock_popen.assert_called_once()


def test_throttle_prevents_double_rebuild(tmp_path: Path) -> None:
    throttle_file = tmp_path / ".last_state_rebuild_ts"
    throttle_file.write_text(str(time.time()), encoding="utf-8")

    receipt = _minimal_receipt("task_complete")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen") as mock_popen:
        ar._maybe_trigger_state_rebuild(receipt)

    mock_popen.assert_not_called()


def test_throttle_allows_rebuild_after_window(tmp_path: Path) -> None:
    throttle_file = tmp_path / ".last_state_rebuild_ts"
    old_ts = time.time() - ar._REBUILD_THROTTLE_SECONDS - 5
    throttle_file.write_text(str(old_ts), encoding="utf-8")

    receipt = _minimal_receipt("task_complete")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen") as mock_popen:
        ar._maybe_trigger_state_rebuild(receipt)

    mock_popen.assert_called_once()


def test_rebuild_failure_does_not_raise(tmp_path: Path) -> None:
    receipt = _minimal_receipt("task_complete")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen", side_effect=OSError("popen failed")):
        ar._maybe_trigger_state_rebuild(receipt)


def test_popen_failure_does_not_write_throttle(tmp_path: Path) -> None:
    """Throttle file must NOT be written when Popen raises (advisory fix)."""
    throttle_file = tmp_path / ".last_state_rebuild_ts"
    receipt = _minimal_receipt("task_complete")

    with mock.patch("append_receipt.resolve_state_dir", return_value=tmp_path), \
         mock.patch("append_receipt.subprocess.Popen", side_effect=OSError("popen failed")):
        ar._maybe_trigger_state_rebuild(receipt)

    assert not throttle_file.exists(), "throttle file must not be written on Popen failure"


def test_rebuild_failure_does_not_break_append(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_file = str(state_dir / "t0_receipts.ndjson")

    receipt = _minimal_receipt("task_complete")

    env_patch = {
        "PROJECT_ROOT": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(SCRIPTS_DIR.parent),
        "VNX_DATA_DIR_EXPLICIT": "1",
    }

    with mock.patch.dict(os.environ, env_patch), \
         mock.patch("append_receipt.resolve_state_dir", return_value=state_dir), \
         mock.patch("append_receipt._enrich_completion_receipt", side_effect=lambda r: r), \
         mock.patch("append_receipt._register_quality_open_items", return_value=0), \
         mock.patch("append_receipt._update_confidence_from_receipt"), \
         mock.patch("append_receipt.subprocess.Popen", side_effect=OSError("boom")):
        result = ar.append_receipt_payload(receipt, receipts_file=receipts_file)

    assert result.status == "appended"
