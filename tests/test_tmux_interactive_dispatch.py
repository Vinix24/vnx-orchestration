#!/usr/bin/env python3
"""Tests for tmux_interactive_dispatch.py — single-shot ephemeral leaseless model.

PR-TMUX-1b: all lease/close/warm-open tests replaced by single-shot ephemeral coverage.
Each dispatch spawns, drives, collects a receipt, and tears down in a single call.
No fixed terminal identities, no warm-open, no leases.
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

from runtime_coordination import get_connection, get_events, init_schema
from tmux_interactive_dispatch import (
    DEFAULT_COMPLETION_STATUSES,
    InteractiveDispatchResult,
    TmuxInteractiveDispatch,
    TmuxResult,
    _default_launch_command,
    _sanitize_session_name,
)


class FakeTmux:
    """Stub tmux runner that simulates worker completion.

    On the first ``send-keys ... Enter`` following a ``paste-buffer``, writes a
    completion receipt to ``receipts_file`` — simulating the worker calling
    ``append_receipt`` directly after finishing work.
    """

    def __init__(
        self,
        *,
        receipts_file: Path,
        dispatch_id: str,
        available: bool = True,
        spawn_ok: bool = True,
        launch_ok: bool = True,
        deliver_ok: bool = True,
        emit_receipt: bool = True,
        ready_content: str = "Welcome to Claude\n? for shortcuts",
    ) -> None:
        self.receipts_file = receipts_file
        self.dispatch_id = dispatch_id
        self._available = available
        self._spawn_ok = spawn_ok
        self._launch_ok = launch_ok
        self._deliver_ok = deliver_ok
        self._emit_receipt = emit_receipt
        self._ready_content = ready_content
        self.commands: list[list[str]] = []
        self.pasted: list[str] = []
        self.killed_sessions: list[str] = []
        self._pending_paste = False
        self._literal_send_attempted = False
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
            if not self._deliver_ok:
                return TmuxResult(1, "", "load-buffer failed")
            if input_text is not None:
                self.pasted.append(input_text)
            else:
                try:
                    self.pasted.append(Path(args[-1]).read_text(encoding="utf-8"))
                except OSError:
                    self.pasted.append("")
            return TmuxResult(0)

        if cmd == "paste-buffer":
            self._pending_paste = True
            return TmuxResult(0)

        if cmd == "send-keys":
            # First literal-mode send (launch command); fail if launch_ok=False.
            if "-l" in args and not self._literal_send_attempted:
                self._literal_send_attempted = True
                if not self._launch_ok:
                    return TmuxResult(1, "", "send-keys literal failed")
            # paste-buffer + Enter → simulate worker completing.
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
            "terminal": "ephemeral",
            "status": "done",
            "source": "tmux_interactive",
            "seq": self.receipts_written,
        }
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt) + "\n")
        self.receipts_written += 1


class _LaneTestCase(unittest.TestCase):
    DISPATCH_ID = "20260527-tmuxint-test"

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
            project_root=self.state_dir,
        )

    def _fast_dispatch(self, lane: TmuxInteractiveDispatch, **overrides):
        kwargs = dict(
            role="backend-developer",
            model="sonnet",
            deadline_seconds=5.0,
            poll_interval=0.01,
            warmup_timeout=0.5,
            warmup_poll_interval=0.01,
        )
        kwargs.update(overrides)
        return lane.dispatch("Do the thing.", self.DISPATCH_ID, **kwargs)


class TestSingleShotSuccess(_LaneTestCase):
    def test_single_shot_success(self):
        """Happy path: spawn -> receipt appears -> success; session killed, handle removed."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertIsInstance(result, InteractiveDispatchResult)
        self.assertTrue(result.success, result.failure_reason)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["status"], "done")
        self.assertEqual(result.dispatch_id, self.DISPATCH_ID)
        # Teardown must have run: session killed.
        self.assertTrue(fake.killed_sessions, "kill-session was not called on success teardown")
        # Teardown must have run: handle removed.
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertFalse(handle_path.exists(), "handle file must be removed after teardown")

    def test_completion_protocol_absolute_path(self):
        """F1: body injected into the session contains the absolute path to append_receipt.py."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        self._fast_dispatch(lane)

        expected_abs = str(self.state_dir / "scripts" / "append_receipt.py")
        delivered = "\n".join(fake.pasted)
        self.assertIn(
            "python3 " + expected_abs,
            delivered,
            "absolute path to append_receipt.py must appear in the delivered body",
        )

    def test_enter_is_separate_keystroke(self):
        """Launch cmd and body paste each submit Enter as a standalone send-keys call."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        self._fast_dispatch(lane)

        cmds = fake.commands

        # After the literal-mode send-keys (launch command), the very next send-keys = Enter.
        literal_idx = next(
            (i for i, c in enumerate(cmds) if c and c[0] == "send-keys" and "-l" in c),
            None,
        )
        self.assertIsNotNone(literal_idx, "no send-keys -l (literal) found for launch command")
        post_literal_sk = [c for c in cmds[literal_idx + 1:] if c and c[0] == "send-keys"]
        self.assertTrue(post_literal_sk, "no send-keys after launch literal")
        self.assertEqual(post_literal_sk[0][-1], "Enter")
        # Must be: send-keys -t <pane> Enter  (4 tokens, no combined payload)
        self.assertEqual(len(post_literal_sk[0]), 4)

        # After paste-buffer, the very next send-keys = Enter.
        paste_idx = next(
            (i for i, c in enumerate(cmds) if c and c[0] == "paste-buffer"),
            None,
        )
        self.assertIsNotNone(paste_idx, "no paste-buffer found for body delivery")
        post_paste_sk = [c for c in cmds[paste_idx + 1:] if c and c[0] == "send-keys"]
        self.assertTrue(post_paste_sk, "no send-keys after paste-buffer")
        self.assertEqual(post_paste_sk[0][-1], "Enter")
        self.assertEqual(len(post_paste_sk[0]), 4)

    def test_unique_session_per_dispatch(self):
        """Different dispatch_ids yield different session names with no T1/T2/T3 literals."""
        id1, id2 = "20260527-worker-alpha", "20260527-worker-beta"
        sessions = []
        for did in (id1, id2):
            fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=did)
            lane = TmuxInteractiveDispatch(
                self.state_dir,
                runner=fake,
                receipts_file=self.receipts_file,
                project_root=self.state_dir,
            )
            r = lane.dispatch(
                "Do the thing.", did,
                model="sonnet", deadline_seconds=5.0,
                poll_interval=0.01, warmup_timeout=0.5, warmup_poll_interval=0.01,
            )
            sessions.append(r.session)

        self.assertNotEqual(sessions[0], sessions[1], "different dispatch_ids must yield different sessions")
        for s in sessions:
            self.assertIsNotNone(s)
            for fixed_terminal in ("T1", "T2", "T3"):
                self.assertNotIn(fixed_terminal, s, f"session name must not contain {fixed_terminal!r}")

    def test_scope_note_injected(self):
        """dispatch_paths injects a scope block into the body; absent paths injects nothing."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        self._fast_dispatch(lane, dispatch_paths=["scripts/", "tests/"])
        delivered = "\n".join(fake.pasted)
        self.assertIn("Edit ONLY within these paths", delivered)
        self.assertIn("`scripts/`", delivered)

        # Without paths — use a separate receipts file to isolate from prior dispatch.
        did2 = "20260527-no-scope"
        rf2 = self.state_dir / "receipts2.ndjson"
        fake2 = FakeTmux(receipts_file=rf2, dispatch_id=did2)
        lane2 = TmuxInteractiveDispatch(
            self.state_dir, runner=fake2, receipts_file=rf2, project_root=self.state_dir,
        )
        lane2.dispatch(
            "Do the thing.", did2,
            model="sonnet", deadline_seconds=5.0,
            poll_interval=0.01, warmup_timeout=0.5, warmup_poll_interval=0.01,
        )
        delivered2 = "\n".join(fake2.pasted)
        self.assertNotIn("Edit ONLY within these paths", delivered2)


class TestTimeoutTeardown(_LaneTestCase):
    def test_timeout_tears_down(self):
        """Deadline exceeded: failure result, session killed, handle removed, exit event emitted."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)
        self.assertTrue(fake.killed_sessions, "kill-session must be called on timeout")
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertFalse(handle_path.exists(), "handle must be removed on timeout teardown")
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        self.assertIn("interactive_exit", {e["event_type"] for e in events})


