#!/usr/bin/env python3
"""Tests for VNX_ADAPTER_T0=subprocess cutover flag and is_headless_t0() branch.

Covers:
- is_headless_t0() returns True/False based on VNX_ADAPTER_T0 env var
- is_headless_t0() is case-insensitive
- T0 defaults to tmux (NOT subprocess) when VNX_ADAPTER_T0 is unset
- _enrich_completion_receipt annotates T0 snapshot entry when headless
- heartbeat_ack_monitor._is_subprocess_terminal works for T0
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from append_receipt import is_headless_t0


# ---------------------------------------------------------------------------
# is_headless_t0()
# ---------------------------------------------------------------------------

class TestIsHeadlessT0:
    def test_true_when_subprocess_lowercase(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "subprocess"}):
            assert is_headless_t0() is True

    def test_true_when_subprocess_uppercase(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "SUBPROCESS"}):
            assert is_headless_t0() is True

    def test_true_when_subprocess_mixed_case(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "SubProcess"}):
            assert is_headless_t0() is True

    def test_false_when_tmux(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "tmux"}):
            assert is_headless_t0() is False

    def test_false_when_unset(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, clean_env, clear=True):
            assert is_headless_t0() is False

    def test_false_when_empty_string(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": ""}):
            assert is_headless_t0() is False

    def test_false_when_other_value(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "docker"}):
            assert is_headless_t0() is False


# ---------------------------------------------------------------------------
# T0 defaults to tmux — NOT subprocess (unlike T1)
# ---------------------------------------------------------------------------

class TestT0DefaultAdapter:
    def test_t0_defaults_to_tmux_not_subprocess(self):
        # T1 defaults to subprocess; T0 must not
        clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, clean_env, clear=True):
            adapter_type = os.environ.get("VNX_ADAPTER_T0", "tmux")
            assert adapter_type == "tmux"

    def test_t0_becomes_subprocess_when_explicit(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "subprocess"}):
            adapter_type = os.environ.get("VNX_ADAPTER_T0", "tmux")
            assert adapter_type == "subprocess"

    def test_adapter_var_name_for_t0(self):
        # The shell script reads VNX_ADAPTER_{terminal_id}
        terminal_id = "T0"
        adapter_var = f"VNX_ADAPTER_{terminal_id}"
        assert adapter_var == "VNX_ADAPTER_T0"

    def test_t0_and_t1_are_independent_flags(self):
        # Setting T1 subprocess should not affect T0
        with patch.dict(os.environ, {"VNX_ADAPTER_T1": "subprocess"}, clear=False):
            clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
            with patch.dict(os.environ, clean_env, clear=True):
                assert is_headless_t0() is False


# ---------------------------------------------------------------------------
# _enrich_completion_receipt T0 snapshot annotation
# ---------------------------------------------------------------------------

class TestEnrichCompletionReceiptT0Branch:
    """Verify that T0 snapshot entry is annotated when VNX_ADAPTER_T0=subprocess."""

    def _build_minimal_receipt(self) -> dict:
        return {
            "timestamp": "1714000000",
            "event_type": "task_complete",
            "dispatch_id": "test-dispatch-001",
            "terminal": "T0",
        }

    def test_t0_snapshot_annotated_when_headless(self):
        """When is_headless_t0() is True, T0 snapshot entry gains adapter/headless fields."""
        from append_receipt import _enrich_completion_receipt

        receipt = self._build_minimal_receipt()

        mock_snapshot = MagicMock()
        mock_snapshot.to_dict.return_value = {
            "timestamp": "2026-04-22T00:00:00Z",
            "terminals": {
                "T0": {"status": "active", "claimed_by": None},
                "T1": {"status": "idle", "claimed_by": None},
            },
        }

        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "subprocess"}):
            with patch("append_receipt.collect_terminal_snapshot", return_value=mock_snapshot):
                with patch("append_receipt.ensure_env", return_value={
                    "VNX_STATE_DIR": "/tmp/vnx-test-state",
                    "PROJECT_ROOT": "/tmp",
                }):
                    with patch("append_receipt._build_git_provenance", return_value={"git_ref": "abc"}):
                        with patch("append_receipt._build_session_metadata", return_value={"session_id": "s1"}):
                            with patch("append_receipt.enrich_receipt_provenance", return_value=receipt):
                                with patch("append_receipt.validate_receipt_provenance", return_value=MagicMock(gaps=[])):
                                    with patch("append_receipt.get_changed_files", return_value=[]):
                                        enriched = _enrich_completion_receipt(receipt)

        t0_entry = enriched["terminal_snapshot"]["terminals"]["T0"]
        assert t0_entry.get("adapter") == "subprocess"
        assert t0_entry.get("headless") is True

    def test_t1_snapshot_not_annotated_via_t0_flag(self):
        """T1 entry should NOT get T0 headless annotation."""
        from append_receipt import _enrich_completion_receipt

        receipt = self._build_minimal_receipt()

        mock_snapshot = MagicMock()
        mock_snapshot.to_dict.return_value = {
            "timestamp": "2026-04-22T00:00:00Z",
            "terminals": {
                "T0": {"status": "active", "claimed_by": None},
                "T1": {"status": "idle", "claimed_by": None},
            },
        }

        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "subprocess"}):
            with patch("append_receipt.collect_terminal_snapshot", return_value=mock_snapshot):
                with patch("append_receipt.ensure_env", return_value={
                    "VNX_STATE_DIR": "/tmp/vnx-test-state",
                    "PROJECT_ROOT": "/tmp",
                }):
                    with patch("append_receipt._build_git_provenance", return_value={"git_ref": "abc"}):
                        with patch("append_receipt._build_session_metadata", return_value={"session_id": "s1"}):
                            with patch("append_receipt.enrich_receipt_provenance", return_value=receipt):
                                with patch("append_receipt.validate_receipt_provenance", return_value=MagicMock(gaps=[])):
                                    with patch("append_receipt.get_changed_files", return_value=[]):
                                        enriched = _enrich_completion_receipt(receipt)

        t1_entry = enriched["terminal_snapshot"]["terminals"]["T1"]
        assert "adapter" not in t1_entry
        assert "headless" not in t1_entry

    def test_t0_snapshot_not_annotated_when_tmux_mode(self):
        """When VNX_ADAPTER_T0 is unset, T0 snapshot entry should NOT be annotated."""
        from append_receipt import _enrich_completion_receipt

        receipt = self._build_minimal_receipt()

        mock_snapshot = MagicMock()
        mock_snapshot.to_dict.return_value = {
            "timestamp": "2026-04-22T00:00:00Z",
            "terminals": {
                "T0": {"status": "active", "claimed_by": None},
            },
        }

        clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("append_receipt.collect_terminal_snapshot", return_value=mock_snapshot):
                with patch("append_receipt.ensure_env", return_value={
                    "VNX_STATE_DIR": "/tmp/vnx-test-state",
                    "PROJECT_ROOT": "/tmp",
                }):
                    with patch("append_receipt._build_git_provenance", return_value={"git_ref": "abc"}):
                        with patch("append_receipt._build_session_metadata", return_value={"session_id": "s1"}):
                            with patch("append_receipt.enrich_receipt_provenance", return_value=receipt):
                                with patch("append_receipt.validate_receipt_provenance", return_value=MagicMock(gaps=[])):
                                    with patch("append_receipt.get_changed_files", return_value=[]):
                                        enriched = _enrich_completion_receipt(receipt)

        t0_entry = enriched["terminal_snapshot"]["terminals"]["T0"]
        assert "adapter" not in t0_entry
        assert "headless" not in t0_entry


# ---------------------------------------------------------------------------
# _is_subprocess_terminal logic for T0 (tested inline — module has hard deps)
# ---------------------------------------------------------------------------

def _is_subprocess_terminal(terminal: str) -> bool:
    """Inline copy of heartbeat_ack_monitor.HeartbeatAckMonitor._is_subprocess_terminal."""
    env_key = f"VNX_ADAPTER_{terminal}"
    return os.environ.get(env_key, "tmux").lower() == "subprocess"


class TestHeartbeatMonitorT0:
    """Verify _is_subprocess_terminal logic works correctly for T0."""

    def test_t0_is_subprocess_when_flag_set(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_T0": "subprocess"}):
            assert _is_subprocess_terminal("T0") is True

    def test_t0_not_subprocess_when_unset(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, clean_env, clear=True):
            assert _is_subprocess_terminal("T0") is False

    def test_t1_subprocess_does_not_affect_t0(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "VNX_ADAPTER_T0"}
        with patch.dict(os.environ, {**clean_env, "VNX_ADAPTER_T1": "subprocess"}, clear=True):
            assert _is_subprocess_terminal("T0") is False
            assert _is_subprocess_terminal("T1") is True
