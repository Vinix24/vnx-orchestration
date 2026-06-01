#!/usr/bin/env python3
"""Tests for tmux_conversation_normalizer.py — ANSI stripping, text extraction, EventStore wiring."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from tmux_conversation_normalizer import (
    is_redraw_frame,
    normalize_conversation,
    strip_ansi_osc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeEventStore:
    """Minimal EventStore stand-in that records append calls."""

    def __init__(self) -> None:
        self.appended: list[tuple] = []  # (terminal, event, dispatch_id)

    def append(self, terminal, event, *, dispatch_id=None):
        self.appended.append((terminal, event, dispatch_id))


def _write_raw_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# strip_ansi_osc
# ---------------------------------------------------------------------------

class TestStripAnsiOsc(unittest.TestCase):
    """strip_ansi_osc() removes escape sequences and control characters."""

    def test_strips_csi_color_sequence(self):
        self.assertEqual(strip_ansi_osc("\x1b[31mred text\x1b[0m"), "red text")

    def test_strips_csi_cursor_movement(self):
        self.assertEqual(strip_ansi_osc("\x1b[2J\x1b[H"), "")

    def test_strips_osc_window_title(self):
        result = strip_ansi_osc("\x1b]0;window title\x07actual text")
        self.assertEqual(result, "actual text")

    def test_strips_osc_with_string_terminator(self):
        result = strip_ansi_osc("\x1b]2;title\x1b\\text after")
        self.assertEqual(result, "text after")

    def test_strips_carriage_return(self):
        result = strip_ansi_osc("line1\r\nline2\r")
        self.assertEqual(result, "line1\nline2")

    def test_strips_nul_byte(self):
        result = strip_ansi_osc("hello\x00world")
        self.assertEqual(result, "helloworld")

    def test_preserves_plain_text(self):
        text = "plain text without any escape sequences"
        self.assertEqual(strip_ansi_osc(text), text)

    def test_preserves_unicode(self):
        text = "Claude: Ik heb het bestand gelezen"
        self.assertEqual(strip_ansi_osc(text), text)

    def test_strips_multiple_escape_sequences(self):
        raw = "\x1b[1;32m## Summary\x1b[0m\n\x1b[36mChanges:\x1b[0m done"
        result = strip_ansi_osc(raw)
        self.assertNotIn("\x1b", result)
        self.assertIn("## Summary", result)
        self.assertIn("Changes:", result)


# ---------------------------------------------------------------------------
# is_redraw_frame
# ---------------------------------------------------------------------------

class TestIsRedrawFrame(unittest.TestCase):
    """is_redraw_frame() detects TUI full-screen redraw lines."""

    def test_line_with_many_cursor_positions_is_redraw(self):
        # Three absolute cursor-position sequences → redraw frame.
        raw = "\x1b[1;1H\x1b[2;40H\x1b[10;1H content"
        self.assertTrue(is_redraw_frame(raw))

    def test_line_with_two_screen_clears_is_redraw(self):
        raw = "\x1b[2J\x1b[J text here"
        self.assertTrue(is_redraw_frame(raw))

    def test_plain_text_is_not_redraw(self):
        self.assertFalse(is_redraw_frame("Normal assistant response text"))

    def test_single_cursor_move_is_not_redraw(self):
        # One cursor position → not enough to be classified as a full redraw.
        raw = "\x1b[1;1H some text on the first line"
        self.assertFalse(is_redraw_frame(raw))

    def test_empty_line_is_not_redraw(self):
        self.assertFalse(is_redraw_frame(""))


# ---------------------------------------------------------------------------
# normalize_conversation — core logic
# ---------------------------------------------------------------------------

class TestNormalizeConversation(unittest.TestCase):
    """normalize_conversation() reads raw log, strips noise, appends CanonicalEvents."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.raw_log = self.tmp_dir / "conversations" / "test-dispatch.log"
        self.event_store = FakeEventStore()

    # -- helpers
    def _run(self, content: str) -> int:
        _write_raw_log(self.raw_log, content)
        return normalize_conversation(
            self.raw_log,
            self.event_store,
            terminal_id="T1",
            dispatch_id="test-dispatch-001",
            model="sonnet",
        )

    # -- absent / empty file
    def test_returns_zero_when_file_missing(self):
        count = normalize_conversation(
            self.tmp_dir / "nonexistent.log",
            self.event_store,
            terminal_id="T1",
            dispatch_id="test-dispatch-001",
            model="sonnet",
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.event_store.appended, [])

    def test_returns_zero_when_file_empty(self):
        _write_raw_log(self.raw_log, "")
        count = normalize_conversation(
            self.raw_log,
            self.event_store,
            terminal_id="T1",
            dispatch_id="test-dispatch-001",
            model="sonnet",
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.event_store.appended, [])

    def test_returns_zero_when_only_whitespace_after_stripping(self):
        # Raw log contains only ANSI sequences + whitespace.
        count = self._run("\x1b[2J\x1b[H\x1b[1;1H\x1b[2;40H\x1b[10;1H\r\r\r\n\n")
        self.assertEqual(count, 0)
        self.assertEqual(self.event_store.appended, [])

    # -- basic extraction
    def test_strips_ansi_and_emits_text_event(self):
        raw = "\x1b[32mHello\x1b[0m, this is the assistant response.\n"
        count = self._run(raw)
        self.assertGreater(count, 0)
        text_events = [e for _, e, _ in self.event_store.appended if e.event_type == "text"]
        self.assertTrue(text_events, "at least one text event must be emitted")
        text_content = text_events[0].data.get("text", "")
        self.assertIn("Hello", text_content)
        self.assertIn("assistant response", text_content)
        self.assertNotIn("\x1b", text_content)

    def test_emits_complete_event(self):
        count = self._run("Some assistant text\n")
        self.assertGreater(count, 0)
        event_types = [e.event_type for _, e, _ in self.event_store.appended]
        self.assertIn("complete", event_types)

    def test_text_and_complete_events_emitted(self):
        count = self._run("Assistant says hi\n")
        self.assertEqual(count, 2, "exactly 2 events: text + complete")

    # -- correct CanonicalEvent fields
    def test_events_have_correct_terminal_id(self):
        self._run("Hello from Claude\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.terminal_id, "T1")

    def test_events_have_correct_dispatch_id(self):
        self._run("Hello from Claude\n")
        for _, event, dispatch_id in self.event_store.appended:
            self.assertEqual(dispatch_id, "test-dispatch-001")
            self.assertEqual(event.dispatch_id, "test-dispatch-001")

    def test_events_have_provider_claude(self):
        self._run("Hello from Claude\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.provider, "claude")

    def test_events_have_correct_lane_in_provider_meta(self):
        self._run("Hello from Claude\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.provider_meta.get("lane"), "tmux_interactive")

    def test_events_have_correct_source_in_provider_meta(self):
        self._run("Hello from Claude\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.provider_meta.get("source"), "tmux_pipe_pane")

    def test_events_reference_raw_log_path(self):
        self._run("Hello from Claude\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.provider_meta.get("raw_log"), str(self.raw_log))

    def test_events_have_model(self):
        self._run("Hello\n")
        for _, event, _ in self.event_store.appended:
            self.assertEqual(event.model, "sonnet")

    # -- deduplication: redraw frames must not produce duplicate events
    def test_duplicate_redraw_frames_do_not_produce_duplicate_text(self):
        """The same text rendered 5× across TUI redraw frames must appear once in the output."""
        # Each "frame" is a TUI screen dump: lots of cursor moves + text.
        # The real text "Task complete" appears in each frame.
        single_frame = (
            "\x1b[1;1H\x1b[2;40H\x1b[5;1H"  # 3 cursor positions → redraw frame filtered
            "Task complete\n"
        )
        # 5 identical frames
        raw = single_frame * 5
        self._run(raw)

        text_events = [e for _, e, _ in self.event_store.appended if e.event_type == "text"]
        if text_events:
            all_text = "\n".join(e.data.get("text", "") for e in text_events)
            occurrences = all_text.count("Task complete")
            self.assertEqual(occurrences, 1, "deduplicated: 'Task complete' must appear exactly once")

    def test_duplicate_lines_deduplicated(self):
        """Identical text lines across different parts of the log appear once in events."""
        # Same line repeated 10 times (e.g. TUI chrome like "? for shortcuts")
        raw = "? for shortcuts\n" * 10 + "Real content from Claude\n"
        self._run(raw)

        text_events = [e for _, e, _ in self.event_store.appended if e.event_type == "text"]
        self.assertTrue(text_events)
        all_text = text_events[0].data.get("text", "")
        # "? for shortcuts" should appear at most once, not 10 times.
        occurrences = all_text.count("? for shortcuts")
        self.assertLessEqual(occurrences, 1, "duplicate TUI chrome must be deduplicated")

    def test_ansi_sequences_completely_absent_from_events(self):
        """No ANSI escape byte (0x1b) appears in any emitted event text."""
        raw = "\x1b[32mHello\x1b[0m world\n\x1b[1mBold\x1b[0m\n"
        self._run(raw)
        for _, event, _ in self.event_store.appended:
            text = event.data.get("text", "")
            self.assertNotIn("\x1b", text, "ANSI escape must not appear in normalized event text")

    def test_osc_sequences_stripped(self):
        """OSC window-title sequences are not present in emitted event text."""
        raw = "\x1b]0;Claude Code — T1\x07Real assistant response\n"
        self._run(raw)
        text_events = [e for _, e, _ in self.event_store.appended if e.event_type == "text"]
        self.assertTrue(text_events)
        text = text_events[0].data.get("text", "")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("Claude Code — T1", text, "OSC title must be stripped")
        self.assertIn("Real assistant response", text)

    def test_real_content_preserved_through_stripping(self):
        """Meaningful assistant text survives ANSI stripping."""
        assistant_text = "I have read the file and found 3 issues:"
        raw = f"\x1b[1m{assistant_text}\x1b[0m\n"
        self._run(raw)
        text_events = [e for _, e, _ in self.event_store.appended if e.event_type == "text"]
        self.assertTrue(text_events)
        self.assertIn(assistant_text, text_events[0].data.get("text", ""))


# ---------------------------------------------------------------------------
# normalize_conversation — EventStore integration
# ---------------------------------------------------------------------------

class TestNormalizeConversationEventStoreIntegration(unittest.TestCase):
    """normalize_conversation() uses the real EventStore for end-to-end wiring."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.raw_log = self.tmp_dir / "test.log"

    def test_events_readable_via_event_store_tail(self):
        """Events appended by normalize_conversation are readable via EventStore.tail()."""
        from event_store import EventStore

        events_dir = self.tmp_dir / "events"
        store = EventStore(events_dir=events_dir)

        self.raw_log.write_text("Hello from the tmux lane\n", encoding="utf-8")

        count = normalize_conversation(
            self.raw_log,
            store,
            terminal_id="T1",
            dispatch_id="integ-dispatch-001",
            model="sonnet",
        )
        self.assertEqual(count, 2)

        events = list(store.tail("T1"))
        self.assertTrue(events, "EventStore must have events after normalize_conversation")
        event_types = {e.get("type") for e in events}
        self.assertIn("text", event_types)
        self.assertIn("complete", event_types)

    def test_events_carry_provider_and_lane(self):
        """Events in EventStore have provider=claude and lane=tmux_interactive."""
        from event_store import EventStore

        events_dir = self.tmp_dir / "events"
        store = EventStore(events_dir=events_dir)

        self.raw_log.write_text("Some conversation line\n", encoding="utf-8")
        normalize_conversation(
            self.raw_log,
            store,
            terminal_id="T1",
            dispatch_id="integ-dispatch-002",
            model="haiku",
        )

        events = list(store.tail("T1"))
        for ev in events:
            self.assertEqual(ev.get("provider"), "claude")
            self.assertEqual(ev.get("provider_meta", {}).get("lane"), "tmux_interactive")


# ---------------------------------------------------------------------------
# normalize_conversation — best-effort / error handling
# ---------------------------------------------------------------------------

class TestNormalizeConversationBestEffort(unittest.TestCase):
    """normalize_conversation() returns 0 cleanly on edge-case inputs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_event_store_error_propagates_to_caller(self):
        """EventStore.append raising is NOT silenced by normalize_conversation (caller must handle)."""
        raw_log = self.tmp_dir / "test.log"
        raw_log.write_text("Some text\n", encoding="utf-8")

        bad_store = MagicMock()
        bad_store.append.side_effect = RuntimeError("disk full")

        with self.assertRaises(RuntimeError):
            normalize_conversation(
                raw_log,
                bad_store,
                terminal_id="T1",
                dispatch_id="test-err",
                model="sonnet",
            )

    def test_returns_zero_for_all_control_content(self):
        """A log with only control sequences emits zero events."""
        raw_log = self.tmp_dir / "ctrl.log"
        raw_log.write_text("\x1b[2J\x1b[H\r\x00\r\n\r\n", encoding="utf-8")
        store = FakeEventStore()

        count = normalize_conversation(raw_log, store, terminal_id="T1", dispatch_id="ctrl", model="sonnet")
        self.assertEqual(count, 0)
        self.assertEqual(store.appended, [])


if __name__ == "__main__":
    unittest.main()
