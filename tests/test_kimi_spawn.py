"""Tests for kimi_spawn.py — Kimi CLI subprocess spawn handler (Wave 7.7)."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
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
    _finalize_kimi_result,
    _is_quota_or_auth_error,
    _worktree_has_changes,
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
        self.assertEqual(
            cmd[:6], ["kimi", "--print", "--output-format", "stream-json", "--yolo", "-p"],
        )
        self.assertEqual(cmd[6], "hello world")

    def test_yolo_always_added(self):
        """--yolo is unconditional: without it kimi auto-dismisses tool calls
        instead of executing them (the fabrication bug this dispatch fixes)."""
        cmd = _build_kimi_cmd("prompt", None, None)
        self.assertIn("--yolo", cmd)

    def test_yolo_added_regardless_of_stray_env_var(self):
        """--yolo is hardcoded, not env-gated — a stray VNX_KIMI_YOLO must not
        be able to turn it off (there is no override mechanism by design)."""
        os.environ["VNX_KIMI_YOLO"] = "0"
        try:
            cmd = _build_kimi_cmd("prompt", None, None)
            self.assertIn("--yolo", cmd)
        finally:
            del os.environ["VNX_KIMI_YOLO"]

    def test_yolo_and_work_dir_together_for_governed_build_dispatch(self):
        """The governed build lane passes both flags together: --yolo makes
        kimi execute tools, -w scopes the blast radius to the dispatch worktree."""
        cmd = _build_kimi_cmd("prompt", None, Path("/tmp/worktree"))
        self.assertIn("--yolo", cmd)
        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "/tmp/worktree")

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

    # --- kimi-cli 1.44.0 content-block format (wire protocol 1.10) ---------
    # These fixtures are verbatim raw lines captured from
    # `kimi --print --output-format stream-json -p ...` on kimi-cli 1.44.0.

    def test_normalize_144_assistant_content_block_extracts_text(self):
        """1.44.0 assistant message: text comes from content[] type==text blocks."""
        raw = {
            "role": "assistant",
            "content": [
                {"type": "think", "think": "reasoning here", "encrypted": None},
                {"type": "text", "text": "Hello! 👋\n\n1\n2\n3\n\nDone."},
            ],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "Hello! 👋\n\n1\n2\n3\n\nDone.")

    def test_normalize_144_think_block_captured_as_reasoning(self):
        """think blocks are captured as non-fatal reasoning, never as answer text."""
        raw = {
            "role": "assistant",
            "content": [
                {"type": "think", "think": "internal chain of thought"},
                {"type": "text", "text": "final answer"},
            ],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.data["text"], "final answer")
        self.assertEqual(event.data["reasoning"], "internal chain of thought")

    def test_normalize_144_multiple_text_blocks_are_concatenated(self):
        raw = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "part one "},
                {"type": "text", "text": "part two"},
            ],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.data["text"], "part one part two")

    def test_normalize_144_tool_calls_carried_for_observability(self):
        """An intermediate assistant turn carries tool_calls but no text block."""
        raw = {
            "role": "assistant",
            "content": [{"type": "think", "think": "I should run a shell tool."}],
            "tool_calls": [{
                "type": "function",
                "id": "tool_abc",
                "function": {"name": "Shell", "arguments": "{\"command\": \"echo hi\"}"},
            }],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        # No text block -> empty completion text on this intermediate turn.
        self.assertEqual(event.data["text"], "")
        # tool_calls ride along for observability.
        self.assertEqual(event.data["tool_calls"][0]["id"], "tool_abc")

    def test_normalize_144_tool_role_maps_to_tool_result(self):
        raw = {
            "role": "tool",
            "content": [
                {"type": "text", "text": "<system>Command executed successfully.</system>"},
                {"type": "text", "text": "step-one\nstep-two\n"},
            ],
            "tool_call_id": "tool_abc",
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "tool_result")
        self.assertEqual(event.data["tool_use_id"], "tool_abc")
        self.assertIn("step-one", event.data["content"])

    def test_normalize_144_does_not_shadow_legacy_string_content(self):
        """Legacy events carry content as a string and must still parse via event_type."""
        raw = {"event_type": "assistant_text", "content": "legacy string"}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "legacy string")

    def test_event_has_correct_dispatch_and_terminal(self):
        raw = {"event_type": "complete"}
        event = normalize_kimi_event(raw, "T2", "my-dispatch")
        self.assertEqual(event.terminal_id, "T2")
        self.assertEqual(event.dispatch_id, "my-dispatch")
        self.assertEqual(event.observability_tier, 1)


# ---------------------------------------------------------------------------
# MAJOR-2 regression suite — kimi-deepfix-major2
#
# #763 fixed the immediate fail-loud gap (unknown event_type -> "info" ->
# silently-empty completion_text reported as success). These tests cover the
# deeper content-block edge cases #763 did not: a role != "assistant"/"tool"
# content-block message (e.g. an echoed "user" turn) must never be extracted
# as answer text, and malformed content[] entries must degrade gracefully
# instead of crashing or corrupting completion_text.
# ---------------------------------------------------------------------------

class TestKimiDeepfixMajor2ContentBlockRoleGate(unittest.TestCase):
    """Unit-level: normalize_kimi_event must gate text extraction on role=='assistant'.

    Bug: the pre-fix content-block branch treated ANY string role that wasn't
    "tool" as an assistant turn ("assistant (and any other agent role)"), so a
    kimi-cli stream that echoes the user's own prompt back as a
    {"role": "user", "content": [...]} line — common in transcript/session-
    resume output — would have that echoed prompt silently concatenated into
    completion_text alongside (or instead of) the real answer.
    """

    def test_user_role_content_block_is_not_extracted_as_text(self):
        raw = {
            "role": "user",
            "content": [{"type": "text", "text": "please review this PR"}],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "info")
        self.assertNotIn("text", event.data)

    def test_system_role_content_block_is_not_extracted_as_text(self):
        raw = {
            "role": "system",
            "content": [{"type": "text", "text": "you are a helpful assistant"}],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "info")
        self.assertNotIn("text", event.data)

    def test_unrecognized_role_content_block_maps_to_info_not_text(self):
        raw = {"role": "developer", "content": [{"type": "text", "text": "hint"}]}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "info")

    def test_assistant_role_still_extracts_text(self):
        """Control: the fix must not regress the actual assistant path."""
        raw = {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "the answer")

    def test_tool_role_still_maps_to_tool_result(self):
        """Control: the fix must not regress the tool-role path."""
        raw = {
            "role": "tool",
            "content": [{"type": "text", "text": "tool output"}],
            "tool_call_id": "tc-1",
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "tool_result")
        self.assertEqual(event.data["content"], "tool output")

    # --- defensive reads on malformed content-block shapes -----------------

    def test_content_none_on_assistant_message_does_not_crash_or_fabricate_text(self):
        raw = {"role": "assistant", "content": None}
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        # Falls through to the unknown-event-type fallback: non-fatal, no
        # crash, and critically no fabricated text.
        self.assertEqual(event.event_type, "info")

    def test_content_list_with_non_dict_entries_ignored_not_crashed(self):
        raw = {
            "role": "assistant",
            "content": ["a bare string block", 42, None, {"type": "text", "text": "real answer"}],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.event_type, "text")
        self.assertEqual(event.data["text"], "real answer")

    def test_content_block_missing_type_key_ignored(self):
        raw = {
            "role": "assistant",
            "content": [{"text": "no type field"}, {"type": "text", "text": "kept"}],
        }
        event = normalize_kimi_event(raw, "T1", "dispatch-01")
        self.assertEqual(event.data["text"], "kept")


class TestKimiDeepfixMajor2EndToEnd(unittest.TestCase):
    """Integration-level: an echoed user turn in the stream must never leak into
    completion_text, even when it arrives alongside a real assistant answer.
    """

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
                result = spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
        finally:
            writer_thread.join(timeout=5)
        return result

    def test_echoed_user_turn_does_not_leak_into_completion_text(self):
        events = [
            {"role": "user", "content": [{"type": "text", "text": "please review this PR"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "reviewing now"},
                    {"type": "text", "text": "VERDICT: approve"},
                ],
            },
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.completion_text, "VERDICT: approve")
        self.assertNotIn("please review this PR", result.completion_text)


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


def _spawn_kimi_with_events(
    events: list, returncode: int = 0, cwd=None, event_writer=None,
) -> KimiSpawnResult:
    """Run spawn_kimi with a real-pipe-backed fake process emitting the given events.

    Shared by integration-style test classes below — real pipe so
    drain_stream.fileno() works, ``_start_kimi_subprocess`` mocked out so no
    real ``kimi`` binary is required.
    """
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
            result = spawn_kimi(
                "prompt", dispatch_id="d1", terminal_id="T1", cwd=cwd,
                event_writer=event_writer,
            )
    finally:
        writer_thread.join(timeout=5)
    return result


class TestSpawnKimiIntegration(unittest.TestCase):
    """Integration-style tests using a real pipe so drain_stream.fileno() works."""

    def _run_with_events(self, events: list, returncode: int = 0) -> KimiSpawnResult:
        """Run spawn_kimi with a real-pipe-backed fake process emitting the given events."""
        return _spawn_kimi_with_events(events, returncode=returncode)

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

    # --- kimi-cli 1.44.0 content-block end-to-end ---------------------------

    def test_144_content_block_stream_extracts_final_answer(self):
        """Full 1.44.0 tool-using stream: only the final assistant text is kept.

        Verbatim shape from a real `kimi --print --output-format stream-json` run:
        assistant(think+tool_calls) -> tool(result) -> assistant(think+text).
        """
        events = [
            {
                "role": "assistant",
                "content": [{"type": "think", "think": "I should run a shell tool."}],
                "tool_calls": [{
                    "type": "function", "id": "tool_1",
                    "function": {"name": "Shell", "arguments": "{\"command\": \"echo hi\"}"},
                }],
            },
            {
                "role": "tool",
                "content": [{"type": "text", "text": "hi\n"}],
                "tool_call_id": "tool_1",
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "The command ran."},
                    {"type": "text", "text": "The command printed: hi"},
                ],
            },
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertIsNone(result.error, f"unexpected error: {result.error!r}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.completion_text, "The command printed: hi")

    def test_144_empty_extraction_on_nonempty_response_is_failure(self):
        """FAIL-LOUD: CLI emits message lines but no text block -> FAILURE, not empty-success.

        This is the core 1.44.0 governance defect: an output-format change made
        text extraction yield zero characters while the process exited 0. The old
        behavior was a silent empty report reported as success. The fix marks it
        a failure with the raw output captured.
        """
        events = [
            {
                "role": "assistant",
                "content": [{"type": "think", "think": "only reasoning, no answer block"}],
                "tool_calls": [{
                    "type": "function", "id": "tool_x",
                    "function": {"name": "Shell", "arguments": "{}"},
                }],
            },
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertEqual(result.completion_text, "")
        self.assertIsNotNone(result.error, "empty extraction must surface as an error")
        self.assertNotEqual(result.returncode, 0, "empty extraction must not exit 0")
        self.assertIn("ZERO", result.error)
        # The raw output sample must be captured for diagnosis.
        self.assertIn("raw_event_sample", result.error)

    def test_144_unknown_format_text_under_wrong_key_fails_loud(self):
        """If the CLI moves answer text to an unrecognized block type, fail loud."""
        events = [
            {"role": "assistant", "content": [{"type": "answer", "answer": "hidden"}]},
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertEqual(result.completion_text, "")
        self.assertIsNotNone(result.error)
        self.assertNotEqual(result.returncode, 0)

    def test_144_token_usage_unavailable_marked_explicitly_not_silent_zero(self):
        """1.44.0 reports no usage: token_usage is None and property flags it."""
        events = [
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        ]
        result = self._run_with_events(events, returncode=0)
        self.assertIsNone(result.token_usage, "no usage line -> token_usage must be None")
        # token_usage_measured is a property, not part of the cross-provider
        # frontmatter_fields() contract — check it on the result object directly.
        self.assertFalse(result.token_usage_measured)
        fm = result.frontmatter_fields()
        # The numeric fields remain schema-valid zeros, but measured=False marks
        # them as unavailable rather than a measured zero.
        self.assertEqual(fm["token_usage"], {"input": 0, "output": 0, "cache_read": 0})

    def test_token_usage_measured_true_when_usage_present(self):
        """When a usage event IS present, the property marks tokens as measured."""
        result = KimiSpawnResult(
            returncode=0, completion_text="x", events_written=1, session_id=None,
            timed_out=False, token_usage={"input_tokens": 10, "output_tokens": 5},
        )
        # token_usage_measured is a property, not part of frontmatter_fields().
        self.assertTrue(result.token_usage_measured)
        fm = result.frontmatter_fields()
        self.assertEqual(fm["token_usage"]["input"], 10)
        self.assertEqual(fm["token_usage"]["output"], 5)


class TestIsQuotaOrAuthError(unittest.TestCase):
    """Unit tests for _is_quota_or_auth_error()."""

    def test_detects_403_literal(self):
        self.assertTrue(_is_quota_or_auth_error("HTTP 403 Forbidden"))

    def test_detects_quota(self):
        self.assertTrue(_is_quota_or_auth_error("quota exceeded for this account"))

    def test_detects_rate_limit_with_space(self):
        self.assertTrue(_is_quota_or_auth_error("rate limit exceeded"))

    def test_detects_ratelimit_no_space(self):
        self.assertTrue(_is_quota_or_auth_error("ratelimit hit"))

    def test_detects_unauthorized(self):
        self.assertTrue(_is_quota_or_auth_error("unauthorized request"))

    def test_detects_forbidden(self):
        self.assertTrue(_is_quota_or_auth_error("access forbidden"))

    def test_detects_token_expired(self):
        self.assertTrue(_is_quota_or_auth_error("token expired, please re-login"))

    def test_returns_false_for_normal_text(self):
        self.assertFalse(_is_quota_or_auth_error("model returned a helpful response"))

    def test_returns_false_for_empty_string(self):
        self.assertFalse(_is_quota_or_auth_error(""))

    def test_returns_false_for_none(self):
        self.assertFalse(_is_quota_or_auth_error(None))

    def test_case_insensitive(self):
        self.assertTrue(_is_quota_or_auth_error("QUOTA EXCEEDED"))
        self.assertTrue(_is_quota_or_auth_error("Rate Limit Hit"))


class TestNonJsonAnd403Handling(unittest.TestCase):
    """Integration tests: non-JSON / 403-style output yields structured errors, no crash."""

    def _run_with_raw_bytes(self, raw_bytes: bytes, returncode: int = 1) -> "KimiSpawnResult":
        read_fd, write_fd = os.pipe()

        def _writer():
            try:
                os.write(write_fd, raw_bytes)
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
                result = spawn_kimi("prompt", dispatch_id="d-nonjson", terminal_id="T1")
        finally:
            writer_thread.join(timeout=5)
        return result

    def test_non_json_403_line_yields_structured_error_no_exception(self):
        """A bare non-JSON 403 line must produce a structured error, not a traceback."""
        raw = b"Error: HTTP 403 Forbidden - quota exceeded\n"
        result = self._run_with_raw_bytes(raw)
        # Must not crash — must return a KimiSpawnResult
        self.assertIsInstance(result, KimiSpawnResult)
        self.assertIsNotNone(result.error)
        self.assertNotEqual(result.returncode, 0)
        # Must surface the quota_or_auth classification
        self.assertIn("quota_or_auth", result.error)

    def test_non_json_output_includes_raw_line_for_diagnosis(self):
        """The raw first line must be preserved in the error for diagnosis."""
        raw_line = b"Error: HTTP 403 Forbidden - quota exceeded\n"
        result = self._run_with_raw_bytes(raw_line)
        # The raw line (truncated) must appear in the error for diagnosis
        self.assertIn("403", result.error)

    def test_json_http_403_response_yields_quota_or_auth(self):
        """A JSON response with status=403 must be classified as quota_or_auth."""
        raw = b'{"status": 403, "message": "quota exceeded for account"}\n'
        result = self._run_with_raw_bytes(raw)
        self.assertIsInstance(result, KimiSpawnResult)
        self.assertIsNotNone(result.error)
        self.assertIn("quota_or_auth", result.error)
        self.assertNotEqual(result.returncode, 0)

    def test_json_401_response_yields_quota_or_auth(self):
        """A JSON response with status=401 must be classified as quota_or_auth."""
        raw = b'{"status": 401, "message": "unauthorized"}\n'
        result = self._run_with_raw_bytes(raw)
        self.assertIsNotNone(result.error)
        self.assertIn("quota_or_auth", result.error)

    def test_normal_error_without_quota_pattern_passes_through_verbatim(self):
        """Non-quota error messages must not be reclassified as quota_or_auth."""
        events_bytes = b'{"event_type": "error", "message": "upstream model returned 503"}\n'
        result = self._run_with_raw_bytes(events_bytes, returncode=0)
        # Should still be an error
        self.assertIsNotNone(result.error)
        # Must NOT be labeled quota_or_auth
        self.assertNotIn("quota_or_auth", result.error)
        # Original message must be preserved
        self.assertIn("503", result.error)


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


class TestKimiEnvIsolation(unittest.TestCase):
    """The kimi CLI must run with a clean Python env — a foreign venv's
    VIRTUAL_ENV/PYTHONPATH/PYTHONHOME shadows kimi's own deps (mcp.types clash →
    0.6s crash, live-proven on a SEOcrawler .venv-spawned plan-gate panel)."""

    def test_isolate_strips_venv_pollution(self) -> None:
        from provider_spawns.kimi_spawn import _isolate_kimi_env

        dirty = {
            "VIRTUAL_ENV": "/x/.venv",
            "PYTHONPATH": "/x/.venv/lib/site-packages",
            "PYTHONHOME": "/x/.venv",
            "PATH": "/usr/bin",
            "HOME": "/Users/me",
        }
        clean = _isolate_kimi_env(dirty)
        self.assertNotIn("VIRTUAL_ENV", clean)
        self.assertNotIn("PYTHONPATH", clean)
        self.assertNotIn("PYTHONHOME", clean)
        # non-pollution vars are preserved untouched
        self.assertEqual(clean["PATH"], "/usr/bin")
        self.assertEqual(clean["HOME"], "/Users/me")

    def test_spawn_kimi_passes_clean_env_to_subprocess(self) -> None:
        """End-to-end wiring: even with a polluted os.environ AND extra_env, the
        env handed to the subprocess carries none of the venv-pollution vars."""
        from provider_spawns import kimi_spawn

        polluted = {
            **os.environ,
            "VIRTUAL_ENV": "/foreign/.venv",
            "PYTHONPATH": "/foreign/site-packages",
            "PYTHONHOME": "/foreign",
        }
        captured: dict = {}

        def _fake_start(cmd, env, cwd_str):
            captured["env"] = env
            return None, KimiSpawnResult(
                returncode=127, completion_text="", events_written=0,
                session_id=None, timed_out=False, stopped_early=False,
                token_usage=None, error="stub",
            )

        with patch.dict(os.environ, polluted, clear=True), \
                patch.object(kimi_spawn, "_start_kimi_subprocess", _fake_start):
            # extra_env also tries to inject pollution — it must still be stripped.
            kimi_spawn.spawn_kimi(
                prompt="review this",
                dispatch_id="d-iso",
                terminal_id="T1",
                extra_env={"PYTHONPATH": "/also/foreign"},
            )

        env = captured["env"]
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)


# ---------------------------------------------------------------------------
# kimi-agentic-worker-lane: --yolo/-w wiring + completion-vs-execution invariant
# ---------------------------------------------------------------------------


class TestKimiEffectiveArgvLogging(unittest.TestCase):
    """The effective argv (yolo/worktree posture) must be logged, never silent,
    and recorded as an event when a sink is wired — the "always logged, never
    hidden" YOLO-mode principle."""

    def test_effective_argv_logged_without_leaking_prompt(self) -> None:
        secret_prompt = "SECRET_INSTRUCTION_DO_NOT_LEAK_THIS_CONTENT"
        with patch(
            "provider_spawns.kimi_spawn._start_kimi_subprocess",
            return_value=(None, KimiSpawnResult(
                returncode=127, completion_text="", events_written=0,
                session_id=None, timed_out=False, stopped_early=False,
                token_usage=None, error="kimi binary not found",
            )),
        ), self.assertLogs("provider_spawns.kimi_spawn", level=logging.INFO) as cap:
            spawn_kimi(
                prompt=secret_prompt, model="k2-test",
                dispatch_id="test-argv-log", terminal_id="T1",
            )

        log_text = "\n".join(cap.output)
        self.assertNotIn(secret_prompt, log_text)
        self.assertIn(str(len(secret_prompt)), log_text)
        self.assertIn("--yolo", log_text)

    def test_effective_argv_emitted_as_event_via_event_writer(self) -> None:
        """When an event_writer sink is wired (the governed build lane's
        production wiring), the effective argv must be recorded through it so
        it lands in the archived event stream the receipt points to."""
        recorded: list = []

        def _fake_writer(terminal_id, event_dict, dispatch_id=None):
            recorded.append(event_dict)

        with patch(
            "provider_spawns.kimi_spawn._start_kimi_subprocess",
            return_value=(None, KimiSpawnResult(
                returncode=127, completion_text="", events_written=0,
                session_id=None, timed_out=False, stopped_early=False,
                token_usage=None, error="kimi binary not found",
            )),
        ):
            spawn_kimi(
                prompt="do the thing", dispatch_id="d1", terminal_id="T1",
                event_writer=_fake_writer, cwd=Path("/tmp/worktree-xyz"),
            )

        argv_events = [e for e in recorded if (e.get("data") or {}).get("kind") == "effective_argv"]
        self.assertEqual(len(argv_events), 1, f"expected exactly one effective_argv event, got {recorded}")
        data = argv_events[0]["data"]
        self.assertTrue(data["yolo"])
        self.assertEqual(data["work_dir"], "/tmp/worktree-xyz")
        self.assertIn("--yolo", data["argv"])
        self.assertIn("-w", data["argv"])


