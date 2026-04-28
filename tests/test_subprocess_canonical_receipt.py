#!/usr/bin/env python3
"""Tests for VNX-R4: subprocess receipts routed through canonical append_receipt_payload."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import subprocess_dispatch as sd


def _make_append_result(status="appended", path=None):
    """Return a mock AppendResult-like object."""
    result = MagicMock()
    result.status = status
    result.receipts_file = path or Path("/tmp/test-state/t0_receipts.ndjson")
    return result


class TestCanonicalReceiptPath:
    """Test that _write_receipt routes through append_receipt_payload."""

    def test_canonical_path_called_on_normal_flow(self, tmp_path):
        """append_receipt_payload is called (not bare write) in normal flow."""
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            result_path = sd._write_receipt(
                dispatch_id="test-dispatch-001",
                terminal_id="T1",
                status="success",
            )

        append_mod.append_receipt_payload.assert_called_once()
        call_args = append_mod.append_receipt_payload.call_args[0][0]
        assert call_args["dispatch_id"] == "test-dispatch-001"
        assert call_args["terminal"] == "T1"
        assert call_args["status"] == "success"
        assert call_args["event_type"] == "subprocess_completion"
        assert call_args["source"] == "subprocess"
        assert result_path == expected_path

    def test_register_event_emitted_via_canonical_path(self, tmp_path):
        """dispatch_register events fire because append_receipt_payload is called."""
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-dispatch-002",
                terminal_id="T2",
                status="success",
                event_count=5,
            )

        # append_receipt_payload is responsible for emitting register events;
        # assert it was called with a receipt that has event_count
        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["event_count"] == 5
        assert receipt_arg["dispatch_id"] == "test-dispatch-002"

    def test_fallback_to_bare_write_on_import_error(self, tmp_path):
        """If append_receipt_payload import fails, falls back to bare write without losing receipt."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        receipt_file = state_dir / "t0_receipts.ndjson"

        # Simulate import failure by raising on import
        with patch.dict("sys.modules", {"append_receipt": None}), \
             patch.object(sd, "_default_state_dir", return_value=state_dir):
            result_path = sd._write_receipt(
                dispatch_id="test-dispatch-003",
                terminal_id="T1",
                status="failed",
                failure_reason="timeout",
            )

        # Bare write should have created the file
        assert result_path == receipt_file
        assert receipt_file.exists()
        line = receipt_file.read_text().strip()
        receipt_data = json.loads(line)
        assert receipt_data["dispatch_id"] == "test-dispatch-003"
        assert receipt_data["status"] == "failed"
        assert receipt_data["failure_reason"] == "timeout"
        assert receipt_data["event_type"] == "subprocess_completion"

    def test_subprocess_completion_event_type_preserved(self, tmp_path):
        """Receipt event_type remains 'subprocess_completion' through canonical path."""
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-dispatch-004",
                terminal_id="T3",
                status="success",
                committed=True,
                commit_hash_before="abc123",
                commit_hash_after="def456",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["event_type"] == "subprocess_completion"
        assert receipt_arg["committed"] is True
        assert receipt_arg["commit_hash_before"] == "abc123"
        assert receipt_arg["commit_hash_after"] == "def456"

    def test_idempotent_duplicate_does_not_raise(self, tmp_path):
        """When append_receipt_payload returns status='duplicate', function completes normally."""
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="duplicate", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            result_path = sd._write_receipt(
                dispatch_id="test-dispatch-005",
                terminal_id="T1",
                status="success",
            )

        assert result_path == expected_path
        append_mod.append_receipt_payload.assert_called_once()
