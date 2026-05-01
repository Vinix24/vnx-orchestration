#!/usr/bin/env python3
"""Regression tests for OI-1125: _session_ids never refreshes for new dispatches.

The bug: read_events() and read_events_with_timeout() only stored a session_id
when `terminal_id not in self._session_ids`. Long-running or resumed processes
held the session_id from their first dispatch forever; subsequent dispatches
attached work to the wrong conversation.

Fix: remove the `terminal_id not in` guard — always overwrite on init event.
"""

from __future__ import annotations

import io
import json
import select
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from subprocess_adapter import SubprocessAdapter


def _init_line(session_id: str, subtype: str = "init") -> bytes:
    """Produce a raw NDJSON init event line."""
    payload = {"type": "system", "subtype": subtype, "session_id": session_id}
    return (json.dumps(payload) + "\n").encode()


def _result_line() -> bytes:
    payload = {"type": "result", "subtype": "success", "result": "ok", "session_id": ""}
    return (json.dumps(payload) + "\n").encode()


def _fake_process(lines: list[bytes]) -> MagicMock:
    """Return a mock process whose stdout yields the given bytes lines."""
    proc = MagicMock()
    proc.stdout = io.BytesIO(b"".join(lines))
    proc.poll.return_value = None
    return proc


class TestSessionIdsRefresh:
    """OI-1125: _session_ids must update on each new init event."""

    def _make_adapter(self) -> SubprocessAdapter:
        with patch("subprocess_adapter.SubprocessAdapter._get_event_store", return_value=None):
            return SubprocessAdapter()

    def test_first_dispatch_stores_session_id(self):
        """Baseline: first init event populates _session_ids."""
        adapter = self._make_adapter()
        adapter._processes["T1"] = _fake_process([_init_line("sess-001"), _result_line()])

        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))

        assert adapter.get_session_id("T1") == "sess-001"

    def test_second_dispatch_refreshes_session_id(self):
        """Regression: second init event for same terminal must replace old session_id.

        Before the fix, `terminal_id not in self._session_ids` prevented the
        update and the old session_id was returned forever.
        """
        adapter = self._make_adapter()

        # First dispatch — session sess-001
        adapter._processes["T1"] = _fake_process([_init_line("sess-001"), _result_line()])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))
        assert adapter.get_session_id("T1") == "sess-001"

        # Second dispatch — session sess-002 (simulates process resume / new dispatch)
        adapter._processes["T1"] = _fake_process([_init_line("sess-002"), _result_line()])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))

        assert adapter.get_session_id("T1") == "sess-002", (
            "get_session_id returned stale session_id from first dispatch — "
            "OI-1125 regression: 'terminal_id not in self._session_ids' guard still present"
        )

    def test_different_terminals_independent(self):
        """Session IDs for T1 and T2 do not bleed into each other."""
        adapter = self._make_adapter()

        adapter._processes["T1"] = _fake_process([_init_line("sess-T1")])
        adapter._processes["T2"] = _fake_process([_init_line("sess-T2")])

        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))
            list(adapter.read_events("T2"))

        assert adapter.get_session_id("T1") == "sess-T1"
        assert adapter.get_session_id("T2") == "sess-T2"

    def test_no_session_id_in_non_init_event(self):
        """Non-init events must not update _session_ids."""
        adapter = self._make_adapter()

        # First init sets sess-aaa
        adapter._processes["T1"] = _fake_process([_init_line("sess-aaa")])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))
        assert adapter.get_session_id("T1") == "sess-aaa"

        # A result event with a different session_id field must not overwrite
        result_payload = {"type": "result", "subtype": "success", "result": "x", "session_id": "sess-bbb"}
        adapter._processes["T1"] = _fake_process(
            [(json.dumps(result_payload) + "\n").encode()]
        )
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))

        assert adapter.get_session_id("T1") == "sess-aaa", (
            "A non-init event's session_id field must not overwrite the stored session_id"
        )

    def test_empty_session_id_not_stored(self):
        """An init event with empty session_id must not overwrite a valid one."""
        adapter = self._make_adapter()

        # Store a good session_id
        adapter._processes["T1"] = _fake_process([_init_line("sess-valid")])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))
        assert adapter.get_session_id("T1") == "sess-valid"

        # Empty session_id in next init
        adapter._processes["T1"] = _fake_process([_init_line("")])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))

        assert adapter.get_session_id("T1") == "sess-valid", (
            "Empty session_id in init event must not clear a valid stored session_id"
        )

    def test_read_events_with_timeout_also_refreshes(self):
        """read_events_with_timeout must also refresh _session_ids on new dispatch."""
        adapter = self._make_adapter()

        # First dispatch via read_events
        adapter._processes["T1"] = _fake_process([_init_line("sess-first")])
        with patch.object(adapter, "_get_event_store", return_value=None):
            list(adapter.read_events("T1"))
        assert adapter.get_session_id("T1") == "sess-first"

        # Second dispatch via read_events_with_timeout — we need to mock select()
        second_lines = [_init_line("sess-second"), _result_line()]
        proc = _fake_process(second_lines)
        # Make stdout return lines one-by-one via readline
        buf = io.BytesIO(b"".join(second_lines))
        proc.stdout = buf
        proc.stdout.fileno = lambda: 999  # dummy fd for select mock
        adapter._processes["T1"] = proc
        adapter._timed_out.discard("T1")

        # Patch select to always report ready, and readline to consume buf
        with patch("subprocess_adapter.select") as mock_select, \
             patch.object(adapter, "_get_event_store", return_value=None):
            call_count = [0]
            def fake_select(rlist, wlist, xlist, timeout):
                call_count[0] += 1
                line = buf.readline()
                if not line:
                    return [], [], []
                buf.seek(buf.tell() - len(line))  # put it back
                return [999], [], []

            # Use real BytesIO readline by routing through the buffer
            buf2 = io.BytesIO(b"".join(second_lines))
            proc.stdout.readline = buf2.readline
            mock_select.select = lambda *a, **kw: ([999], [], [])

            list(adapter.read_events_with_timeout("T1", chunk_timeout=10, total_deadline=30))

        assert adapter.get_session_id("T1") == "sess-second", (
            "read_events_with_timeout did not refresh session_id — "
            "OI-1125 regression in timeout path"
        )
