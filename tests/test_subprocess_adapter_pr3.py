#!/usr/bin/env python3
"""Tests for F28 PR-3: StreamEvent parsing and --resume support.

Tests use mock subprocesses with NDJSON fixture data — no external processes
are spawned. Covers all 7 event types, session_id extraction, --resume flag
construction, malformed line handling, and read_events() iteration.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# Add scripts/lib to path so imports work without install
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from subprocess_adapter import StreamEvent, SubprocessAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# NDJSON fixtures
# ---------------------------------------------------------------------------

FIXTURE_INIT = {"type": "init", "session_id": "ses_abc123", "model": "claude-sonnet-4-6"}
FIXTURE_THINKING = {"type": "thinking", "thinking": "Let me analyze this..."}
FIXTURE_TOOL_USE = {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/foo"}}
FIXTURE_TOOL_RESULT = {"type": "tool_result", "content": "file contents here"}
FIXTURE_TEXT = {"type": "text", "text": "Here is the answer."}
FIXTURE_RESULT = {"type": "result", "result": "Task completed.", "session_id": "ses_abc123"}
FIXTURE_ERROR = {"type": "error", "error": "Something went wrong"}

ALL_FIXTURE_EVENTS = [
    FIXTURE_INIT,
    FIXTURE_THINKING,
    FIXTURE_TOOL_USE,
    FIXTURE_TOOL_RESULT,
    FIXTURE_TEXT,
    FIXTURE_RESULT,
    FIXTURE_ERROR,
]


def make_ndjson_bytes(events: List[Dict[str, Any]]) -> bytes:
    """Encode a list of dicts as NDJSON bytes."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def make_mock_process(stdout_bytes: bytes, returncode: int = 0) -> MagicMock:
    """Return a mock Popen-like object with a readable stdout pipe."""
    mock = MagicMock()
    mock.stdout = io.BytesIO(stdout_bytes)
    mock.poll.return_value = returncode
    mock.pid = 12345
    mock.returncode = returncode
    return mock


# ---------------------------------------------------------------------------
# StreamEvent dataclass tests
# ---------------------------------------------------------------------------

class TestStreamEventDataclass(unittest.TestCase):
    def test_required_fields(self):
        evt = StreamEvent(type="text", data={"text": "hi"})
        self.assertEqual(evt.type, "text")
        self.assertEqual(evt.data, {"text": "hi"})
        self.assertIsNone(evt.session_id)
        self.assertIsInstance(evt.timestamp, float)
        self.assertGreater(evt.timestamp, 0)

    def test_session_id_field(self):
        evt = StreamEvent(type="init", data={}, session_id="ses_xyz")
        self.assertEqual(evt.session_id, "ses_xyz")

    def test_all_event_types_constructible(self):
        for etype in ("init", "thinking", "tool_use", "tool_result", "text", "result", "error"):
            evt = StreamEvent(type=etype, data={"type": etype})
            self.assertEqual(evt.type, etype)


# ---------------------------------------------------------------------------
# read_events() tests
# ---------------------------------------------------------------------------

class TestReadEvents(unittest.TestCase):
    def _adapter_with_fixture(self, events: List[Dict]) -> SubprocessAdapter:
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(make_ndjson_bytes(events))
        return adapter

    def test_all_seven_event_types_parsed(self):
        adapter = self._adapter_with_fixture(ALL_FIXTURE_EVENTS)
        events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 7)
        types = [e.type for e in events]
        self.assertEqual(types, ["init", "thinking", "tool_use", "tool_result", "text", "result", "error"])

    def test_init_event_fields(self):
        adapter = self._adapter_with_fixture([FIXTURE_INIT])
        events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.type, "init")
        self.assertEqual(evt.session_id, "ses_abc123")
        self.assertEqual(evt.data["model"], "claude-sonnet-4-6")

    def test_text_event_fields(self):
        adapter = self._adapter_with_fixture([FIXTURE_TEXT])
        events = list(adapter.read_events("T1"))
        self.assertEqual(events[0].type, "text")
        self.assertEqual(events[0].data["text"], "Here is the answer.")
        self.assertIsNone(events[0].session_id)

    def test_tool_use_event_fields(self):
        adapter = self._adapter_with_fixture([FIXTURE_TOOL_USE])
        events = list(adapter.read_events("T1"))
        self.assertEqual(events[0].type, "tool_use")
        self.assertEqual(events[0].data["name"], "Read")

    def test_error_event_fields(self):
        adapter = self._adapter_with_fixture([FIXTURE_ERROR])
        events = list(adapter.read_events("T1"))
        self.assertEqual(events[0].type, "error")
        self.assertEqual(events[0].data["error"], "Something went wrong")

    def test_empty_stdout_yields_nothing(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(b"")
        events = list(adapter.read_events("T1"))
        self.assertEqual(events, [])

    def test_no_process_yields_nothing(self):
        adapter = SubprocessAdapter()
        events = list(adapter.read_events("T1"))
        self.assertEqual(events, [])

    def test_blank_lines_skipped(self):
        ndjson = b"\n\n" + json.dumps(FIXTURE_TEXT).encode() + b"\n\n"
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(ndjson)
        events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "text")

    def test_malformed_line_skipped_no_raise(self):
        good = json.dumps(FIXTURE_TEXT).encode()
        bad = b"NOT JSON {{{broken"
        ndjson = bad + b"\n" + good + b"\n"
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(ndjson)
        with self.assertLogs("subprocess_adapter", level="WARNING") as cm:
            events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "text")
        self.assertTrue(any("malformed" in msg for msg in cm.output))

    def test_multiple_malformed_lines_all_skipped(self):
        lines = b"garbage\n{bad json\n" + json.dumps(FIXTURE_RESULT).encode() + b"\n"
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(lines)
        with self.assertLogs("subprocess_adapter", level="WARNING"):
            events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "result")


