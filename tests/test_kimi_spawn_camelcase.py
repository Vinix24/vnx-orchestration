"""Tests for Kimi CLI Wire Protocol v1.26+ camelCase event handling.

Validates dual-format support: legacy lowercase events (pre-v1.26) still work,
and camelCase Wire Protocol events (TurnBegin, ContentPart, TextPart, etc.)
are correctly normalized to CanonicalEvent types.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from canonical_event import CanonicalEvent  # noqa: E402
from canonical_event import _from_kimi_event  # noqa: E402
from provider_spawns.kimi_spawn import (  # noqa: E402
    KimiSpawnResult,
    normalize_kimi_event,
    spawn_kimi,
)


# ---------------------------------------------------------------------------
# Unit tests: normalize_kimi_event — camelCase Wire Protocol
# ---------------------------------------------------------------------------


class TestNormalizeCamelCaseTurnBegin(unittest.TestCase):
    def test_maps_to_text_with_empty_content(self):
        raw = {"event_type": "TurnBegin"}
        event = normalize_kimi_event(raw, "T1", "d-cc-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")
        self.assertEqual(event.provider, "kimi")

    def test_preserves_dispatch_and_terminal(self):
        raw = {"event_type": "TurnBegin"}
        event = normalize_kimi_event(raw, "T3", "dispatch-xyz")
        self.assertEqual(event.terminal_id, "T3")
        self.assertEqual(event.dispatch_id, "dispatch-xyz")
        self.assertEqual(event.observability_tier, 1)


class TestNormalizeCamelCaseStepBegin(unittest.TestCase):
    def test_maps_to_text_with_empty_content(self):
        raw = {"event_type": "StepBegin"}
        event = normalize_kimi_event(raw, "T1", "d-cc-02")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")


class TestNormalizeCamelCaseContentPart(unittest.TestCase):
    def test_maps_to_text_with_content_field(self):
        raw = {"event_type": "ContentPart", "content": "Here is the analysis."}
        event = normalize_kimi_event(raw, "T1", "d-cc-03")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Here is the analysis.")

    def test_falls_back_to_text_field(self):
        raw = {"event_type": "ContentPart", "text": "Fallback text."}
        event = normalize_kimi_event(raw, "T1", "d-cc-03")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Fallback text.")

    def test_handles_missing_content(self):
        raw = {"event_type": "ContentPart"}
        event = normalize_kimi_event(raw, "T1", "d-cc-03")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")


class TestNormalizeCamelCaseThinkPart(unittest.TestCase):
    def test_maps_to_thinking_with_content_field(self):
        raw = {"event_type": "ThinkPart", "content": "Let me reason about this..."}
        event = normalize_kimi_event(raw, "T1", "d-cc-04")
        self.assertEqual(event.event_type, "thinking")
        self.assertEqual(event.data["text"], "Let me reason about this...")

    def test_falls_back_to_text_field(self):
        raw = {"event_type": "ThinkPart", "text": "Reasoning via text key."}
        event = normalize_kimi_event(raw, "T1", "d-cc-04")
        self.assertEqual(event.event_type, "thinking")
        self.assertEqual(event.data["text"], "Reasoning via text key.")

    def test_handles_missing_content(self):
        raw = {"event_type": "ThinkPart"}
        event = normalize_kimi_event(raw, "T1", "d-cc-04")
        self.assertEqual(event.event_type, "thinking")
        self.assertEqual(event.data["text"], "")


class TestNormalizeCamelCaseTextPart(unittest.TestCase):
    def test_maps_to_text_with_text_field(self):
        raw = {"event_type": "TextPart", "text": "The answer is 42."}
        event = normalize_kimi_event(raw, "T1", "d-cc-05")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "The answer is 42.")

    def test_falls_back_to_content_field(self):
        raw = {"event_type": "TextPart", "content": "Content fallback."}
        event = normalize_kimi_event(raw, "T1", "d-cc-05")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Content fallback.")

    def test_handles_missing_text(self):
        raw = {"event_type": "TextPart"}
        event = normalize_kimi_event(raw, "T1", "d-cc-05")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")


class TestNormalizeCamelCaseStatusUpdate(unittest.TestCase):
    def test_maps_to_text_with_token_count_from_token_count_key(self):
        raw = {
            "event_type": "StatusUpdate",
            "token_count": {
                "input_tokens": 350,
                "output_tokens": 120,
            },
        }
        event = normalize_kimi_event(raw, "T1", "d-cc-06")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")
        tc = event.data["token_count"]
        self.assertEqual(tc["input_tokens"], 350)
        self.assertEqual(tc["output_tokens"], 120)
        self.assertEqual(tc["cache_creation_tokens"], 0)
        self.assertEqual(tc["cache_read_tokens"], 0)

    def test_maps_with_legacy_usage_key_and_field_names(self):
        raw = {
            "event_type": "StatusUpdate",
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 80,
            },
        }
        event = normalize_kimi_event(raw, "T1", "d-cc-06")
        tc = event.data["token_count"]
        self.assertEqual(tc["input_tokens"], 200)
        self.assertEqual(tc["output_tokens"], 80)

    def test_handles_missing_token_count(self):
        raw = {"event_type": "StatusUpdate"}
        event = normalize_kimi_event(raw, "T1", "d-cc-06")
        self.assertEqual(event.event_type, "text")
        tc = event.data["token_count"]
        self.assertEqual(tc["input_tokens"], 0)
        self.assertEqual(tc["output_tokens"], 0)


class TestNormalizeCamelCaseTurnEnd(unittest.TestCase):
    def test_maps_to_complete(self):
        raw = {"event_type": "TurnEnd"}
        event = normalize_kimi_event(raw, "T1", "d-cc-07")
        self.assertEqual(event.event_type, "complete")
        self.assertEqual(event.data, {})


# ---------------------------------------------------------------------------
# Unit tests: _from_kimi_event in canonical_event.py — same camelCase logic
# ---------------------------------------------------------------------------


class TestCanonicalEventFromKimiCamelCase(unittest.TestCase):
    """Verify _from_kimi_event (canonical_event.py) handles camelCase identically."""

    def test_turn_begin(self):
        event = _from_kimi_event({"event_type": "TurnBegin"}, "d1", "T1")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")

    def test_step_begin(self):
        event = _from_kimi_event({"event_type": "StepBegin"}, "d1", "T1")
        self.assertEqual(event.event_type, "text")

    def test_content_part(self):
        event = _from_kimi_event({"event_type": "ContentPart", "content": "Hi"}, "d1", "T1")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Hi")

    def test_think_part(self):
        event = _from_kimi_event({"event_type": "ThinkPart", "content": "hmm"}, "d1", "T1")
        self.assertEqual(event.event_type, "thinking")
        self.assertEqual(event.data["text"], "hmm")

    def test_text_part(self):
        event = _from_kimi_event({"event_type": "TextPart", "text": "answer"}, "d1", "T1")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "answer")

    def test_status_update(self):
        raw = {"event_type": "StatusUpdate", "token_count": {"input_tokens": 10, "output_tokens": 5}}
        event = _from_kimi_event(raw, "d1", "T1")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["token_count"]["input_tokens"], 10)

    def test_turn_end(self):
        event = _from_kimi_event({"event_type": "TurnEnd"}, "d1", "T1")
        self.assertEqual(event.event_type, "complete")


# ---------------------------------------------------------------------------
# Legacy backward-compat: existing event types still work
# ---------------------------------------------------------------------------


class TestLegacyEventsStillWork(unittest.TestCase):
    """Regression: legacy lowercase events must not break."""

    def test_assistant_text(self):
        raw = {"event_type": "assistant_text", "content": "Hello!"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Hello!")

    def test_text_alias(self):
        raw = {"event_type": "text", "content": "World"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "text")

    def test_tool_call(self):
        raw = {"event_type": "tool_call", "name": "ls", "input": {}, "id": "tc-1"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "tool_use")

    def test_tool_result(self):
        raw = {"event_type": "tool_result", "tool_call_id": "tc-1", "output": "ok"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "tool_result")

    def test_usage_complete(self):
        raw = {"event_type": "usage_complete", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["token_count"]["input_tokens"], 10)

    def test_complete(self):
        raw = {"event_type": "complete"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "complete")

    def test_error(self):
        raw = {"event_type": "error", "message": "fail"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "error")

    def test_unknown_still_maps_to_error(self):
        raw = {"event_type": "never_seen_this"}
        event = normalize_kimi_event(raw, "T1", "d-legacy")
        self.assertEqual(event.event_type, "error")
        self.assertIn("never_seen_this", event.data.get("reason", ""))


# ---------------------------------------------------------------------------
# Integration: full v1.26+ stream through spawn_kimi
# ---------------------------------------------------------------------------


class TestCamelCaseStreamIntegration(unittest.TestCase):
    """Integration test: a full v1.26+ camelCase event stream through spawn_kimi."""

    def _run_with_events(self, events: list, returncode: int = 0) -> KimiSpawnResult:
        data = b"".join((json.dumps(e) + "\n").encode() for e in events)
        read_fd, write_fd = os.pipe()

        def _writer():
            try:
                os.write(write_fd, data)
            finally:
                os.close(write_fd)

        writer_thread = threading.Thread(target=_writer, daemon=True)
        writer_thread.start()

        fake_proc = MagicMock()
        fake_proc.returncode = returncode
        fake_proc.poll.return_value = returncode
        fake_proc.stdout = os.fdopen(read_fd, "rb", buffering=0)
        fake_proc.stderr = io.BytesIO(b"")
        fake_proc.wait = MagicMock(return_value=returncode)
        fake_proc.kill = MagicMock()

        try:
            with patch("provider_spawns.kimi_spawn._start_kimi_subprocess") as mock_start:
                mock_start.return_value = (fake_proc, None)
                result = spawn_kimi("prompt", dispatch_id="d-int", terminal_id="T1")
        finally:
            writer_thread.join(timeout=5)
        return result

    def test_full_camelcase_stream_produces_completion_text(self):
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "StepBegin"},
            {"event_type": "ThinkPart", "content": "Let me think about this..."},
            {"event_type": "TextPart", "text": "The answer is "},
            {"event_type": "TextPart", "text": "42."},
            {"event_type": "StatusUpdate", "token_count": {"input_tokens": 100, "output_tokens": 25}},
            {"event_type": "TurnEnd"},
        ]
        result = self._run_with_events(events)
        self.assertIn("The answer is ", result.completion_text)
        self.assertIn("42.", result.completion_text)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.error)

    def test_camelcase_stream_captures_token_usage(self):
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "TextPart", "text": "Done."},
            {"event_type": "StatusUpdate", "token_count": {"input_tokens": 500, "output_tokens": 150}},
            {"event_type": "TurnEnd"},
        ]
        result = self._run_with_events(events)
        self.assertIsNotNone(result.token_usage)
        self.assertEqual(result.token_usage["input_tokens"], 500)
        self.assertEqual(result.token_usage["output_tokens"], 150)

    def test_content_part_contributes_to_completion_text(self):
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "ContentPart", "content": "Here is the code review."},
            {"event_type": "TurnEnd"},
        ]
        result = self._run_with_events(events)
        self.assertIn("Here is the code review.", result.completion_text)

    def test_mixed_legacy_and_camelcase_stream(self):
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "assistant_text", "content": "Legacy part. "},
            {"event_type": "TextPart", "text": "New part."},
            {"event_type": "complete"},
        ]
        result = self._run_with_events(events)
        self.assertIn("Legacy part.", result.completion_text)
        self.assertIn("New part.", result.completion_text)

    def test_camelcase_stream_events_counted(self):
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "TextPart", "text": "x"},
            {"event_type": "TurnEnd"},
        ]
        result = self._run_with_events(events)
        self.assertEqual(result.events_written, 3)

    def test_think_part_not_in_completion_text(self):
        """ThinkPart maps to 'thinking' type, which is NOT aggregated into completion_text."""
        events = [
            {"event_type": "TurnBegin"},
            {"event_type": "ThinkPart", "content": "Internal reasoning."},
            {"event_type": "TextPart", "text": "Visible answer."},
            {"event_type": "TurnEnd"},
        ]
        result = self._run_with_events(events)
        self.assertNotIn("Internal reasoning.", result.completion_text)
        self.assertIn("Visible answer.", result.completion_text)


if __name__ == "__main__":
    unittest.main()
