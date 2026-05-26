"""Tests for kimi_spawn.py — Kimi CLI subprocess spawn handler (Wave 7.7)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make sure scripts/lib is on the path
_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns.kimi_spawn import (  # noqa: E402
    KimiSpawnResult,
    _build_kimi_cmd,
    normalize_kimi_event,
    spawn_kimi,
)


def _make_stdout(*events: dict) -> io.BytesIO:
    """Build a bytes stream of NDJSON events."""
    lines = "".join(json.dumps(e) + "\n" for e in events)
    return io.BytesIO(lines.encode())


def _mock_proc(stdout_events: list, returncode: int = 0) -> MagicMock:
    """Return a mock subprocess.Popen with the given events in stdout."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.poll.return_value = returncode
    data = b"".join((json.dumps(e) + "\n").encode() for e in stdout_events)
    proc.stdout = io.BytesIO(data)
    proc.stderr = io.BytesIO(b"")
    proc.wait = MagicMock(return_value=returncode)
    return proc


class TestBuildKimiCmd(unittest.TestCase):
    def test_constructs_correct_argv_no_model(self):
        cmd = _build_kimi_cmd("hello world", None, None)
        self.assertEqual(cmd[:6], ["kimi", "--print", "--output-format", "stream-json", "--yolo", "-p"])
        self.assertEqual(cmd[6], "hello world")

    def test_passes_model_when_specified(self):
        cmd = _build_kimi_cmd("prompt", "kimi-k2-6", None)
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "kimi-k2-6")

    def test_skips_model_when_none(self):
        cmd = _build_kimi_cmd("prompt", None, None)
        self.assertNotIn("-m", cmd)

    def test_passes_work_dir_when_specified(self):
        cmd = _build_kimi_cmd("prompt", None, Path("/tmp/work"))
        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "/tmp/work")

    def test_skips_work_dir_when_none(self):
        cmd = _build_kimi_cmd("prompt", None, None)
        self.assertNotIn("-w", cmd)


