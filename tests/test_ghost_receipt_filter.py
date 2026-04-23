"""Tests for ghost receipt detection and gate-event stream routing.

Ghost receipts: receipts with dispatch_id="unknown" from headless gate runners.
These should be routed to gate_events.ndjson, not t0_receipts.ndjson.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from ghost_receipt_filter import (
    GATE_EVENTS_FILENAME,
    gate_events_file,
    is_gate_event,
    is_ghost_dispatch_id,
    should_route_to_gate_stream,
)


# ---------------------------------------------------------------------------
# is_ghost_dispatch_id
# ---------------------------------------------------------------------------

class TestIsGhostDispatchId:
    def test_none_is_ghost(self):
        assert is_ghost_dispatch_id(None) is True

    def test_empty_string_is_ghost(self):
        assert is_ghost_dispatch_id("") is True

    def test_unknown_string_is_ghost(self):
        assert is_ghost_dispatch_id("unknown") is True

    def test_unknown_uppercase_is_ghost(self):
        assert is_ghost_dispatch_id("UNKNOWN") is True

    def test_null_string_is_ghost(self):
        assert is_ghost_dispatch_id("null") is True

    def test_none_string_is_ghost(self):
        assert is_ghost_dispatch_id("none") is True

    def test_whitespace_only_is_ghost(self):
        assert is_ghost_dispatch_id("   ") is True

    def test_real_dispatch_id_not_ghost(self):
        assert is_ghost_dispatch_id("20260423-070100-ghost-receipt-filter-B") is False

    def test_short_id_not_ghost(self):
        assert is_ghost_dispatch_id("abc-123") is False


# ---------------------------------------------------------------------------
# is_gate_event
# ---------------------------------------------------------------------------

class TestIsGateEvent:
    def test_known_gate_name_is_gate_event(self):
        assert is_gate_event({"gate": "gemini_review"}) is True

    def test_codex_gate_is_gate_event(self):
        assert is_gate_event({"gate": "codex_gate"}) is True

    def test_unknown_gate_not_gate_event(self):
        assert is_gate_event({"gate": "unknown"}) is False

    def test_empty_gate_not_gate_event(self):
        assert is_gate_event({"gate": ""}) is False

    def test_missing_gate_not_gate_event(self):
        assert is_gate_event({}) is False

    def test_headless_terminal_is_gate_event(self):
        assert is_gate_event({"terminal": "HEADLESS"}) is True

    def test_headless_prefixed_terminal_is_gate_event(self):
        assert is_gate_event({"terminal": "HEADLESS-gemini"}) is True

    def test_normal_terminal_without_gate_not_gate_event(self):
        assert is_gate_event({"terminal": "T1"}) is False

    def test_report_file_with_headless_marker_is_gate_event(self):
        assert is_gate_event({"report_file": "20260423-043226-HEADLESS-gemini_review-pr-254.md"}) is True

    def test_report_path_with_headless_marker_is_gate_event(self):
        assert is_gate_event({"report_path": "/reports/20260423-HEADLESS-codex_gate-pr-254.md"}) is True

    def test_normal_report_not_gate_event(self):
        assert is_gate_event({"report_path": "/reports/20260423-A-completion.md"}) is False

    def test_custom_gate_name_is_gate_event(self):
        assert is_gate_event({"gate": "review_gate"}) is True


# ---------------------------------------------------------------------------
# should_route_to_gate_stream
# ---------------------------------------------------------------------------

class TestShouldRouteToGateStream:
    def test_ghost_gate_receipt_routes_to_gate_stream(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-01T07:54:26Z",
            "terminal": "unknown",
            "dispatch_id": "unknown",
            "gate": "codex_gate",
            "status": "success",
        }
        assert should_route_to_gate_stream(receipt) is True

    def test_ghost_gemini_review_routes_to_gate_stream(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-23T04:30:00Z",
            "terminal": "HEADLESS",
            "dispatch_id": "unknown",
            "gate": "gemini_review",
            "pr_id": "PR-254",
        }
        assert should_route_to_gate_stream(receipt) is True

    def test_valid_dispatch_id_not_routed(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-01T07:54:26Z",
            "terminal": "T1",
            "dispatch_id": "20260401-123456-feature-A",
            "gate": "codex_gate",
        }
        assert should_route_to_gate_stream(receipt) is False

    def test_ghost_non_gate_receipt_not_routed(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-01T07:54:26Z",
            "terminal": "unknown",
            "dispatch_id": "unknown",
        }
        assert should_route_to_gate_stream(receipt) is False

    def test_none_dispatch_id_gate_event_routes(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-01T07:54:26Z",
            "dispatch_id": None,
            "gate": "gemini_review",
        }
        assert should_route_to_gate_stream(receipt) is True

    def test_headless_report_filename_routes(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-23T04:30:48Z",
            "dispatch_id": "unknown",
            "report_file": "20260423-043226-HEADLESS-gemini_review-pr-254.md",
        }
        assert should_route_to_gate_stream(receipt) is True

    def test_real_worker_receipt_not_routed(self):
        receipt = {
            "event_type": "task_complete",
            "timestamp": "2026-04-23T04:30:48Z",
            "terminal": "T2",
            "dispatch_id": "20260423-070100-ghost-receipt-filter-B",
            "status": "success",
        }
        assert should_route_to_gate_stream(receipt) is False


# ---------------------------------------------------------------------------
# gate_events_file
# ---------------------------------------------------------------------------

class TestGateEventsFile:
    def test_returns_path_in_state_dir(self, tmp_path):
        result = gate_events_file(tmp_path)
        assert result == tmp_path / GATE_EVENTS_FILENAME

    def test_filename_is_ndjson(self, tmp_path):
        result = gate_events_file(tmp_path)
        assert result.suffix == ".ndjson"

    def test_constant_matches_filename(self):
        assert GATE_EVENTS_FILENAME == "gate_events.ndjson"


# ---------------------------------------------------------------------------
# Integration: append_receipt routes ghost gate events correctly
# ---------------------------------------------------------------------------

class TestAppendReceiptRouting:
    """Integration tests verifying ghost receipts go to gate_events.ndjson."""

    def _make_ghost_gate_receipt(self) -> dict:
        return {
            "event_type": "task_complete",
            "timestamp": "2026-04-23T04:30:48Z",
            "terminal": "HEADLESS",
            "dispatch_id": "unknown",
            "gate": "codex_gate",
            "pr_id": "PR-254",
            "status": "success",
            "report_path": "/tmp/reports/20260423-HEADLESS-codex_gate-pr-254.md",
        }

    def _make_valid_receipt(self) -> dict:
        return {
            "event_type": "task_complete",
            "timestamp": "2026-04-23T05:00:00Z",
            "terminal": "T2",
            "dispatch_id": "20260423-070100-ghost-receipt-filter-B",
            "status": "success",
            "report_path": "/tmp/reports/20260423-B-filter.md",
        }

    def test_ghost_gate_receipt_written_to_gate_events(self, tmp_path, monkeypatch):
        import os
        import sys
        import importlib

        gate_file = tmp_path / "gate_events.ndjson"
        main_file = tmp_path / "t0_receipts.ndjson"

        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_HOME", str(tmp_path))

        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        sys.path.insert(0, str(SCRIPTS_DIR))

        # Import after env is patched
        import append_receipt as ar
        import importlib
        importlib.reload(ar)

        receipt = self._make_ghost_gate_receipt()

        ar.append_receipt_payload(receipt, receipts_file=str(gate_file))

        assert gate_file.exists(), "gate_events.ndjson was not created"
        lines = [l for l in gate_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        written = json.loads(lines[0])
        assert written.get("gate") == "codex_gate"

    def test_valid_receipt_not_written_to_gate_events(self, tmp_path, monkeypatch):
        import sys

        main_file = tmp_path / "t0_receipts.ndjson"
        gate_file = tmp_path / "gate_events.ndjson"

        monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_HOME", str(tmp_path))

        sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
        sys.path.insert(0, str(SCRIPTS_DIR))

        import append_receipt as ar
        import importlib
        importlib.reload(ar)

        receipt = self._make_valid_receipt()
        ar.append_receipt_payload(receipt, receipts_file=str(main_file))

        # gate_events should be absent or empty
        if gate_file.exists():
            lines = [l for l in gate_file.read_text().splitlines() if l.strip()]
            assert len(lines) == 0, "Valid receipt should not appear in gate_events"