class TestWorktreeHasChanges(unittest.TestCase):
    """`_worktree_has_changes` drives the completion-vs-execution invariant —
    exercised against a real git repo, not a mocked subprocess, per the "tests
    run real code" convention."""

    def test_clean_worktree_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            self.assertIs(_worktree_has_changes(Path(tmp)), False)

    def test_dirty_worktree_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            Path(tmp, "new_file.txt").write_text("hello")
            self.assertIs(_worktree_has_changes(Path(tmp)), True)

    def test_non_git_directory_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_worktree_has_changes(Path(tmp)))


class TestFabricationInvariant(unittest.TestCase):
    """Unit coverage of `_finalize_kimi_result`'s completion-vs-execution guard,
    with `_worktree_has_changes` mocked so behavior is isolated from git."""

    @staticmethod
    def _mock_proc(returncode: int = 0) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.wait = MagicMock(return_value=returncode)
        return proc

    def test_tool_calls_with_no_worktree_diff_fails_loud(self) -> None:
        with patch("provider_spawns.kimi_spawn._worktree_has_changes", return_value=False):
            result = _finalize_kimi_result(
                proc=self._mock_proc(0),
                completion_text="I fixed the bug and wrote the file.",
                events_written=3,
                token_usage=None,
                timed_out=False,
                stopped_early=False,
                event_writer_failures=0,
                errors_captured=[],
                saw_stream_output=True,
                raw_samples=[],
                saw_tool_calls=True,
                worktree=Path("/tmp/fake-worktree"),
            )
        self.assertIsNotNone(result.error)
        self.assertIn("fabrication", result.error.lower())
        self.assertNotEqual(result.returncode, 0)

    def test_tool_calls_with_worktree_diff_passes(self) -> None:
        with patch("provider_spawns.kimi_spawn._worktree_has_changes", return_value=True):
            result = _finalize_kimi_result(
                proc=self._mock_proc(0),
                completion_text="Done, file created.",
                events_written=3,
                token_usage=None,
                timed_out=False,
                stopped_early=False,
                event_writer_failures=0,
                errors_captured=[],
                saw_stream_output=True,
                raw_samples=[],
                saw_tool_calls=True,
                worktree=Path("/tmp/fake-worktree"),
            )
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)

    def test_no_worktree_skips_check_gracefully(self) -> None:
        with patch("provider_spawns.kimi_spawn._worktree_has_changes") as mock_check:
            result = _finalize_kimi_result(
                proc=self._mock_proc(0),
                completion_text="Done, file created.",
                events_written=3,
                token_usage=None,
                timed_out=False,
                stopped_early=False,
                event_writer_failures=0,
                errors_captured=[],
                saw_stream_output=True,
                raw_samples=[],
                saw_tool_calls=True,
                worktree=None,
            )
        mock_check.assert_not_called()
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)

    def test_no_tool_calls_skips_check_even_with_known_worktree(self) -> None:
        """A pure-text response (no tool_calls) never triggers the git check —
        it only guards the specific completion-without-execution pattern."""
        with patch("provider_spawns.kimi_spawn._worktree_has_changes") as mock_check:
            result = _finalize_kimi_result(
                proc=self._mock_proc(0),
                completion_text="Here's my analysis, no changes needed.",
                events_written=2,
                token_usage=None,
                timed_out=False,
                stopped_early=False,
                event_writer_failures=0,
                errors_captured=[],
                saw_stream_output=True,
                raw_samples=[],
                saw_tool_calls=False,
                worktree=Path("/tmp/fake-worktree"),
            )
        mock_check.assert_not_called()
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)