class TestNormalizeKimiEvent(unittest.TestCase):
    def _make_raw(self, **kwargs) -> dict:
        return kwargs

    def test_normalize_assistant_text_event(self):
        raw = {"event_type": "assistant_text", "content": "Hello!"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Hello!")
        self.assertEqual(event.provider, "kimi")

    def test_normalize_text_event_alias(self):
        raw = {"event_type": "text", "content": "World"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "World")

    def test_normalize_tool_call_event(self):
        raw = {
            "event_type": "tool_call",
            "name": "read_file",
            "input": {"path": "/tmp/x.txt"},
            "id": "tc-123",
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "tool_use")
        self.assertEqual(event.data["name"], "read_file")
        self.assertEqual(event.data["input"], {"path": "/tmp/x.txt"})
        self.assertEqual(event.data["id"], "tc-123")

    def test_normalize_tool_result_event(self):
        raw = {
            "event_type": "tool_result",
            "tool_call_id": "tc-123",
            "output": "file contents here",
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "tool_result")
        self.assertEqual(event.data["tool_use_id"], "tc-123")
        self.assertEqual(event.data["content"], "file contents here")

    def test_normalize_usage_complete_extracted_as_text_with_token_count(self):
        raw = {
            "event_type": "usage_complete",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "")
        tc = event.data.get("token_count", {})
        self.assertEqual(tc["input_tokens"], 100)
        self.assertEqual(tc["output_tokens"], 50)
        self.assertEqual(tc["cache_creation_tokens"], 0)
        self.assertEqual(tc["cache_read_tokens"], 0)

    def test_normalize_complete_event(self):
        raw = {"event_type": "complete"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "complete")

    def test_normalize_error_event(self):
        raw = {"event_type": "error", "message": "something went wrong"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "error")
        self.assertEqual(event.data["message"], "something went wrong")

    def test_normalize_unknown_event_type_maps_to_info(self):
        """Unrecognized event types must map to 'info', not 'error'.

        Mapping to 'error' caused a chain reaction: errors_captured gains an
        entry → _finalize_kimi_result sets rc=1 even when kimi exits 0 →
        _dispatch_kimi emits status='failure' for a perfectly valid completion.
        Using 'info' breaks that chain: 'info' events are silently skipped by
        the consumer without affecting errors_captured, completion_text, or
        token_usage.
        """
        raw = {"event_type": "weird_event", "data": "xyz"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "info")
        self.assertIn("raw_type", event.data)
        self.assertEqual(event.data["raw_type"], "weird_event")

    def test_event_has_correct_dispatch_and_terminal(self):
        raw = {"event_type": "complete"}
        event = normalize_kimi_event(raw, "T2", "my-dispatch")
        self.assertEqual(event.terminal_id, "T2")
        self.assertEqual(event.dispatch_id, "my-dispatch")
        self.assertEqual(event.observability_tier, 1)


class TestSpawnKimiSubprocess(unittest.TestCase):
    def test_returns_127_when_cli_missing(self):
        with patch("subprocess.Popen", side_effect=FileNotFoundError("kimi not found")):
            result = spawn_kimi("test prompt", dispatch_id="d1", terminal_id="T1")
        self.assertEqual(result.returncode, 127)
        self.assertIsNotNone(result.error)
        self.assertIn("not found", result.error.lower())
        self.assertEqual(result.events_written, 0)

    def test_spawn_kimi_constructs_correct_argv(self):
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            raise FileNotFoundError("not testing real spawn")

        with patch("subprocess.Popen", side_effect=fake_popen):
            spawn_kimi("my prompt", dispatch_id="d1", terminal_id="T1")

        self.assertIn("kimi", captured_cmd)
        self.assertIn("--print", captured_cmd)
        self.assertIn("--output-format", captured_cmd)
        self.assertIn("stream-json", captured_cmd)
        self.assertIn("--yolo", captured_cmd)
        self.assertIn("-p", captured_cmd)
        self.assertIn("my prompt", captured_cmd)

    def test_spawn_kimi_passes_model_flag_when_specified(self):
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            raise FileNotFoundError("not testing real spawn")

        with patch("subprocess.Popen", side_effect=fake_popen):
            spawn_kimi("prompt", model="kimi-k2-6", dispatch_id="d1", terminal_id="T1")

        self.assertIn("-m", captured_cmd)
        self.assertEqual(captured_cmd[captured_cmd.index("-m") + 1], "kimi-k2-6")

    def test_spawn_kimi_skips_model_flag_when_none(self):
        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            raise FileNotFoundError("not testing real spawn")

        with patch("subprocess.Popen", side_effect=fake_popen):
            spawn_kimi("prompt", model=None, dispatch_id="d1", terminal_id="T1")

        self.assertNotIn("-m", captured_cmd)


class TestSpawnKimiIntegration(unittest.TestCase):
    """Integration-style tests using a real pipe so drain_stream.fileno() works."""

    def _run_with_events(self, events: list, returncode: int = 0) -> KimiSpawnResult:
        """Run spawn_kimi with a real-pipe-backed fake process emitting the given events."""
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
                result = spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
        finally:
            writer_thread.join(timeout=5)
        return result

    def test_response_text_concatenation(self):
        events = [
            {"event_type": "assistant_text", "content": "Hello "},
            {"event_type": "assistant_text", "content": "world!"},
            {"event_type": "complete"},
        ]
        result = self._run_with_events(events)
        self.assertIn("Hello", result.completion_text)
        self.assertIn("world!", result.completion_text)

    def test_captures_usage_field(self):
        events = [
            {"event_type": "assistant_text", "content": "Done."},
            {"event_type": "usage_complete", "usage": {"prompt_tokens": 200, "completion_tokens": 75}},
            {"event_type": "complete"},
        ]
        result = self._run_with_events(events)
        self.assertIsNotNone(result.token_usage)
        self.assertEqual(result.token_usage["input_tokens"], 200)
        self.assertEqual(result.token_usage["output_tokens"], 75)

    def test_error_event_propagated_to_result_error_field(self):
        """Error events emitted by kimi CLI must surface in result.error."""
        events = [
            {"event_type": "assistant_text", "content": "Partial"},
            {"event_type": "error", "message": "upstream model returned 503"},
            {"event_type": "complete"},
        ]
        result = self._run_with_events(events)
        self.assertIsNotNone(result.error)
        self.assertIn("503", result.error)

    def test_error_event_overrides_zero_exit_code(self):
        """An error event with exit_code=0 must set result.error and returncode != 0."""
        events = [
            {"event_type": "error", "message": "auth token expired"},
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertIsNotNone(result.error)
        self.assertIn("auth token expired", result.error)
        self.assertNotEqual(result.returncode, 0)

    def test_unknown_event_type_in_success_stream_does_not_cause_failure(self):
        """Regression: unrecognized kimi event types must NOT flip status to failure.

        Bug (PR #642 fallout): unknown event_type → mapped to 'error' canonical
        event → added to errors_captured → _finalize_kimi_result set rc=1 even
        when kimi exited 0 → _dispatch_kimi emitted status='failure' for a valid
        completion.  After fix: unknown events map to 'info' and are silently
        skipped.
        """
        events = [
            {"event_type": "assistant_text", "content": "Here is the result."},
            # An event type that kimi CLI may emit but the normalizer doesn't
            # explicitly handle — must be ignored, not treated as an error.
            {"event_type": "InternalDiagnostic", "detail": "tool_trace_dump"},
            {"event_type": "usage_complete", "usage": {"prompt_tokens": 150, "completion_tokens": 60}},
            {"event_type": "complete"},
        ]
        result = self._run_with_events(events, returncode=0)

        # Status must reflect the real outcome: success.
        self.assertIsNone(result.error, f"expected no error but got: {result.error!r}")
        self.assertEqual(result.returncode, 0)

        # Output must be captured.
        self.assertIn("Here is the result.", result.completion_text)

        # Token usage must be extracted.
        self.assertIsNotNone(result.token_usage)
        self.assertEqual(result.token_usage["input_tokens"], 150)
        self.assertEqual(result.token_usage["output_tokens"], 60)

    def test_real_kimi_error_event_still_causes_failure(self):
        """Real kimi error events (with 'error' event_type) must still set failure.

        The fix must not swallow genuine errors — only unrecognized informational
        event types get the non-fatal 'info' treatment.
        """
        events = [
            {"event_type": "assistant_text", "content": "Partial"},
            {"event_type": "error", "message": "rate limit exceeded"},
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertIsNotNone(result.error)
        self.assertIn("rate limit exceeded", result.error)
        self.assertNotEqual(result.returncode, 0)


class TestDispatchKimiEventStoreFailure(unittest.TestCase):
    """Tests for _dispatch_kimi EventStore audit-invariant enforcement (ADR-005)."""

    def test_event_store_init_failure_returns_nonzero(self):
        """_dispatch_kimi must return non-zero when EventStore fails to initialize."""
        import argparse
        _PARENT_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
        if _PARENT_LIB not in sys.path:
            sys.path.insert(0, _PARENT_LIB)
        import provider_dispatch as pd

        args = argparse.Namespace(
            instruction="test prompt",
            dispatch_id="d-es-fail",
            terminal_id="T1",
            pr_id=None,
        )
        with patch("event_store.EventStore", side_effect=RuntimeError("db locked")):
            rc = pd._dispatch_kimi(args)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
