#!/usr/bin/env python3
"""Tests for tmux_interactive_dispatch.py (PR-TMUX-1).

Exercises the real two-phase lane logic through a stub tmux runner — no live
Claude call, mirroring the burn-in / subprocess stub convention. The stub
simulates the worker by appending a completion receipt to the canonical NDJSON
when the dispatch (or follow-up) instruction is submitted (paste-buffer + Enter).

Coverage:
  * Phase A round-trip: spawn -> drive -> receipt; lease acquired; handle persisted.
  * Enter is ALWAYS a separate keystroke after a paste-buffer.
  * Completion-protocol footer instructs the worker to call append_receipt.
  * Window stays WARM-OPEN after Phase A (no kill-session).
  * Phase B close: kills session, releases lease, removes handle.
  * Phase B follow-up: drives the warm session and awaits the follow-up receipt.
  * NDJSON audit parity: coordination_events emitted for spawn/deliver/close.
  * Negative paths: tmux missing, spawn failure, receipt timeout, double-close.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import get_connection, get_events, get_lease, init_schema
from tmux_interactive_dispatch import (
    DEFAULT_COMPLETION_STATUSES,
    InteractiveCloseResult,
    InteractiveDispatchResult,
    TmuxInteractiveDispatch,
    TmuxResult,
    _default_launch_command,
    _sanitize_session_name,
)


class FakeTmux:
    """In-memory tmux that records commands and simulates a worker receipt.

    On the first ``send-keys ... Enter`` that follows a ``paste-buffer`` it
    appends a completion receipt for ``dispatch_id`` to ``receipts_file`` — the
    stand-in for the interactive worker calling ``append_receipt`` directly.
    """

    def __init__(
        self,
        *,
        receipts_file: Path,
        dispatch_id: str,
        terminal_id: str,
        available: bool = True,
        spawn_ok: bool = True,
        emit_receipt: bool = True,
        ready_content: str = "Welcome to Claude\n? for shortcuts",
    ) -> None:
        self.receipts_file = receipts_file
        self.dispatch_id = dispatch_id
        self.terminal_id = terminal_id
        self._available = available
        self._spawn_ok = spawn_ok
        self._emit_receipt = emit_receipt
        self._ready_content = ready_content
        self.commands: list[list[str]] = []
        self.pasted: list[str] = []
        self.killed_sessions: list[str] = []
        self._pending_paste = False
        self.receipts_written = 0

    def available(self) -> bool:
        return self._available

    def run(self, args, *, timeout: int = 10, input_text=None) -> TmuxResult:
        self.commands.append(list(args))
        cmd = args[0]

        if cmd == "new-session":
            if not self._spawn_ok:
                return TmuxResult(1, "", "session create failed")
            return TmuxResult(0, "%1\n")
        if cmd == "display-message":
            return TmuxResult(0, "@1\n")
        if cmd == "capture-pane":
            return TmuxResult(0, self._ready_content)
        if cmd == "load-buffer":
            if input_text is not None:
                self.pasted.append(input_text)
            else:
                # last arg is a temp file path
                try:
                    self.pasted.append(Path(args[-1]).read_text(encoding="utf-8"))
                except OSError:
                    self.pasted.append("")
            return TmuxResult(0)
        if cmd == "paste-buffer":
            self._pending_paste = True
            return TmuxResult(0)
        if cmd == "send-keys":
            if args[-1] == "Enter" and self._pending_paste:
                self._pending_paste = False
                if self._emit_receipt:
                    self._write_receipt()
            return TmuxResult(0)
        if cmd == "kill-session":
            self.killed_sessions.append(args[-1])
            return TmuxResult(0)
        if cmd == "switch-client":
            return TmuxResult(0)
        return TmuxResult(0)

    def _write_receipt(self) -> None:
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.dispatch_id,
            "terminal": self.terminal_id,
            "status": "done",
            "source": "tmux_interactive",
            "seq": self.receipts_written,
        }
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt) + "\n")
        self.receipts_written += 1


class _LaneTestCase(unittest.TestCase):
    DISPATCH_ID = "20260527-tmuxint-test"
    TERMINAL = "T1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        init_schema(self.state_dir)
        self.receipts_file = self.state_dir / "t0_receipts.ndjson"

    def _make_lane(self, fake: FakeTmux) -> TmuxInteractiveDispatch:
        return TmuxInteractiveDispatch(
            self.state_dir,
            runner=fake,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,  # no terminal dir -> spawn cwd = root
        )

    def _fast_dispatch(self, lane, fake, **overrides):
        kwargs = dict(
            role="backend-developer",
            model="sonnet",
            deadline_seconds=5.0,
            poll_interval=0.01,
            warmup_timeout=0.5,
            warmup_poll_interval=0.01,
        )
        kwargs.update(overrides)
        return lane.dispatch(self.TERMINAL, "Do the thing.", self.DISPATCH_ID, **kwargs)


class TestPhaseADispatch(_LaneTestCase):
    def test_round_trip_success(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake)

        self.assertIsInstance(result, InteractiveDispatchResult)
        self.assertTrue(result.success, result.failure_reason)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["status"], "done")
        self.assertEqual(result.pane_id, "%1")
        self.assertEqual(result.window_id, "@1")
        self.assertIsNotNone(result.lease_generation)

    def test_window_stays_warm_open_after_phase_a(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)
        # Phase A must NOT close the window.
        self.assertEqual(fake.killed_sessions, [])
        # Handle persisted so Phase B can find the warm window.
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertTrue(handle_path.exists())
        handle = json.loads(handle_path.read_text())
        self.assertEqual(handle["phase"], "warm_open")
        self.assertEqual(handle["pane_id"], "%1")

    def test_enter_is_separate_keystroke_after_paste(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)

        # Find the paste-buffer for the dispatch instruction, then assert the
        # very next send-keys is a standalone "Enter" (not combined).
        idx = next(
            i for i, c in enumerate(fake.commands) if c and c[0] == "paste-buffer"
        )
        following = [c for c in fake.commands[idx + 1 :] if c and c[0] == "send-keys"]
        self.assertTrue(following, "no send-keys after paste-buffer")
        self.assertEqual(following[0][-1], "Enter")
        self.assertEqual(len(following[0]), 4)  # send-keys -t <pane> Enter

    def test_completion_protocol_footer_present(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)
        # The pasted instruction body must instruct the worker to emit a receipt.
        delivered = "\n".join(fake.pasted)
        self.assertIn("append_receipt.py", delivered)
        self.assertIn(self.DISPATCH_ID, delivered)

    def test_lease_acquired_in_db(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)
        with get_connection(self.state_dir) as conn:
            lease = get_lease(conn, self.TERMINAL)
        self.assertEqual(lease["state"], "leased")
        self.assertEqual(lease["dispatch_id"], self.DISPATCH_ID)

    def test_audit_events_emitted(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        types = {e["event_type"] for e in events}
        for expected in (
            "interactive_spawn",
            "interactive_deliver_start",
            "interactive_deliver_success",
            "interactive_receipt_observed",
            "interactive_warm_open",
        ):
            self.assertIn(expected, types)

    def test_attach_false_by_default_no_switch_client(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake)
        self.assertFalse(result.attached)
        self.assertFalse(any(c[0] == "switch-client" for c in fake.commands))


class TestPhaseANegative(_LaneTestCase):
    def test_tmux_unavailable(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
            available=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake)
        self.assertFalse(result.success)
        self.assertIn("tmux", result.failure_reason)

    def test_spawn_failure(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
            spawn_ok=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake)
        self.assertFalse(result.success)
        self.assertIn("new-session", result.failure_reason)

    def test_receipt_timeout_leaves_window_open(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
            emit_receipt=False,  # worker never emits a receipt
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake, deadline_seconds=0.2, poll_interval=0.02)
        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)
        # Warm-open contract: window NOT killed on timeout, handle persisted.
        self.assertEqual(fake.killed_sessions, [])
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertTrue(handle_path.exists())

    def test_stale_receipt_does_not_complete_phase_a(self):
        """A pre-existing completion receipt for the same dispatch_id must NOT
        trigger a false Phase-A success.  The baseline snapshot taken before
        delivery ensures the stale receipt is counted in the baseline so only
        a fresh receipt (len > baseline) would satisfy the wait."""
        # Pre-write a stale matching completion receipt for the dispatch_id.
        stale = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": self.TERMINAL,
            "status": "done",
            "source": "stale_from_prior_run",
        }
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stale) + "\n")

        # emit_receipt=False: the stub worker never emits a fresh receipt.
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, fake, deadline_seconds=0.2, poll_interval=0.02)

        # The stale receipt must NOT count as completion — must time out.
        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)


class TestPhaseBClose(_LaneTestCase):
    def _dispatched_lane(self):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            terminal_id=self.TERMINAL,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, fake)
        return lane, fake

    def test_close_kills_session_and_releases_lease(self):
        lane, fake = self._dispatched_lane()
        result = lane.close(self.DISPATCH_ID)
        self.assertIsInstance(result, InteractiveCloseResult)
        self.assertTrue(result.success)
        self.assertTrue(result.window_killed)
        self.assertTrue(result.lease_released)
        self.assertIn(_sanitize_session_name(f"vnx-int-{self.DISPATCH_ID}"), fake.killed_sessions)

        # Lease back to idle.
        with get_connection(self.state_dir) as conn:
            lease = get_lease(conn, self.TERMINAL)
        self.assertEqual(lease["state"], "idle")
        # Handle removed.
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertFalse(handle_path.exists())

    def test_close_emits_window_closed_event(self):
        lane, fake = self._dispatched_lane()
        lane.close(self.DISPATCH_ID)
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        types = {e["event_type"] for e in events}
        self.assertIn("interactive_window_closed", types)

    def test_close_with_follow_up(self):
        lane, fake = self._dispatched_lane()
        baseline_receipts = fake.receipts_written
        result = lane.close(
            self.DISPATCH_ID,
            follow_up_instruction="Now fix the lint error.",
            deadline_seconds=5.0,
            poll_interval=0.01,
        )
        self.assertTrue(result.success, result.failure_reason)
        self.assertIsNotNone(result.follow_up_receipt)
        # A new receipt was driven by the follow-up.
        self.assertEqual(fake.receipts_written, baseline_receipts + 1)
        self.assertTrue(result.window_killed)
        self.assertTrue(result.lease_released)

    def test_double_close_is_idempotent(self):
        lane, fake = self._dispatched_lane()
        first = lane.close(self.DISPATCH_ID)
        self.assertTrue(first.success)
        second = lane.close(self.DISPATCH_ID)
        self.assertTrue(second.success)
        self.assertFalse(second.window_killed)
        self.assertFalse(second.lease_released)

    def test_follow_up_timeout_still_closes(self):
        lane, fake = self._dispatched_lane()
        # Stop emitting receipts so the follow-up wait times out.
        fake._emit_receipt = False
        result = lane.close(
            self.DISPATCH_ID,
            follow_up_instruction="Try again.",
            deadline_seconds=0.2,
            poll_interval=0.02,
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.follow_up_receipt)
        # Cleanup still happened.
        self.assertTrue(result.window_killed)
        self.assertTrue(result.lease_released)


class TestHelpers(_LaneTestCase):
    def test_default_launch_command_interactive_not_headless(self):
        cmd = _default_launch_command("sonnet")
        self.assertIn("claude --model sonnet", cmd)
        self.assertNotIn(" -p", cmd)  # interactive, never headless
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_default_launch_command_skip_permissions(self):
        cmd = _default_launch_command("opus", skip_permissions=True)
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_sanitize_session_name(self):
        self.assertEqual(_sanitize_session_name("vnx-int-2026.05:27"), "vnx-int-2026-05-27")

    def test_default_completion_statuses(self):
        self.assertIn("done", DEFAULT_COMPLETION_STATUSES)
        self.assertIn("failed", DEFAULT_COMPLETION_STATUSES)

    def test_extra_flags_rejects_print_mode(self):
        """extra_flags containing -p or --print must raise ValueError (billing
        invariant: the interactive lane must never become headless)."""
        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="-p")
        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="--print")
        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="--print=stream")
        # A benign extra flag must still build the command without error.
        cmd = _default_launch_command("sonnet", extra_flags="--verbose")
        self.assertIn("--verbose", cmd)
        self.assertNotIn("-p", cmd.split())
        self.assertNotIn("--print", cmd.split())


if __name__ == "__main__":
    unittest.main()