# ---------------------------------------------------------------------------
# get_session_id() tests
# ---------------------------------------------------------------------------

class TestGetSessionId(unittest.TestCase):
    def test_returns_none_before_read(self):
        adapter = SubprocessAdapter()
        self.assertIsNone(adapter.get_session_id("T1"))

    def test_extracted_from_init_event(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(make_ndjson_bytes([FIXTURE_INIT]))
        list(adapter.read_events("T1"))  # consume iterator
        self.assertEqual(adapter.get_session_id("T1"), "ses_abc123")

    def test_extracted_from_first_init_only(self):
        second_init = {"type": "init", "session_id": "ses_other", "model": "x"}
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(
            make_ndjson_bytes([FIXTURE_INIT, second_init])
        )
        list(adapter.read_events("T1"))
        # First init wins
        self.assertEqual(adapter.get_session_id("T1"), "ses_abc123")

    def test_no_session_id_if_no_init_event(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(make_ndjson_bytes([FIXTURE_TEXT]))
        list(adapter.read_events("T1"))
        self.assertIsNone(adapter.get_session_id("T1"))

    def test_init_without_session_id_field(self):
        init_no_sid = {"type": "init", "model": "claude-sonnet-4-6"}
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(make_ndjson_bytes([init_no_sid]))
        list(adapter.read_events("T1"))
        self.assertIsNone(adapter.get_session_id("T1"))

    def test_session_ids_isolated_per_terminal(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = make_mock_process(make_ndjson_bytes([FIXTURE_INIT]))
        adapter._processes["T2"] = make_mock_process(
            make_ndjson_bytes([{"type": "init", "session_id": "ses_t2_xyz"}])
        )
        list(adapter.read_events("T1"))
        list(adapter.read_events("T2"))
        self.assertEqual(adapter.get_session_id("T1"), "ses_abc123")
        self.assertEqual(adapter.get_session_id("T2"), "ses_t2_xyz")


# ---------------------------------------------------------------------------
# --resume flag in deliver() tests
# ---------------------------------------------------------------------------

class TestDeliverResumeFlag(unittest.TestCase):
    def _captured_cmd(self, **deliver_kwargs) -> List[str]:
        """Run deliver() with a mocked Popen and return the cmd list it was called with."""
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {})
        captured = {}

        def mock_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.poll.return_value = None
            mock.pid = 99
            mock.returncode = None
            mock.stdout = io.BytesIO(b"")
            mock.stderr = io.BytesIO(b"")
            return mock

        with patch("subprocess_adapter.subprocess.Popen", side_effect=mock_popen):
            adapter.deliver("T1", "dispatch-001", **deliver_kwargs)

        return captured.get("cmd", [])

    def test_no_resume_session_no_resume_flag(self):
        cmd = self._captured_cmd(instruction="do work", model="sonnet")
        self.assertNotIn("--resume", cmd)

    def test_resume_session_adds_resume_flag(self):
        cmd = self._captured_cmd(instruction="do work", model="sonnet", resume_session="ses_abc123")
        self.assertIn("--resume", cmd)
        idx = cmd.index("--resume")
        self.assertEqual(cmd[idx + 1], "ses_abc123")

    def test_resume_session_position_before_instruction(self):
        cmd = self._captured_cmd(instruction="do work", model="sonnet", resume_session="ses_abc123")
        resume_idx = cmd.index("--resume")
        instruction_idx = cmd.index("do work")
        self.assertLess(resume_idx, instruction_idx)

    def test_resume_none_omits_flag(self):
        cmd = self._captured_cmd(instruction="do work", model="sonnet", resume_session=None)
        self.assertNotIn("--resume", cmd)

    def test_output_format_stream_json_always_present(self):
        cmd = self._captured_cmd(instruction="do work", resume_session="ses_abc123")
        self.assertIn("--output-format", cmd)
        idx = cmd.index("--output-format")
        self.assertEqual(cmd[idx + 1], "stream-json")

    def test_model_flag_always_present(self):
        cmd = self._captured_cmd(instruction="do work", model="opus", resume_session="ses_abc123")
        self.assertIn("--model", cmd)
        idx = cmd.index("--model")
        self.assertEqual(cmd[idx + 1], "opus")

    def test_deliver_returns_success_with_resume(self):
        adapter = SubprocessAdapter()
        adapter.spawn("T1", {})

        def mock_popen(cmd, **kwargs):
            mock = MagicMock()
            mock.poll.return_value = None
            mock.pid = 99
            mock.returncode = None
            mock.stdout = io.BytesIO(b"")
            mock.stderr = io.BytesIO(b"")
            return mock

        with patch("subprocess_adapter.subprocess.Popen", side_effect=mock_popen):
            result = adapter.deliver(
                "T1", "dispatch-001",
                instruction="do work",
                resume_session="ses_abc123",
            )

        self.assertTrue(result.success)
        self.assertEqual(result.path_used, "subprocess")


if __name__ == "__main__":
    unittest.main()