class TestKimiFabricationInvariantEndToEnd(unittest.TestCase):
    """Full wiring through spawn_kimi() -> _consume_kimi_stream() ->
    _finalize_kimi_result(), against a real temp git worktree — proves the
    saw_tool_calls signal and the worktree arg are actually threaded through,
    not just correct in isolation."""

    _TOOL_CALL_EVENTS = [
        {
            "role": "assistant",
            "content": [{"type": "think", "think": "writing a file"}],
            "tool_calls": [{
                "type": "function", "id": "t1",
                "function": {"name": "Write", "arguments": "{\"path\": \"x\"}"},
            }],
        },
        {"role": "tool", "content": [{"type": "text", "text": "ok"}], "tool_call_id": "t1"},
        {"role": "assistant", "content": [{"type": "text", "text": "Done, file created."}]},
    ]

    def test_tool_calls_no_diff_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            result = _spawn_kimi_with_events(self._TOOL_CALL_EVENTS, cwd=tmp)
        self.assertIsNotNone(result.error)
        self.assertIn("fabrication", result.error.lower())
        self.assertNotEqual(result.returncode, 0)

    def test_tool_calls_with_diff_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            Path(tmp, "x").write_text("content")
            result = _spawn_kimi_with_events(self._TOOL_CALL_EVENTS, cwd=tmp)
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)

    def test_tool_calls_no_worktree_known_skips_gracefully(self) -> None:
        result = _spawn_kimi_with_events(self._TOOL_CALL_EVENTS, cwd=None)
        self.assertIsNone(result.error)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
