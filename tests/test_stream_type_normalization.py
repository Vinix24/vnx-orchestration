#!/usr/bin/env python3
"""Tests for CLI-to-dashboard event type normalization in SubprocessAdapter.

Covers the _normalize_cli_event() static method and the integration with
read_events() for real CLI stream-json payloads (system, assistant, user,
result, rate_limit_event).
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from subprocess_adapter import SubprocessAdapter


# ---------------------------------------------------------------------------
# Real CLI payload fixtures (from burn-in NDJSON)
# ---------------------------------------------------------------------------

CLI_SYSTEM_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "abc-123",
    "model": "claude-haiku-4-5-20251001",
    "cwd": "/tmp",
    "tools": ["Bash", "Read"],
}

CLI_ASSISTANT_THINKING = {
    "type": "assistant",
    "message": {
        "model": "claude-haiku-4-5-20251001",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Let me analyze this..."},
        ],
    },
}

CLI_ASSISTANT_TOOL_USE = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_01X", "name": "Read", "input": {"file_path": "/tmp/foo"}},
        ],
    },
}

CLI_ASSISTANT_TEXT = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Here is the answer."},
        ],
    },
}

CLI_ASSISTANT_MULTI_BLOCK = {
    "type": "assistant",
    "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Thinking first..."},
            {"type": "tool_use", "id": "toolu_02Y", "name": "Glob", "input": {"pattern": "*.py"}},
        ],
    },
}

CLI_USER_TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01X", "content": "file contents here"},
        ],
    },
}

CLI_USER_TOOL_RESULT_LIST_CONTENT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_03Z",
                "content": [
                    {"type": "text", "text": "line 1"},
                    {"type": "text", "text": "line 2"},
                ],
            },
        ],
    },
}

CLI_RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "Task completed successfully.",
    "session_id": "abc-123",
}

CLI_RATE_LIMIT = {
    "type": "rate_limit_event",
    "retry_after": 1.5,
}

CLI_ASSISTANT_EMPTY_CONTENT = {
    "type": "assistant",
    "message": {"role": "assistant", "content": []},
}

CLI_USER_NO_TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "text", "text": "some user text"},
        ],
    },
}


# ---------------------------------------------------------------------------
# _normalize_cli_event() unit tests
# ---------------------------------------------------------------------------

class TestNormalizeCliEvent(unittest.TestCase):
    def test_system_init_becomes_init(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_SYSTEM_INIT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "init")
        self.assertEqual(result[0]["data"]["session_id"], "abc-123")
        self.assertEqual(result[0]["data"]["model"], "claude-haiku-4-5-20251001")

    def test_assistant_thinking_becomes_thinking(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_ASSISTANT_THINKING)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "thinking")
        self.assertEqual(result[0]["data"]["thinking"], "Let me analyze this...")

    def test_assistant_tool_use_becomes_tool_use(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_ASSISTANT_TOOL_USE)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "tool_use")
        self.assertEqual(result[0]["data"]["name"], "Read")
        self.assertEqual(result[0]["data"]["id"], "toolu_01X")
        self.assertEqual(result[0]["data"]["input"]["file_path"], "/tmp/foo")

    def test_assistant_text_becomes_text(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_ASSISTANT_TEXT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "text")
        self.assertEqual(result[0]["data"]["text"], "Here is the answer.")

    def test_assistant_multi_block_splits(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_ASSISTANT_MULTI_BLOCK)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["type"], "thinking")
        self.assertEqual(result[1]["type"], "tool_use")
        self.assertEqual(result[1]["data"]["name"], "Glob")

    def test_user_tool_result_becomes_tool_result(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_USER_TOOL_RESULT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "tool_result")
        self.assertEqual(result[0]["data"]["tool_use_id"], "toolu_01X")
        self.assertEqual(result[0]["data"]["content"], "file contents here")

    def test_user_tool_result_list_content_joined(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_USER_TOOL_RESULT_LIST_CONTENT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "tool_result")
        self.assertIn("line 1", result[0]["data"]["content"])
        self.assertIn("line 2", result[0]["data"]["content"])

    def test_result_normalized(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_RESULT)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "result")
        self.assertEqual(result[0]["data"]["text"], "Task completed successfully.")
        self.assertEqual(result[0]["data"]["subtype"], "success")

    def test_rate_limit_event_skipped(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_RATE_LIMIT)
        self.assertEqual(result, [])

    def test_assistant_empty_content_returns_empty(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_ASSISTANT_EMPTY_CONTENT)
        self.assertEqual(result, [])

    def test_user_without_tool_result_returns_empty(self):
        result = SubprocessAdapter._normalize_cli_event(CLI_USER_NO_TOOL_RESULT)
        self.assertEqual(result, [])

    def test_already_normalized_init_passes_through(self):
        payload = {"type": "init", "session_id": "ses_x", "model": "sonnet"}
        result = SubprocessAdapter._normalize_cli_event(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "init")
        self.assertEqual(result[0]["data"]["session_id"], "ses_x")

    def test_already_normalized_error_passes_through(self):
        payload = {"type": "error", "error": "boom"}
        result = SubprocessAdapter._normalize_cli_event(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "error")
        self.assertEqual(result[0]["data"]["error"], "boom")

    def test_unknown_type_passes_through(self):
        payload = {"type": "custom_event", "foo": "bar"}
        result = SubprocessAdapter._normalize_cli_event(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "custom_event")


# ---------------------------------------------------------------------------
# read_events() integration with real CLI payloads
# ---------------------------------------------------------------------------

def _make_ndjson_bytes(events):
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _mock_process(events):
    mock = MagicMock()
    mock.stdout = io.BytesIO(_make_ndjson_bytes(events))
    mock.poll.return_value = 0
    mock.pid = 99
    mock.returncode = 0
    return mock


class TestReadEventsCliPayloads(unittest.TestCase):
    def test_full_cli_conversation_normalized(self):
        """Simulates a real CLI conversation: init → thinking → tool_use → tool_result → text → result."""
        cli_events = [
            CLI_SYSTEM_INIT,
            CLI_ASSISTANT_THINKING,
            CLI_ASSISTANT_TOOL_USE,
            CLI_USER_TOOL_RESULT,
            CLI_ASSISTANT_TEXT,
            CLI_RESULT,
        ]
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _mock_process(cli_events)
        events = list(adapter.read_events("T1"))

        types = [e.type for e in events]
        self.assertEqual(types, ["init", "thinking", "tool_use", "tool_result", "text", "result"])

    def test_session_id_extracted_from_system_init(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _mock_process([CLI_SYSTEM_INIT])
        list(adapter.read_events("T1"))
        self.assertEqual(adapter.get_session_id("T1"), "abc-123")

    def test_rate_limit_events_filtered(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _mock_process([
            CLI_SYSTEM_INIT,
            CLI_RATE_LIMIT,
            CLI_ASSISTANT_TEXT,
        ])
        events = list(adapter.read_events("T1"))
        types = [e.type for e in events]
        self.assertNotIn("rate_limit_event", types)
        self.assertEqual(types, ["init", "text"])

    def test_multi_block_assistant_splits_into_separate_events(self):
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _mock_process([CLI_ASSISTANT_MULTI_BLOCK])
        events = list(adapter.read_events("T1"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, "thinking")
        self.assertEqual(events[1].type, "tool_use")

    def test_normalized_data_fields_match_dashboard_expectations(self):
        """Verify the data fields that renderEventContent() reads."""
        adapter = SubprocessAdapter()
        adapter._processes["T1"] = _mock_process([
            CLI_SYSTEM_INIT,
            CLI_ASSISTANT_THINKING,
            CLI_ASSISTANT_TOOL_USE,
            CLI_USER_TOOL_RESULT,
            CLI_ASSISTANT_TEXT,
            CLI_RESULT,
        ])
        events = list(adapter.read_events("T1"))

        # init: data.session_id
        self.assertIn("session_id", events[0].data)

        # thinking: data.thinking
        self.assertIn("thinking", events[1].data)

        # tool_use: data.name
        self.assertEqual(events[2].data["name"], "Read")

        # tool_result: data.content
        self.assertEqual(events[3].data["content"], "file contents here")

        # text: data.text
        self.assertEqual(events[4].data["text"], "Here is the answer.")

        # result: data.text
        self.assertEqual(events[5].data["text"], "Task completed successfully.")


if __name__ == "__main__":
    unittest.main()