class TestLaunchFailureTeardown(_LaneTestCase):
    def test_launch_failure_tears_down(self):
        """Failed send-keys for launch: teardown runs, no instruction delivered, failure result."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            launch_ok=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertIn("launch", result.failure_reason)
        self.assertTrue(fake.killed_sessions, "kill-session must be called on launch failure")
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertFalse(handle_path.exists())
        self.assertEqual(fake.pasted, [], "instruction must not be delivered when launch fails")


class TestDeliverFailureTeardown(_LaneTestCase):
    def test_deliver_failure_tears_down(self):
        """load-buffer failure during delivery: teardown runs, failure result."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            deliver_ok=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertIn("deliver", result.failure_reason)
        self.assertTrue(fake.killed_sessions, "kill-session must be called on deliver failure")
        handle_path = self.state_dir / "tmux_interactive" / f"{self.DISPATCH_ID}.json"
        self.assertFalse(handle_path.exists())


class TestNoDashP(_LaneTestCase):
    def test_no_dash_p_in_launch(self):
        """F2: default launch command has no -p; extra_flags with -p/--print raises ValueError."""
        cmd = _default_launch_command("sonnet")
        self.assertIn("claude --model sonnet", cmd)
        self.assertNotIn("-p", cmd.split())
        self.assertNotIn("--print", cmd.split())

        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="-p")
        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="--print")
        with self.assertRaises(ValueError):
            _default_launch_command("sonnet", extra_flags="--print=stream")

        # Benign flag must still build without error.
        safe = _default_launch_command("sonnet", extra_flags="--verbose")
        self.assertIn("--verbose", safe)
        self.assertNotIn("-p", safe.split())


class TestStaleReceiptGuard(_LaneTestCase):
    def test_stale_receipt_does_not_complete(self):
        """F3: pre-existing receipt for dispatch_id must not satisfy the baseline-guarded wait."""
        stale = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "ephemeral",
            "status": "done",
            "source": "stale_from_prior_run",
        }
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stale) + "\n")

        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)


class TestTeardownIdempotent(_LaneTestCase):
    def test_teardown_idempotent(self):
        """Early teardown (launch fail) + finally teardown must not double-kill the session."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            launch_ok=False,
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane)

        kill_cmds = [c for c in fake.commands if c and c[0] == "kill-session"]
        self.assertEqual(
            len(kill_cmds), 1,
            f"kill-session called {len(kill_cmds)} times; _torn_down guard must prevent double-kill",
        )


if __name__ == "__main__":
    unittest.main()
