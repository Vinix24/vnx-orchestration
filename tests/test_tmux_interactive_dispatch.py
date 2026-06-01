#!/usr/bin/env python3
"""Tests for tmux_interactive_dispatch.py — single-shot ephemeral leaseless model.

PR-TMUX-1b: all lease/close/warm-open tests replaced by single-shot ephemeral coverage.
Each dispatch spawns, drives, collects a receipt, and tears down in a single call.
No fixed terminal identities, no warm-open, no leases.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import get_connection, get_events, init_schema
from tmux_interactive_dispatch import (
    DEFAULT_COMPLETION_STATUSES,
    InteractiveDispatchResult,
    TmuxInteractiveDispatch,
    TmuxResult,
    _assert_no_headless_flags,
    _default_launch_command,
    _resolve_state_dir,
    _sanitize_session_name,
    main,
)
from tmux_worktree import ReapResult, WorktreeAllocateError, WorktreeHandle


class FakeTmux:
    """Stub tmux runner that simulates worker completion.

    On the first ``send-keys ... Enter`` following a ``paste-buffer``, writes a
    completion receipt to ``receipts_file`` — simulating the worker calling
    ``append_receipt`` directly after finishing work.

    ``post_paste_capture_seq``: optional list of strings consumed (FIFO) by
    capture-pane calls that happen AFTER the first Enter following paste-buffer.
    Used to simulate "staged" vs "working" states for submit-verify tests.
    When the list is exhausted, falls back to ``ready_content``.
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
        receipt_status: str = "done",
        ready_content: str = "Welcome to Claude\n? for shortcuts",
        post_paste_capture_seq: "list[str] | None" = None,
    ) -> None:
        self.receipts_file = receipts_file
        self.dispatch_id = dispatch_id
        self._available = available
        self._spawn_ok = spawn_ok
        self._launch_ok = launch_ok
        self._deliver_ok = deliver_ok
        self._emit_receipt = emit_receipt
        self._receipt_status = receipt_status
        self._ready_content = ready_content
        self._post_paste_seq: list[str] = list(post_paste_capture_seq or [])
        self.commands: list[list[str]] = []
        self.pasted: list[str] = []
        self.killed_sessions: list[str] = []
        self._pending_paste = False
        self._paste_fired = False  # True after first Enter following paste-buffer
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
            # Post-paste: consume the staged-response queue (FIFO).
            if self._paste_fired and self._post_paste_seq:
                return TmuxResult(0, self._post_paste_seq.pop(0))
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
            # First Enter after paste-buffer → simulate worker completing.
            if args[-1] == "Enter" and self._pending_paste:
                self._pending_paste = False
                self._paste_fired = True
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
            "status": self._receipt_status,
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
            isolated_worktree=False,  # existing tests focus on tmux logic, not worktree
        )
        kwargs.update(overrides)
        # Zero-out time-sensitive env vars so tests run fast.
        _env = {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
        }
        with patch.dict(os.environ, _env):
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
        m = re.search(r"```bash\n(.+?)\n```", delivered, re.DOTALL)
        self.assertIsNotNone(m, "could not extract bash command from delivered body")
        argv = shlex.split(m.group(1).strip())
        python_idx = argv.index("python3")
        self.assertEqual(
            argv[python_idx + 1],
            expected_abs,
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
                isolated_worktree=False,
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


class TestReceiptStatusControlsSuccess(_LaneTestCase):
    def test_dispatch_returns_failure_on_failed_receipt(self):
        """A worker failure receipt must produce a failed lane result."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            receipt_status="failed",
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["status"], "failed")
        self.assertIn("failed", result.failure_reason)

    def test_dispatch_returns_failure_on_blocked_receipt(self):
        """A worker blocked receipt must produce a failed lane result."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            receipt_status="blocked",
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["status"], "blocked")
        self.assertIn("blocked", result.failure_reason)

    def test_dispatch_cli_exits_nonzero_on_failed(self):
        """CLI return code propagates a failed worker receipt result."""
        failed_receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": "cli-failed-receipt",
            "terminal": "ephemeral",
            "status": "failed",
            "source": "tmux_interactive",
        }

        def fake_dispatch(self_inner, instruction, dispatch_id, **kwargs):
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                receipt=failed_receipt,
                failure_reason="worker_status: failed",
            )

        with patch.object(TmuxInteractiveDispatch, "dispatch", fake_dispatch):
            with patch(
                "tmux_interactive_dispatch._resolve_state_dir",
                return_value=self.state_dir,
            ):
                rc = main([
                    "--dispatch-id", "cli-failed-receipt",
                    "--instruction", "do the thing",
                    "--shared-worktree",
                ])

        self.assertNotEqual(rc, 0)


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


class TestHeadlessGuard(_LaneTestCase):
    def test_model_with_dash_p_rejected(self):
        """model='sonnet -p' raises ValueError before any tmux session is spawned."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        with self.assertRaises(ValueError):
            self._fast_dispatch(lane, model="sonnet -p")
        spawn_cmds = [c for c in fake.commands if c and c[0] == "new-session"]
        self.assertEqual(spawn_cmds, [], "no session must be spawned for an invalid model")

    def test_model_with_print_rejected(self):
        """model='sonnet --print' raises ValueError before any tmux session is spawned."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        with self.assertRaises(ValueError):
            self._fast_dispatch(lane, model="sonnet --print")
        spawn_cmds = [c for c in fake.commands if c and c[0] == "new-session"]
        self.assertEqual(spawn_cmds, [], "no session must be spawned for an invalid model")

    def test_custom_launch_builder_with_dash_p_rejected(self):
        """Custom launch_builder returning a command with '-p' is blocked by final-command guard."""
        def bad_builder(model, *, skip_permissions=False, extra_flags="", **kwargs):
            return "claude -p something"

        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = TmuxInteractiveDispatch(
            self.state_dir,
            runner=fake,
            launch_builder=bad_builder,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,
        )
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "headless_flag_blocked")
        # Guard fires before _launch_claude, so no literal-mode send-keys should occur.
        literal_sends = [c for c in fake.commands if c and c[0] == "send-keys" and "-l" in c]
        self.assertEqual(literal_sends, [], "launch send-keys must not happen when headless flag blocked")
        self.assertTrue(fake.killed_sessions, "session must be killed on headless_flag_blocked teardown")

    def test_assert_no_headless_flags_raises_on_dash_p(self):
        """_assert_no_headless_flags raises ValueError for command containing -p."""
        with self.assertRaises(ValueError):
            _assert_no_headless_flags("source ~/.zshrc; claude --model sonnet -p")

    def test_assert_no_headless_flags_passes_clean_command(self):
        """_assert_no_headless_flags is silent for a clean interactive launch command."""
        _assert_no_headless_flags("source ~/.zshrc 2>/dev/null; claude --model sonnet")


class TestTeardownGlobal(_LaneTestCase):
    def test_persist_handle_exception_still_tears_down(self):
        """Exception in _persist_handle triggers teardown and returns a failure result."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        def raise_on_persist(dispatch_id, handle):
            raise RuntimeError("simulated persist failure")

        lane._persist_handle = raise_on_persist
        result = self._fast_dispatch(lane)

        self.assertFalse(result.success, "dispatch must return failure result when _persist_handle raises")
        self.assertTrue(
            fake.killed_sessions,
            "session must be killed (teardown ran) even when _persist_handle raises",
        )


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


class TestCompletionProtocolIntegration(_LaneTestCase):
    def test_completion_protocol_payload_accepted_by_append_receipt(self):
        """Integration: completion-protocol receipt JSON passes _validate_receipt without error.

        This test FAILS before the timestamp fix (missing_required_key: timestamp)
        and PASSES after. It closes the gap that unit tests with stubbed
        append_receipt missed.
        """
        from append_receipt_internals.common import AppendReceiptError
        from append_receipt_internals.validation import _validate_receipt

        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        protocol = lane._build_completion_protocol(self.DISPATCH_ID, "T1")

        m = re.search(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
        self.assertIsNotNone(m, "could not extract bash command from completion protocol")
        argv = shlex.split(m.group(1).strip())
        receipt = json.loads(argv[argv.index("--receipt") + 1])

        try:
            _validate_receipt(receipt)
        except AppendReceiptError as exc:
            self.fail(f"_validate_receipt raised AppendReceiptError: {exc.message}")

        self.assertTrue(receipt.get("timestamp"), "timestamp must be non-empty in protocol receipt")
        self.assertEqual(receipt.get("event_type"), "subprocess_completion")
        self.assertEqual(receipt.get("dispatch_id"), self.DISPATCH_ID)

    def test_completion_protocol_shell_safe_quoting(self):
        """Completion protocol shell command round-trips paths and JSON safely."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state dir's $(touch injected)"
            receipts_file = state_dir / "receipts file's $(touch receipt).ndjson"
            project_root = tmp_path / "project root's $(touch project)"
            lane = TmuxInteractiveDispatch(
                state_dir=state_dir,
                receipts_file=receipts_file,
                project_root=project_root,
            )

            protocol = lane._build_completion_protocol(
                self.DISPATCH_ID,
                "vincent's-test",
            )

            m = re.search(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
            self.assertIsNotNone(m, "could not extract bash command from completion protocol")
            argv = shlex.split(m.group(1).strip())

            self.assertIn(f"VNX_STATE_DIR={state_dir}", argv)
            self.assertIn(f"VNX_DATA_DIR={tmp_path}", argv)
            python_idx = argv.index("python3")
            self.assertEqual(
                argv[python_idx + 1],
                str(project_root / "scripts" / "append_receipt.py"),
            )
            self.assertEqual(
                argv[argv.index("--receipts-file") + 1],
                str(receipts_file),
            )

            receipt = json.loads(argv[argv.index("--receipt") + 1])
            self.assertEqual(receipt["dispatch_id"], self.DISPATCH_ID)
            self.assertEqual(receipt["terminal"], "vincent's-test")
            self.assertEqual(receipt["status"], "done")


class TestCompletionProtocolPinsReceiptsFile(_LaneTestCase):
    def test_completion_protocol_env_pins_receipts_file_landing(self):
        """Env-pin: VNX_STATE_DIR/VNX_DATA_DIR are prepended to the python3 command.

        Invoking the extracted shell command (via subprocess shell=True) must write
        the receipt to the lane's state_dir/t0_receipts.ndjson — proving that env is
        the working channel (--receipts-file is also present as defensive belt-and-suspenders).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()

            real_project_root = SCRIPT_DIR.parent

            lane = TmuxInteractiveDispatch(
                state_dir=state_dir,
                receipts_file=state_dir / "t0_receipts.ndjson",
                project_root=real_project_root,
            )

            protocol = lane._build_completion_protocol(self.DISPATCH_ID, "T1")

            # Part 1: env vars present in the command string.
            m = re.search(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
            self.assertIsNotNone(m, "could not extract bash command from completion protocol")
            cmd_string = m.group(1).strip()
            argv = shlex.split(cmd_string)
            self.assertIn(f"VNX_STATE_DIR={state_dir}", argv)
            self.assertIn(f"VNX_DATA_DIR={tmp_path}", argv)
            python_idx = argv.index("python3")
            self.assertEqual(
                argv[python_idx + 1],
                str(real_project_root / "scripts" / "append_receipt.py"),
            )
            self.assertEqual(
                argv[argv.index("--receipts-file") + 1],
                str(state_dir / "t0_receipts.ndjson"),
            )

            # Part 2: extract the bash command and invoke it via shell.
            proc = subprocess.run(cmd_string, shell=True, capture_output=True, text=True)
            self.assertEqual(
                proc.returncode,
                0,
                f"shell command exited {proc.returncode}: stderr={proc.stderr}",
            )

            receipts_ndjson = state_dir / "t0_receipts.ndjson"
            self.assertTrue(
                receipts_ndjson.exists(),
                f"receipt must land at {receipts_ndjson}",
            )
            lines = [
                ln for ln in receipts_ndjson.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
            self.assertEqual(len(lines), 1, "exactly one receipt line must be written")


class TestStateDirMatchesCanonical(unittest.TestCase):
    """Regression guard: lane's _resolve_state_dir must agree with the canonical resolver.

    Guards against future ad-hoc-resolver drift that caused the live central-install
    receipt timeout bug (lane polled local path, worker wrote to central path via
    project_root.resolve_state_dir).
    """

    def test_lane_state_dir_matches_append_receipt_resolver(self):
        from project_root import resolve_state_dir as canonical

        canonical_path = canonical()
        lane_path = _resolve_state_dir()

        self.assertEqual(
            canonical_path,
            lane_path,
            f"MISMATCH: lane={lane_path!r} != canonical={canonical_path!r}",
        )


# ---------------------------------------------------------------------------
# PR-TMUX-3: Worktree isolation integration tests
# ---------------------------------------------------------------------------

class _WorktreeTestCase(_LaneTestCase):
    """Base for worktree-integration tests — injects a fake WorktreeHandle."""

    def _make_handle(self, path: Path | None = None) -> WorktreeHandle:
        wt_path = path or (self.state_dir / "worktrees" / f"dispatch-{self.DISPATCH_ID}")
        wt_path.mkdir(parents=True, exist_ok=True)
        return WorktreeHandle(
            path=wt_path,
            branch=f"dispatch/{self.DISPATCH_ID}",
            base_sha="deadbeef" * 5,
            base_ref="origin/main",
            dispatch_id=self.DISPATCH_ID,
        )


class TestWorktreeDefaultIsolated(_WorktreeTestCase):
    def test_dispatch_default_uses_isolated_worktree(self):
        """dispatch() with no explicit isolated_worktree allocates a worktree (True is the default)."""
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        # Call lane.dispatch() directly — no isolated_worktree kwarg — to exercise the TRUE default.
        with patch("tmux_interactive_dispatch.allocate", return_value=handle) as mock_allocate:
            with patch("tmux_interactive_dispatch.classify", return_value="clean"):
                with patch(
                    "tmux_interactive_dispatch.reap",
                    return_value=ReapResult(removed=True),
                ):
                    result = lane.dispatch(
                        "Do the thing.",
                        self.DISPATCH_ID,
                        role="backend-developer",
                        model="sonnet",
                        deadline_seconds=5.0,
                        poll_interval=0.01,
                        warmup_timeout=0.5,
                        warmup_poll_interval=0.01,
                    )

        mock_allocate.assert_called_once()
        call_kw = mock_allocate.call_args
        called_id = call_kw.kwargs.get("dispatch_id") or (call_kw.args[0] if call_kw.args else None)
        self.assertEqual(called_id, self.DISPATCH_ID)

        new_session_cmds = [c for c in fake.commands if c and c[0] == "new-session"]
        self.assertTrue(new_session_cmds, "new-session must have been called")
        ns_cmd = new_session_cmds[0]
        c_idx = ns_cmd.index("-c") if "-c" in ns_cmd else -1
        self.assertGreaterEqual(c_idx, 0, "-c flag must be present in new-session command")
        self.assertEqual(ns_cmd[c_idx + 1], str(handle.path))


class TestWorktreeSharedOptOut(_WorktreeTestCase):
    def test_dispatch_shared_worktree_opt_out(self):
        """isolated_worktree=False skips allocation and uses project_root as cwd (back-compat)."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch("tmux_interactive_dispatch.allocate") as mock_allocate:
            # explicit False — ensures the shared path is tested
            result = self._fast_dispatch(lane, isolated_worktree=False)

        mock_allocate.assert_not_called()
        new_session_cmds = [c for c in fake.commands if c and c[0] == "new-session"]
        self.assertTrue(new_session_cmds)
        ns_cmd = new_session_cmds[0]
        c_idx = ns_cmd.index("-c") if "-c" in ns_cmd else -1
        self.assertGreaterEqual(c_idx, 0)
        # cwd must be project_root (state_dir in _make_lane), not a worktree path
        self.assertEqual(ns_cmd[c_idx + 1], str(self.state_dir))


class TestWorktreeAllocateFailure(_WorktreeTestCase):
    def test_dispatch_worktree_allocate_failure_no_spawn(self):
        """allocate() raising WorktreeAllocateError aborts before any tmux spawn."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch(
            "tmux_interactive_dispatch.allocate",
            side_effect=WorktreeAllocateError("disk full"),
        ):
            result = self._fast_dispatch(lane, isolated_worktree=True)

        self.assertFalse(result.success)
        self.assertIn("worktree_add_failed", result.failure_reason)
        spawn_cmds = [c for c in fake.commands if c and c[0] == "new-session"]
        self.assertEqual(spawn_cmds, [], "no tmux session must be spawned on allocate failure")


class TestWorktreeTeardownLifecycle(_WorktreeTestCase):
    def test_teardown_classifies_and_reaps(self):
        """On success teardown, classify() and reap() are called; result has worktree_state."""
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        mock_reap = MagicMock(return_value=ReapResult(removed=True))

        with patch("tmux_interactive_dispatch.allocate", return_value=handle):
            with patch(
                "tmux_interactive_dispatch.classify", return_value="clean"
            ) as mock_classify:
                with patch("tmux_interactive_dispatch.reap", mock_reap):
                    result = self._fast_dispatch(lane, isolated_worktree=True)

        mock_classify.assert_called_once_with(handle)
        mock_reap.assert_called_once_with(handle, "clean")
        self.assertEqual(result.worktree_state, "clean")

    def test_teardown_dirty_preserves_emits_event(self):
        """Dirty worktree: reap preserves it and interactive_teardown_preserved is emitted."""
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        dirty_reap = ReapResult(removed=False, preserved_path=handle.path)

        with patch("tmux_interactive_dispatch.allocate", return_value=handle):
            with patch("tmux_interactive_dispatch.classify", return_value="dirty"):
                with patch("tmux_interactive_dispatch.reap", return_value=dirty_reap):
                    result = self._fast_dispatch(lane, isolated_worktree=True)

        # Worktree directory still present (reap did not remove it in our mock).
        self.assertTrue(handle.path.is_dir())

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        event_types = {e["event_type"] for e in events}
        self.assertIn(
            "interactive_teardown_preserved",
            event_types,
            "interactive_teardown_preserved must be emitted for dirty worktree",
        )
        self.assertEqual(result.worktree_state, "dirty")


class TestWorktreeCliFlags(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def test_cli_flags_isolated_and_base_ref_parsed(self):
        """--shared-worktree and --base-ref are parsed and forwarded to dispatch()."""
        captured: dict = {}

        def fake_dispatch(self_inner, instruction, dispatch_id, **kwargs):
            captured.update(kwargs)
            return InteractiveDispatchResult(success=True, dispatch_id=dispatch_id)

        with patch.object(TmuxInteractiveDispatch, "dispatch", fake_dispatch):
            with patch(
                "tmux_interactive_dispatch._resolve_state_dir",
                return_value=self.state_dir,
            ):
                main([
                    "--dispatch-id", "cli-wt-test",
                    "--instruction", "do the thing",
                    "--shared-worktree",
                    "--base-ref", "origin/feature/foo",
                ])

        self.assertIs(captured.get("isolated_worktree"), False)
        self.assertEqual(captured.get("base_ref"), "origin/feature/foo")


class TestAssembleContextEnrichment(unittest.TestCase):
    """_assemble_context must inject skill body + intelligence, not just a role label."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def _make_lane(self) -> TmuxInteractiveDispatch:
        return TmuxInteractiveDispatch(
            self.state_dir,
            project_root=self.state_dir,
        )

    def test_assemble_context_contains_skill_body_and_instruction(self):
        """_assemble_context output contains mocked skill body + instruction (not just role label)."""
        lane = self._make_lane()
        fake_enriched = (
            "## Base Worker Context\n\nYou are backend-developer.\n\n"
            "DISPATCH INSTRUCTION:\n\nDo the thing."
        )
        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            return_value=fake_enriched,
        ):
            result = lane._assemble_context(
                role="backend-developer",
                terminal_id="T1",
                dispatch_id="test-dispatch-123",
                instruction="Do the thing.",
            )
        self.assertIn("Base Worker Context", result)
        self.assertIn("Do the thing.", result)
        self.assertNotIn("## Role\n\nYou are operating as a **backend-developer** worker.", result)

    def test_assemble_context_contains_intelligence_when_injected(self):
        """_assemble_context passes intelligence via _inject_skill_context enrichment path."""
        lane = self._make_lane()
        fake_enriched = (
            "## Relevant Intelligence (from past dispatches)\n\n"
            "- **Antipattern**: do not rewrite enrichment from scratch\n\n"
            "---\n\n"
            "## Base Worker Context\n\nYou are backend-developer.\n\n"
            "DISPATCH INSTRUCTION:\n\nDo the thing."
        )
        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            return_value=fake_enriched,
        ):
            result = lane._assemble_context(
                role="backend-developer",
                terminal_id="T1",
                dispatch_id="test-dispatch-123",
                instruction="Do the thing.",
            )
        self.assertIn("Relevant Intelligence", result)
        self.assertIn("Antipattern", result)
        self.assertIn("Do the thing.", result)

    def test_assemble_context_fallback_contains_role_label_and_instruction(self):
        """When _inject_skill_context raises, fallback includes role label + instruction."""
        lane = self._make_lane()
        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            side_effect=ImportError("no module"),
        ):
            result = lane._assemble_context(
                role="backend-developer",
                terminal_id="T1",
                dispatch_id="test-dispatch-123",
                instruction="Do the thing.",
            )
        self.assertIn("backend-developer", result)
        self.assertIn("Do the thing.", result)

    def test_assemble_context_passes_dispatch_id_to_enricher(self):
        """_assemble_context forwards dispatch_id in dispatch_metadata to _inject_skill_context."""
        lane = self._make_lane()
        captured_metadata: list = []

        def capture_inject(terminal_id, instruction, role, dispatch_metadata):
            captured_metadata.append(dispatch_metadata or {})
            return instruction

        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            side_effect=capture_inject,
        ):
            lane._assemble_context(
                role="backend-developer",
                terminal_id="T1",
                dispatch_id="my-dispatch-xyz",
                instruction="Do the thing.",
            )

        self.assertTrue(captured_metadata, "dispatch_metadata must have been forwarded")
        self.assertEqual(captured_metadata[0].get("dispatch_id"), "my-dispatch-xyz")


class TestUnifiedReportEmission(_LaneTestCase):
    """dispatch() must emit a unified_report alongside the receipt (audit parity)."""

    def test_success_dispatch_emits_unified_report(self):
        """Successful dispatch emits a unified_report with correct dispatch_id and provider."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch("governance_emit.emit_unified_report") as mock_emit:
            result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args.kwargs
        self.assertEqual(call_kwargs["dispatch_id"], self.DISPATCH_ID)
        self.assertEqual(call_kwargs["provider"], "claude")

    def test_failed_receipt_dispatch_emits_unified_report(self):
        """Even a failed-receipt dispatch emits a unified_report (audit completeness)."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            receipt_status="failed",
        )
        lane = self._make_lane(fake)

        with patch("governance_emit.emit_unified_report") as mock_emit:
            result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        mock_emit.assert_called_once()

    def test_timeout_dispatch_emits_unified_report(self):
        """Timeout (no receipt collected) also emits a unified_report."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch("governance_emit.emit_unified_report") as mock_emit:
            result = self._fast_dispatch(lane, deadline_seconds=0.15, poll_interval=0.02)

        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)
        mock_emit.assert_called_once()

    def test_report_emit_failure_marks_degraded(self):
        """When unified_report emit fails, success=False with failure_reason=unified_report_emit_failed.

        Regression for the codex-gate finding: a default-lane dispatch must not
        claim success while leaving the audit trail without a linked report.
        """
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch(
            "governance_emit.emit_unified_report",
            side_effect=RuntimeError("simulated disk full"),
        ):
            result = self._fast_dispatch(lane)

        self.assertFalse(result.success, "success must be False when report emit fails")
        self.assertEqual(
            result.failure_reason,
            "unified_report_emit_failed",
            f"expected 'unified_report_emit_failed', got {result.failure_reason!r}",
        )
        # Worker receipt is still present (worker completed OK, only report is missing).
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt.get("status"), "done")


class TestCompletionReceiptReportPath(_LaneTestCase):
    """Completion receipt emitted by the worker must carry report_path (audit linkage)."""

    def test_completion_protocol_receipt_includes_report_path(self):
        """The receipt JSON in the worker footer must include a non-empty report_path.

        Regression for codex-gate: tmux receipts omitted report_path, breaking the
        receipt->report linkage on the now-DEFAULT lane.
        """
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        protocol = lane._build_completion_protocol(self.DISPATCH_ID, "T1")

        m = re.search(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
        self.assertIsNotNone(m, "could not extract bash command from completion protocol")
        argv = shlex.split(m.group(1).strip())
        receipt = json.loads(argv[argv.index("--receipt") + 1])

        self.assertIn("report_path", receipt, "report_path must be present in the receipt payload")
        rp = receipt["report_path"]
        self.assertTrue(rp, "report_path must be non-empty")
        self.assertIn(self.DISPATCH_ID, rp, "report_path must reference the dispatch_id")
        self.assertTrue(rp.endswith(".md"), "report_path must point to the .md unified report")

    def test_completion_protocol_report_path_points_to_unified_reports_dir(self):
        """report_path must be rooted in unified_reports/ relative to data_dir."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        protocol = lane._build_completion_protocol(self.DISPATCH_ID, "T1")

        m = re.search(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
        self.assertIsNotNone(m)
        argv = shlex.split(m.group(1).strip())
        receipt = json.loads(argv[argv.index("--receipt") + 1])

        expected_report_path = str(
            self.state_dir.parent / "unified_reports" / f"{self.DISPATCH_ID}.md"
        )
        self.assertEqual(
            receipt["report_path"],
            expected_report_path,
            "report_path must be the deterministic unified_reports/<dispatch_id>.md path",
        )


# ---------------------------------------------------------------------------
# T1: VNX_SHARED_PREPARE wiring on the tmux lane
# ---------------------------------------------------------------------------

class TestSharedPrepareWiringTmux(unittest.TestCase):
    """_assemble_context wiring: VNX_SHARED_PREPARE=1 delegates to dispatch_prepare.prepare()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def _make_lane(self) -> TmuxInteractiveDispatch:
        return TmuxInteractiveDispatch(
            self.state_dir,
            project_root=self.state_dir,
        )

    def _patched_assemble(self, dispatch_id="tmux-shared-test", **extra_env):
        lane = self._make_lane()
        fake_skill = "## Skill Context\n\nINSTRUCTION"
        fake_perm = "## Permission Profile\n\n---\n\n" + fake_skill

        env = {"VNX_SHARED_PREPARE": "1"}
        env.update(extra_env)

        with patch.dict(os.environ, env):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                return_value=fake_skill,
            ):
                with patch(
                    "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                    return_value=fake_perm,
                ):
                    return lane._assemble_context(
                        role="backend-developer",
                        terminal_id="T1",
                        dispatch_id=dispatch_id,
                        instruction="Do the thing.",
                    )

    def test_shared_prepare_1_contains_permission_preamble(self):
        """VNX_SHARED_PREPARE=1: assembled context contains permission preamble text."""
        result = self._patched_assemble()
        self.assertIn("## Permission Profile", result)

    def test_shared_prepare_1_contains_footer_sentinel(self):
        """VNX_SHARED_PREPARE=1: assembled context contains worker-rules footer sentinel."""
        from dispatch_prepare import _WORKER_RULES_FOOTER_SENTINEL
        result = self._patched_assemble()
        self.assertIn(_WORKER_RULES_FOOTER_SENTINEL, result)

    def test_shared_prepare_1_contains_directive(self):
        """VNX_SHARED_PREPARE=1: assembled context contains report-contract directive."""
        result = self._patched_assemble()
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)

    def test_shared_prepare_1_no_trailer_in_assemble_context(self):
        """VNX_SHARED_PREPARE=1: _assemble_context does NOT include the trailer sentinel.
        The trailer is appended by dispatch() after the completion-protocol."""
        from dispatch_prepare import END_OF_INSTRUCTION_SENTINEL
        result = self._patched_assemble()
        self.assertNotIn(END_OF_INSTRUCTION_SENTINEL, result,
                         "trailer must not be in _assemble_context — it is added by dispatch()")

    def test_shared_prepare_0_default_no_footer_no_trailer(self):
        """VNX_SHARED_PREPARE=0 (default): _assemble_context does not contain footer/trailer."""
        from dispatch_prepare import _WORKER_RULES_FOOTER_SENTINEL, _TRAILER_SENTINEL
        lane = self._make_lane()
        fake_enriched = "## Skill Context\n\nDo the thing."

        with patch.dict(os.environ, {"VNX_SHARED_PREPARE": "0"}):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                return_value=fake_enriched,
            ):
                result = lane._assemble_context(
                    role="backend-developer",
                    terminal_id="T1",
                    dispatch_id="default-path-test",
                    instruction="Do the thing.",
                )

        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertNotIn(_TRAILER_SENTINEL, result)

    def test_shared_prepare_1_smart_context_prepended(self):
        """VNX_SHARED_PREPARE=1: smart_context is prepended before the prepare() body."""
        from dispatch_prepare import _WORKER_RULES_FOOTER_SENTINEL
        lane = self._make_lane()
        fake_skill = "SKILL_BODY"
        fake_perm = "PREAMBLE\n---\n\n" + fake_skill

        with patch.dict(os.environ, {"VNX_SHARED_PREPARE": "1"}):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                return_value=fake_skill,
            ):
                with patch(
                    "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                    return_value=fake_perm,
                ):
                    result = lane._assemble_context(
                        role="backend-developer",
                        terminal_id="T1",
                        dispatch_id="smart-ctx-test",
                        instruction="Do the thing.",
                        smart_context="SMART_CTX_BLOCK",
                    )

        preamble_pos = result.find("PREAMBLE")
        smart_pos = result.find("SMART_CTX_BLOCK")
        self.assertGreaterEqual(smart_pos, 0, "smart_context must appear in result")
        self.assertLess(smart_pos, preamble_pos, "smart_context must precede the prepare() body")

    def test_shared_prepare_fallback_on_prepare_error(self):
        """VNX_SHARED_PREPARE=1: if prepare() raises, falls back to standard enrichment."""
        from dispatch_prepare import _WORKER_RULES_FOOTER_SENTINEL
        lane = self._make_lane()
        fake_enriched = "STANDARD_ENRICHMENT_BODY"

        with patch.dict(os.environ, {"VNX_SHARED_PREPARE": "1"}):
            with patch("dispatch_prepare.prepare", side_effect=RuntimeError("simulated error")):
                with patch(
                    "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                    return_value=fake_enriched,
                ):
                    result = lane._assemble_context(
                        role="backend-developer",
                        terminal_id="T1",
                        dispatch_id="fallback-test",
                        instruction="Do the thing.",
                    )

        # Must have fallen back to standard enrichment (no footer sentinel from prepare())
        self.assertIn("STANDARD_ENRICHMENT_BODY", result)
        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result)


# ---------------------------------------------------------------------------
# T1: Full delivered-body order — VNX_SHARED_PREPARE=1 + dispatch_paths
# ---------------------------------------------------------------------------

class TestFullDeliveredBodyOrder(_LaneTestCase):
    """Full tmux delivered body with VNX_SHARED_PREPARE=1 must have the exact order:
    [prepare body incl. scope-note] + [completion-protocol] + [trailer as final content].
    scope-note must appear EXACTLY ONCE and NOT be duplicated by the lane.
    """

    DISPATCH_ID_FULL = "20260601-full-body-order"

    def _run_shared_dispatch(self, dispatch_paths=None):
        """Run dispatch() with VNX_SHARED_PREPARE=1 and return delivered body string."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID_FULL,
        )
        lane = TmuxInteractiveDispatch(
            self.state_dir,
            runner=fake,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,
        )
        fake_skill = "## Skill Context\n\nINSTRUCTION"
        fake_perm = "## Permission Profile\n\n---\n\n" + fake_skill

        env = {"VNX_SHARED_PREPARE": "1"}
        dispatch_kw = dict(
            role="backend-developer",
            model="sonnet",
            deadline_seconds=5.0,
            poll_interval=0.01,
            warmup_timeout=0.5,
            warmup_poll_interval=0.01,
            isolated_worktree=False,
        )
        if dispatch_paths is not None:
            dispatch_kw["dispatch_paths"] = dispatch_paths

        with patch.dict(os.environ, env):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                return_value=fake_skill,
            ):
                with patch(
                    "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                    return_value=fake_perm,
                ):
                    lane.dispatch("Do the thing.", self.DISPATCH_ID_FULL, **dispatch_kw)

        return "\n".join(fake.pasted)

    def test_full_body_trailer_is_absolute_last(self):
        """VNX_SHARED_PREPARE=1 + dispatch_paths: trailer sentinel is the final non-whitespace content."""
        from dispatch_prepare import END_OF_INSTRUCTION_SENTINEL
        body = self._run_shared_dispatch(dispatch_paths=["scripts/", "tests/"])
        stripped = body.rstrip()
        self.assertTrue(
            stripped.endswith(END_OF_INSTRUCTION_SENTINEL),
            f"trailer must be absolute last non-whitespace; ends with: {stripped[-120:]!r}",
        )

    def test_full_body_scope_note_exactly_once(self):
        """VNX_SHARED_PREPARE=1 + dispatch_paths: scope-note appears exactly once (no duplication)."""
        body = self._run_shared_dispatch(dispatch_paths=["scripts/", "tests/"])
        count = body.count("Edit ONLY within these paths")
        self.assertEqual(count, 1,
                         f"scope-note must appear exactly once; found {count} times")

    def test_full_body_order_scope_before_footer_before_directive_before_protocol_before_trailer(self):
        """VNX_SHARED_PREPARE=1 + dispatch_paths: exact order — scope-note → footer → directive → completion-protocol → trailer."""
        from dispatch_prepare import _WORKER_RULES_FOOTER_SENTINEL, END_OF_INSTRUCTION_SENTINEL
        body = self._run_shared_dispatch(dispatch_paths=["scripts/", "tests/"])

        scope_pos = body.find("Edit ONLY within these paths")
        footer_pos = body.find(_WORKER_RULES_FOOTER_SENTINEL)
        directive_pos = body.find("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->")
        protocol_pos = body.find("Completion Protocol (interactive lane)")
        trailer_pos = body.find(END_OF_INSTRUCTION_SENTINEL)

        self.assertGreaterEqual(scope_pos, 0, "scope-note must be present")
        self.assertGreaterEqual(footer_pos, 0, "worker-rules footer must be present")
        self.assertGreaterEqual(directive_pos, 0, "report-contract directive must be present")
        self.assertGreaterEqual(protocol_pos, 0, "completion-protocol must be present")
        self.assertGreaterEqual(trailer_pos, 0, "trailer sentinel must be present")

        self.assertLess(scope_pos, footer_pos,
                        "scope-note must precede worker-rules footer")
        self.assertLess(footer_pos, directive_pos,
                        "worker-rules footer must precede report-contract directive")
        self.assertLess(directive_pos, protocol_pos,
                        "report-contract directive must precede completion-protocol")
        self.assertLess(protocol_pos, trailer_pos,
                        "completion-protocol must precede trailer sentinel")

    def test_full_body_scope_note_paths_present(self):
        """VNX_SHARED_PREPARE=1: scope-note lists the exact dispatch_paths."""
        body = self._run_shared_dispatch(dispatch_paths=["scripts/", "tests/"])
        self.assertIn("`scripts/`", body)
        self.assertIn("`tests/`", body)

    def test_full_body_no_scope_note_without_paths(self):
        """VNX_SHARED_PREPARE=1 + no dispatch_paths: scope-note block is absent."""
        body = self._run_shared_dispatch(dispatch_paths=None)
        self.assertNotIn("Edit ONLY within these paths", body)

    def test_full_body_trailer_still_last_without_paths(self):
        """VNX_SHARED_PREPARE=1 + no dispatch_paths: trailer is still the absolute last content."""
        from dispatch_prepare import END_OF_INSTRUCTION_SENTINEL
        body = self._run_shared_dispatch(dispatch_paths=None)
        stripped = body.rstrip()
        self.assertTrue(
            stripped.endswith(END_OF_INSTRUCTION_SENTINEL),
            f"trailer must be last even without dispatch_paths; ends with: {stripped[-120:]!r}",
        )


# ---------------------------------------------------------------------------
# T2: VNX_SHARED_GOVERN wiring on the tmux lane
# ---------------------------------------------------------------------------

class TestSharedGovernWiringTmux(_LaneTestCase):
    """_govern_report routing: VNX_SHARED_GOVERN=1 routes through dispatch_govern.govern()."""

    def _run_dispatch_with_govern_flag(self, flag_value: str, *, receipt_status: str = "done"):
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            receipt_status=receipt_status,
        )
        lane = self._make_lane(fake)

        govern_calls = []

        def fake_govern(spec, raw, lane):
            govern_calls.append((spec, raw, lane))
            from dispatch_govern import GovernedOutcome
            return GovernedOutcome(
                report_path=None,
                contract_status="synthesized",
                permission_enforcement="soft",
            )

        with patch.dict(os.environ, {"VNX_SHARED_GOVERN": flag_value}):
            with patch("dispatch_govern.govern", side_effect=fake_govern):
                result = self._fast_dispatch(lane)

        return result, govern_calls

    def test_shared_govern_1_routes_through_govern(self):
        """VNX_SHARED_GOVERN=1: _govern_report calls dispatch_govern.govern()."""
        _, govern_calls = self._run_dispatch_with_govern_flag("1")
        self.assertGreaterEqual(
            len(govern_calls), 1,
            "dispatch_govern.govern() must be called when VNX_SHARED_GOVERN=1",
        )

    def test_shared_govern_1_passes_correct_lane(self):
        """VNX_SHARED_GOVERN=1: govern() is called with lane='tmux_interactive'."""
        _, govern_calls = self._run_dispatch_with_govern_flag("1")
        self.assertTrue(govern_calls, "govern() must have been called")
        _, _, lane_arg = govern_calls[-1]
        self.assertEqual(lane_arg, "tmux_interactive")

    def test_shared_govern_0_always_routes_through_govern(self):
        """VNX_SHARED_GOVERN=0: tmux lane still calls govern() — flag is irrelevant for tmux."""
        _, govern_calls = self._run_dispatch_with_govern_flag("0")
        self.assertGreaterEqual(
            len(govern_calls), 1,
            "dispatch_govern.govern() must be called even when VNX_SHARED_GOVERN=0 (tmux always uses govern)",
        )

    def test_shared_govern_no_legacy_fallback_on_govern_error(self):
        """When govern() is forced to raise (broken mock), there is no legacy emit fallback.

        In production govern() never raises (error fallback is internal). This test
        verifies that the tmux _govern_report does NOT fall back to the old placeholder
        emit path even in exceptional conditions.
        """
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
        )
        lane = self._make_lane(fake)
        mock_emit_calls = []

        def capturing_emit(*args, response_text=None, **kwargs):
            mock_emit_calls.append(response_text or "")
            reports_dir = self.state_dir.parent / "unified_reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = reports_dir / f"{self.DISPATCH_ID}.md"
            path.write_text(response_text or "fallback", encoding="utf-8")
            return path

        with patch("dispatch_govern.govern", side_effect=RuntimeError("govern exploded")):
            with patch("governance_emit.emit_unified_report", side_effect=capturing_emit):
                self._fast_dispatch(lane)

        # The legacy placeholder string must not appear in any emit call.
        forbidden = "Interactive tmux dispatch (lane: tmux_interactive). Status:"
        for body in mock_emit_calls:
            self.assertNotIn(forbidden, body, "Legacy placeholder must never be emitted")

    def test_no_placeholder_in_govern_flag_on_reports(self):
        """VNX_SHARED_GOVERN=1: emitted reports must not contain the legacy placeholder."""
        placeholder = "Interactive tmux dispatch (lane: tmux_interactive). Status:"

        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
        )
        lane = self._make_lane(fake)

        written_bodies = []

        def capturing_emit(*args, body_override=None, **kwargs):
            written_bodies.append(body_override or "")
            reports_dir = self.state_dir.parent / "unified_reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            path = reports_dir / f"{self.DISPATCH_ID}.md"
            path.write_text(body_override or "fallback", encoding="utf-8")
            return path

        with patch.dict(os.environ, {"VNX_SHARED_GOVERN": "1"}):
            with patch("governance_emit.emit_unified_report", side_effect=capturing_emit):
                with patch("dispatch_govern._git_summary",
                           return_value="feat: implement GOVERN with enough detail here to pass summary length check"):
                    with patch("dispatch_govern._git_changes", return_value="scripts/lib/dispatch_govern.py | 5 ++"):
                        self._fast_dispatch(lane)

        for body in written_bodies:
            self.assertNotIn(placeholder, body,
                             f"Legacy placeholder found in governed report body: {body[:300]}")


# ---------------------------------------------------------------------------
# RECEIPT step: F1 lane-synthesized receipt guarantee (VNX_RECEIPT_FALLBACK)
# ---------------------------------------------------------------------------

class TestReceiptFallback(_LaneTestCase):
    """ensure_receipt fires on timeout, is suppressed on normal path and when flag=0."""

    def test_worker_skip_synthesizes_exactly_one_receipt(self):
        """Worker completes but never emits a receipt -> exactly one lane-synthesized receipt."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_RECEIPT_FALLBACK": "1"}):
            with patch("dispatch_govern._git_summary", return_value="timeout synthesis"):
                with patch("dispatch_govern._git_changes", return_value="No diff"):
                    self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        self.assertTrue(self.receipts_file.exists(), "receipts_file must exist after timeout + fallback")
        lines = [ln for ln in self.receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        synthesized = [json.loads(ln) for ln in lines
                       if json.loads(ln).get("source") == "tmux_interactive_lane_synthesized"]
        self.assertEqual(
            len(synthesized), 1,
            f"Expected exactly 1 lane-synthesized receipt, got {len(synthesized)}: {synthesized}",
        )
        rec = synthesized[0]
        self.assertTrue(rec.get("synthesized"), "synthesized field must be True")
        self.assertEqual(rec["dispatch_id"], self.DISPATCH_ID)
        self.assertEqual(rec["failure_reason"], "tmux_receipt_deadline_exceeded")

    def test_worker_skip_synthesized_receipt_has_report_path(self):
        """Lane-synthesized receipt includes a report_path linking to the emitted report."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_RECEIPT_FALLBACK": "1"}):
            with patch("dispatch_govern._git_summary", return_value="no worker receipt scenario"):
                with patch("dispatch_govern._git_changes", return_value="No diff"):
                    self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        lines = [ln for ln in self.receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        synthesized = [json.loads(ln) for ln in lines
                       if json.loads(ln).get("source") == "tmux_interactive_lane_synthesized"]
        self.assertEqual(len(synthesized), 1)
        report_path = synthesized[0].get("report_path")
        self.assertTrue(report_path, "report_path must be present in lane-synthesized receipt")
        self.assertIn(self.DISPATCH_ID, report_path)

    def test_normal_path_no_synthesized_receipt(self):
        """Worker emits its own receipt -> no lane-synthesized receipt appended."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=True,
            receipt_status="done",
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_RECEIPT_FALLBACK": "1"}):
            with patch("dispatch_govern._git_summary",
                       return_value="feat: worker completed with enough chars"):
                with patch("dispatch_govern._git_changes", return_value="scripts/x.py | 5 ++"):
                    result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        if self.receipts_file.exists():
            lines = [ln for ln in self.receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            synthesized = [json.loads(ln) for ln in lines
                           if json.loads(ln).get("source") == "tmux_interactive_lane_synthesized"]
            self.assertEqual(
                len(synthesized), 0,
                f"No synthesized receipt expected when worker emitted its own: {synthesized}",
            )

    def test_fallback_disabled_no_synthesized_receipt(self):
        """VNX_RECEIPT_FALLBACK=0: timeout path must not append a synthesized receipt."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_RECEIPT_FALLBACK": "0"}):
            with patch("dispatch_govern._git_summary", return_value="fallback disabled scenario"):
                with patch("dispatch_govern._git_changes", return_value="No diff"):
                    result = self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        self.assertFalse(result.success)
        self.assertIn("deadline", result.failure_reason)
        if self.receipts_file.exists():
            lines = [ln for ln in self.receipts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            synthesized = [json.loads(ln) for ln in lines
                           if json.loads(ln).get("source") == "tmux_interactive_lane_synthesized"]
            self.assertEqual(
                len(synthesized), 0,
                f"VNX_RECEIPT_FALLBACK=0 must suppress synthesized receipt: {synthesized}",
            )


# ---------------------------------------------------------------------------
# RECEIPT step: dedup — authored > synthesized; late worker wins
# ---------------------------------------------------------------------------

class TestReceiptDedup(_LaneTestCase):
    """_wait_for_receipt applies dedup: authored receipt wins over synthesized."""

    def test_dedup_authored_wins_over_synthesized_when_both_present(self):
        """If both a synthesized and an authored receipt exist, _wait_for_receipt returns the authored one."""
        synthesized_receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "T1",
            "status": "failed",
            "source": "tmux_interactive_lane_synthesized",
            "synthesized": True,
            "timestamp": "2026-06-01T10:00:00Z",
        }
        authored_receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "ephemeral",
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": "2026-06-01T11:00:00Z",
        }
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(synthesized_receipt) + "\n")
            fh.write(json.dumps(authored_receipt) + "\n")

        # Baseline = 0 (fresh dispatch start), both receipts are "new"
        lane = TmuxInteractiveDispatch(
            self.state_dir,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,
        )
        selected = lane._wait_for_receipt(
            self.DISPATCH_ID, deadline_seconds=0.1, poll_interval=0.01,
            completion_statuses=DEFAULT_COMPLETION_STATUSES, baseline_count=0,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.get("source"), "tmux_interactive",
            f"Authored receipt must win; got source={selected.get('source')!r}",
        )
        self.assertFalse(selected.get("synthesized"), "Authored receipt must not have synthesized=True")

    def test_dedup_no_double_count_both_receipts_returns_one(self):
        """With both receipts present, _wait_for_receipt returns exactly one dict (no double-count)."""
        for source, synth, ts in [
            ("tmux_interactive_lane_synthesized", True, "2026-06-01T10:00:00Z"),
            ("tmux_interactive", False, "2026-06-01T11:00:00Z"),
        ]:
            self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
            r = {
                "event_type": "subprocess_completion",
                "dispatch_id": self.DISPATCH_ID,
                "terminal": "T1",
                "status": "done",
                "source": source,
                "synthesized": synth,
                "timestamp": ts,
            }
            with self.receipts_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")

        lane = TmuxInteractiveDispatch(
            self.state_dir,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,
        )
        selected = lane._wait_for_receipt(
            self.DISPATCH_ID, deadline_seconds=0.1, poll_interval=0.01,
            completion_statuses=DEFAULT_COMPLETION_STATUSES, baseline_count=0,
        )

        self.assertIsInstance(selected, dict, "Must return a single dict, not a list")

    def test_dedup_authored_wins_when_synthesized_last_by_position(self):
        """Nontrivial case: authored is written FIRST, synthesized LAST.

        Without dedup, [-1] would pick synthesized. With dedup, authored always wins.
        """
        authored_receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "ephemeral",
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": "2026-06-01T10:00:00Z",
        }
        synthesized_receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "T1",
            "status": "failed",
            "source": "tmux_interactive_lane_synthesized",
            "synthesized": True,
            "timestamp": "2026-06-01T11:00:00Z",  # later timestamp, last by position
        }
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(authored_receipt) + "\n")
            fh.write(json.dumps(synthesized_receipt) + "\n")

        lane = TmuxInteractiveDispatch(
            self.state_dir,
            receipts_file=self.receipts_file,
            project_root=self.state_dir,
        )
        selected = lane._wait_for_receipt(
            self.DISPATCH_ID, deadline_seconds=0.1, poll_interval=0.01,
            completion_statuses=DEFAULT_COMPLETION_STATUSES, baseline_count=0,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(
            selected.get("source"), "tmux_interactive",
            f"Authored receipt must win even when synthesized is last by position; "
            f"got source={selected.get('source')!r}",
        )
        self.assertFalse(
            selected.get("synthesized"),
            "Winner must not have synthesized=True; authored receipt should be selected",
        )

    def test_consumer_path_dedup_authored_wins_exactly_once(self):
        """Consumer code path (headless_orchestrator._OrchestratedReceiptWatcher) deduplicates
        by dispatch_id: emits exactly one LoopEvent for the authored receipt when the ledger
        contains a synthesized receipt first and an authored receipt later for the same dispatch_id.
        """
        import queue
        import threading
        from headless_orchestrator import _OrchestratedReceiptWatcher, LoopEvent

        synthesized = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "T1",
            "status": "failed",
            "source": "tmux_interactive_lane_synthesized",
            "synthesized": True,
            "timestamp": "2026-06-01T08:00:00Z",
        }
        authored = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "terminal": "T1",
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": "2026-06-01T09:00:00Z",
        }
        self.receipts_file.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_file.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(synthesized) + "\n")
            fh.write(json.dumps(authored) + "\n")

        bus: "queue.Queue[LoopEvent]" = queue.Queue()
        shutdown = threading.Event()
        watcher = _OrchestratedReceiptWatcher(
            self.state_dir,
            shutdown_event=shutdown,
            event_bus=bus,
            dry_run=True,
        )
        watcher._refresh_t0_state = lambda state_dir: None
        watcher._file_pos = 0  # read from the beginning

        watcher._check_new_lines()

        self.assertFalse(bus.empty(), "Consumer must emit a LoopEvent for the actionable receipts")
        event = bus.get_nowait()
        self.assertTrue(bus.empty(), "Exactly one LoopEvent expected — not two (no double-count)")
        self.assertIsInstance(event, LoopEvent)
        self.assertEqual(event.reason, "receipt")
        self.assertEqual(
            event.context.get("latest_dispatch_id"), self.DISPATCH_ID,
            "LoopEvent must reference the correct dispatch_id",
        )
        self.assertEqual(
            event.context.get("receipt_count"), 1,
            "Dedup must collapse both receipts to exactly 1 winner per dispatch_id",
        )
        self.assertEqual(
            event.context.get("receipt_status"), "done",
            "Winner must be the authored receipt (status='done'), not the synthesized one (status='failed')",
        )

    def test_shared_dedup_helper_consumer_level(self):
        """dedup_completion_receipts (shared helper) returns exactly one winner: the authored receipt.

        Validates the helper used by both lane and consumer sites.
        """
        from dispatch_govern import dedup_completion_receipts

        authored = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": "2026-06-01T09:00:00Z",  # authored is EARLIER in time
        }
        synthesized = {
            "event_type": "subprocess_completion",
            "dispatch_id": self.DISPATCH_ID,
            "status": "failed",
            "source": "tmux_interactive_lane_synthesized",
            "synthesized": True,
            "timestamp": "2026-06-01T10:00:00Z",  # synthesized is NEWER timestamp
        }

        # Synthesized written first (earlier position), authored second: authored wins by preference
        winner_synth_first = dedup_completion_receipts([synthesized, authored])
        self.assertIsNotNone(winner_synth_first)
        self.assertEqual(
            winner_synth_first.get("source"), "tmux_interactive",
            "Authored must win when synthesized is first in list",
        )
        self.assertFalse(winner_synth_first.get("synthesized"))

        # Authored written first (earlier position), synthesized last: authored STILL wins
        winner_authored_first = dedup_completion_receipts([authored, synthesized])
        self.assertIsNotNone(winner_authored_first)
        self.assertEqual(
            winner_authored_first.get("source"), "tmux_interactive",
            "Authored must win when authored is first in list (synthesized is last by position AND newer)",
        )
        self.assertFalse(winner_authored_first.get("synthesized"))

        # Exactly one winner per dispatch_id — no double processing
        self.assertIs(
            winner_synth_first, winner_authored_first,
            "Both orderings must return the SAME authored receipt object",
        )


# ---------------------------------------------------------------------------
# FIX 1 — v2.1.159 readiness markers + VNX_TMUX_READY_STRICT fail-fast
# ---------------------------------------------------------------------------

class TestReadinessV2(_LaneTestCase):
    """Readiness marker detection for Claude Code v2.1.159 + STRICT fail-fast guard."""

    def test_v2_ready_marker_detected(self):
        """'Claude Code v2.1.159' startup banner is detected as ready (no legacy marker needed)."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            ready_content="Claude Code v2.1.159\n> ",
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)
        self.assertTrue(result.success, result.failure_reason)

    def test_legacy_ready_marker_still_works(self):
        """Legacy 'Welcome to Claude' content is still detected as ready."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            ready_content="Welcome to Claude\n? for shortcuts",
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)
        self.assertTrue(result.success, result.failure_reason)

    def test_strict_ready_timeout_no_paste_failed_receipt(self):
        """STRICT=1 + never-ready pane: no paste sent, failure_reason = interactive_ready_timeout."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            # Content with no known readiness marker.
            ready_content="[no marker here, just a shell prompt $]",
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_READY_STRICT": "1", "VNX_TMUX_PASTE_SETTLE_SECONDS": "0"}):
            result = self._fast_dispatch(lane)

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "interactive_ready_timeout")
        # No paste must have happened.
        self.assertEqual(fake.pasted, [], "instruction must not be pasted when STRICT timeout fires")
        # Session must be torn down.
        self.assertTrue(fake.killed_sessions, "session must be killed on ready_timeout teardown")

    def test_strict_1_ready_timeout_emits_coordination_event(self):
        """STRICT=1 + never-ready: an 'interactive_ready_timeout' coordination event is emitted."""
        from runtime_coordination import get_connection, get_events
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            ready_content="[no marker]",
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_READY_STRICT": "1", "VNX_TMUX_PASTE_SETTLE_SECONDS": "0"}):
            self._fast_dispatch(lane)

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        self.assertIn("interactive_ready_timeout", {e["event_type"] for e in events})

    def test_strict_0_best_effort_proceeds_even_if_not_ready(self):
        """VNX_TMUX_READY_STRICT=0: paste happens even when no readiness marker is seen."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            ready_content="[no marker]",
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_READY_STRICT": "0"}):
            result = self._fast_dispatch(lane)

        # With emit_receipt=True (default), receipt is written on Enter → dispatch succeeds.
        self.assertTrue(result.success, result.failure_reason)
        self.assertNotEqual(fake.pasted, [], "instruction must be pasted when STRICT=0 (best-effort)")


# ---------------------------------------------------------------------------
# FIX 2 — paste-settle + submit-verify + one-retry path
# ---------------------------------------------------------------------------

class TestSubmitVerify(_LaneTestCase):
    """Submit verification: sentinel staged → retry; working state → no retry; timeout → fail."""

    _SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"
    _WORKING = "esc to interrupt"

    def _staged_body(self) -> str:
        """A body string that contains the sentinel so _still_staged() triggers."""
        return f"Do the thing.\n\n{self._SENTINEL}"

    def test_no_retry_when_pane_shows_working_state(self):
        """When capture-pane shows 'esc to interrupt' after first Enter, no second Enter is sent."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=[self._WORKING],
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        # Count Enter keystrokes sent after paste-buffer.
        paste_idx = next(
            (i for i, c in enumerate(fake.commands) if c and c[0] == "paste-buffer"), None
        )
        self.assertIsNotNone(paste_idx)
        post_paste_enters = [
            c for c in fake.commands[paste_idx + 1:]
            if c and c[0] == "send-keys" and c[-1] == "Enter"
        ]
        self.assertEqual(len(post_paste_enters), 1, "only one Enter expected when working state seen")

    def test_retry_enter_sent_when_sentinel_staged(self):
        """When sentinel is staged after first Enter, exactly one retry Enter is sent."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            # First capture: staged; second: working state (clears staged).
            post_paste_capture_seq=[self._SENTINEL, self._WORKING],
        )
        lane = self._make_lane(fake)
        # Dispatch body must contain the sentinel for _still_staged() to trigger.
        with patch.dict(os.environ, {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.5",
        }):
            result = lane.dispatch(
                self._staged_body(),
                self.DISPATCH_ID,
                role="backend-developer",
                model="sonnet",
                deadline_seconds=5.0,
                poll_interval=0.01,
                warmup_timeout=0.5,
                warmup_poll_interval=0.01,
                isolated_worktree=False,
            )

        self.assertTrue(result.success, result.failure_reason)
        paste_idx = next(
            (i for i, c in enumerate(fake.commands) if c and c[0] == "paste-buffer"), None
        )
        self.assertIsNotNone(paste_idx)
        post_paste_enters = [
            c for c in fake.commands[paste_idx + 1:]
            if c and c[0] == "send-keys" and c[-1] == "Enter"
        ]
        self.assertEqual(
            len(post_paste_enters), 2,
            f"expected 2 Enters (first + one retry); got {len(post_paste_enters)}",
        )

    def test_submit_failed_when_sentinel_staged_past_verify_timeout(self):
        """When sentinel is still staged after verify-timeout, failure_reason=submit_failed."""
        # 50 staged responses is far more than the verify-timeout loop can consume in 0.1s.
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=[self._SENTINEL] * 50,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
        }):
            with patch("dispatch_govern.govern") as mock_govern:
                from dispatch_govern import GovernedOutcome
                mock_govern.return_value = GovernedOutcome(
                    report_path=None,
                    contract_status="synthesized",
                    permission_enforcement="soft",
                )
                result = lane.dispatch(
                    self._staged_body(),
                    self.DISPATCH_ID,
                    role="backend-developer",
                    model="sonnet",
                    deadline_seconds=5.0,
                    poll_interval=0.01,
                    warmup_timeout=0.5,
                    warmup_poll_interval=0.01,
                    isolated_worktree=False,
                )

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, "submit_failed")
        # Session must be killed (no full-deadline wait).
        self.assertTrue(fake.killed_sessions, "session must be killed on submit_failed teardown")

    def test_submit_failed_emits_coordination_event(self):
        """submit_failed path emits an 'interactive_submit_failed' coordination event."""
        from runtime_coordination import get_connection, get_events
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=[self._SENTINEL] * 50,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
        }):
            with patch("dispatch_govern.govern") as mock_govern:
                from dispatch_govern import GovernedOutcome
                mock_govern.return_value = GovernedOutcome(
                    report_path=None,
                    contract_status="synthesized",
                    permission_enforcement="soft",
                )
                lane.dispatch(
                    self._staged_body(),
                    self.DISPATCH_ID,
                    role="backend-developer",
                    model="sonnet",
                    deadline_seconds=5.0,
                    poll_interval=0.01,
                    warmup_timeout=0.5,
                    warmup_poll_interval=0.01,
                    isolated_worktree=False,
                )

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        self.assertIn("interactive_submit_failed", {e["event_type"] for e in events})

    def test_settle_and_retry_timeouts_read_from_env(self):
        """VNX_TMUX_PASTE_SETTLE_SECONDS / SUBMIT_RETRY_DELAY / SUBMIT_VERIFY_TIMEOUT read from env."""
        import time as _time
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
        )
        lane = self._make_lane(fake)

        # Use a body without sentinel so _verify_submit returns True immediately.
        # Settle=0 means no actual sleep; we just verify the dispatch completes.
        with patch.dict(os.environ, {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.05",
        }):
            result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)


# ---------------------------------------------------------------------------
# PR-TMUX-4: Submit-verify signal fix — input-region scoping, no echo false-positive
# ---------------------------------------------------------------------------

class _VerifyRunner:
    """Minimal tmux runner for _verify_submit isolation tests.

    Returns ``capture_seq`` items in order for ``capture-pane`` calls.
    Counts ``send-keys Enter`` calls from within _verify_submit (retry Enters only).
    """

    def __init__(self, capture_seq):
        self._seq = list(capture_seq)
        self.enter_count = 0

    def available(self):
        return True

    def run(self, args, *, timeout=10, input_text=None):
        cmd = args[0] if args else ""
        if cmd == "capture-pane":
            content = self._seq.pop(0) if self._seq else "? for shortcuts\n> "
            return TmuxResult(0, content)
        if cmd == "send-keys" and args and args[-1] == "Enter":
            self.enter_count += 1
        return TmuxResult(0)


class TestSubmitVerifySignal(unittest.TestCase):
    """_verify_submit uses working-state and input-region signals, not sentinel-anywhere.

    Regression for two codex-gate findings:
    1. Echo false-positive: sentinel appearing in scrollback (Claude's echo after real
       submit) was treated as "still staged" when no working marker was visible yet.
    2. Legacy default path (no sentinel in body): no verification at all — new code
       detects staged paste via [Pasted text annotation and body fingerprint.
    """

    SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"
    WORKING = "esc to interrupt"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)

    def _lane(self, runner):
        return TmuxInteractiveDispatch(
            self.state_dir,
            runner=runner,
            project_root=self.state_dir,
        )

    def _verify(self, runner, body, *, timeout="0.05", retry_delay="0"):
        env = {
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": timeout,
            "VNX_TMUX_SUBMIT_RETRY_DELAY": retry_delay,
        }
        with patch.dict(os.environ, env):
            return self._lane(runner)._verify_submit("pane-0", body)

    def _scrollback_echoed_pane(self):
        """Pane where sentinel appears in scrollback (upper 2 lines), bottom 10 are idle.

        Simulates what Claude's TUI shows after a successful submit: the body (including
        sentinel) is echoed into the conversation history while the input line is cleared.
        """
        upper = [self.SENTINEL, "scrollback: assistant starting..."]
        bottom = ["? for shortcuts"] * 9 + ["> "]
        return "\n".join(upper + bottom)

    def test_echoed_sentinel_no_working_marker_is_not_staged(self):
        """Sentinel in scrollback (upper lines), no working marker → NOT staged.

        Core regression test: old code returned True (still staged) here because it
        checked sentinel-anywhere in the pane without scoping to the input region.
        """
        body = f"## Context\n\nInstruction.\n\n{self.SENTINEL}\n"
        runner = _VerifyRunner([self._scrollback_echoed_pane()])
        result = self._verify(runner, body)

        self.assertTrue(result, "sentinel in scrollback must NOT be treated as staged")

    def test_echoed_sentinel_no_retry_enter(self):
        """Sentinel only in scrollback → no retry Enter sent from _verify_submit."""
        body = f"## Context\n\nInstruction.\n\n{self.SENTINEL}\n"
        runner = _VerifyRunner([self._scrollback_echoed_pane()])
        self._verify(runner, body)

        self.assertEqual(
            runner.enter_count, 0,
            "no retry Enter must be sent when sentinel is only in the scrollback",
        )

    def test_sentinel_in_input_region_is_staged(self):
        """Sentinel in bottom 10 lines (still in input buffer) → still staged."""
        body = f"## Context\n\nInstruction.\n\n{self.SENTINEL}\n"
        # Sentinel appears in the last 2 lines (input region).
        staged_pane = f"scrollback content\nmore content\n{self.SENTINEL}\n> "
        runner = _VerifyRunner([staged_pane] * 20)
        result = self._verify(runner, body, timeout="0.05")

        self.assertFalse(result, "sentinel in input region must be treated as staged")

    def test_bracketed_paste_marker_in_input_region_is_staged(self):
        """[Pasted text in bottom lines → staged. Works on legacy path without sentinel in body."""
        body = "## Context\n\nInstruction without sentinel"  # legacy path — no sentinel
        staged_pane = "[Pasted text (2048 chars)]\nsome instruction content here..."
        runner = _VerifyRunner([staged_pane] * 20)
        result = self._verify(runner, body, timeout="0.05")

        self.assertFalse(result, "[Pasted text in input region must be detected as staged")

    def test_legacy_path_working_state_is_submitted(self):
        """No sentinel in body + working marker → submitted (legacy path works correctly)."""
        body = "## Context\n\nInstruction without sentinel"
        runner = _VerifyRunner([f"some content\n{self.WORKING}\n> "])
        result = self._verify(runner, body)

        self.assertTrue(result, "working marker must cause submitted result on legacy path")

    def test_working_marker_overrides_staged_signals(self):
        """Working marker anywhere overrides bracketed-paste and sentinel staged signals."""
        body = f"## Context\n\nInstruction.\n\n{self.SENTINEL}\n"
        pane = f"[Pasted text (100 chars)]\n{self.SENTINEL}\n{self.WORKING}\n> "
        runner = _VerifyRunner([pane])
        result = self._verify(runner, body)

        self.assertTrue(result, "working marker must override staged signals")
        self.assertEqual(runner.enter_count, 0, "no retry Enter when working marker present")


class TestSubmitVerifyEchoIntegration(_LaneTestCase):
    """Integration: echoed sentinel in scrollback must not cause submit_failed or double-Enter."""

    SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"

    def _echoed_pane(self):
        """Sentinel in scrollback (upper 2 lines), bottom 10 lines are idle prompt."""
        upper = [self.SENTINEL, "scrollback: assistant starting..."]
        bottom = ["? for shortcuts"] * 9 + ["> "]
        return "\n".join(upper + bottom)

    def test_echoed_sentinel_dispatch_succeeds(self):
        """Full dispatch: pane shows echoed sentinel in scrollback → success, not submit_failed.

        Regression: old _still_staged() saw sentinel-anywhere and returned True, causing
        spurious retry Enter and eventually submit_failed on a real submit.
        """
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=[self._echoed_pane()],
        )
        lane = self._make_lane(fake)
        result = self._fast_dispatch(lane)

        self.assertTrue(
            result.success,
            f"echoed sentinel in scrollback must not cause submit_failed: {result.failure_reason}",
        )
        self.assertNotEqual(result.failure_reason, "submit_failed")

    def test_echoed_sentinel_no_spurious_retry_enter(self):
        """Full dispatch: echoed sentinel → exactly 1 Enter after paste-buffer (no spurious retry)."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=[self._echoed_pane()],
        )
        lane = self._make_lane(fake)
        self._fast_dispatch(lane)

        paste_idx = next(
            (i for i, c in enumerate(fake.commands) if c and c[0] == "paste-buffer"), None
        )
        self.assertIsNotNone(paste_idx, "paste-buffer must have been called")
        post_paste_enters = [
            c for c in fake.commands[paste_idx + 1:]
            if c and c[0] == "send-keys" and c[-1] == "Enter"
        ]
        self.assertEqual(
            len(post_paste_enters), 1,
            f"spurious retry Enter detected: {len(post_paste_enters)} Enters after paste-buffer "
            "(expected 1 = initial only, no echo false-positive retry)",
        )

    def test_legacy_path_staged_paste_causes_submit_failed(self):
        """Legacy path (no sentinel in body): [Pasted text in pane → submit_failed.

        Regression: old code (sentinel_in_body=False) always returned False from
        _still_staged(), providing zero verification on the default path. New code
        detects [Pasted text staging annotation in the input region.
        """
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            post_paste_capture_seq=["[Pasted text (4096 chars)]\ninstruction content..."] * 50,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
        }):
            with patch("dispatch_govern.govern") as mock_govern:
                from dispatch_govern import GovernedOutcome
                mock_govern.return_value = GovernedOutcome(
                    report_path=None,
                    contract_status="synthesized",
                    permission_enforcement="soft",
                )
                result = lane.dispatch(
                    "Do the thing.",
                    self.DISPATCH_ID,
                    role="backend-developer",
                    model="sonnet",
                    deadline_seconds=5.0,
                    poll_interval=0.01,
                    warmup_timeout=0.5,
                    warmup_poll_interval=0.01,
                    isolated_worktree=False,
                )

        self.assertFalse(result.success)
        self.assertEqual(
            result.failure_reason, "submit_failed",
            f"legacy staged paste must cause submit_failed, got: {result.failure_reason!r}",
        )


# ---------------------------------------------------------------------------
# CAPTURE step: pipe-pane wiring + normalizer close-out
# ---------------------------------------------------------------------------

class TestCapturePipePaneWiring(_LaneTestCase):
    """pipe-pane is wired at spawn when VNX_TMUX_CAPTURE=1 (default), not when =0."""

    def test_pipe_pane_wired_when_capture_enabled(self):
        """VNX_TMUX_CAPTURE=1: pipe-pane command recorded after new-session."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            self._fast_dispatch(lane)

        pipe_cmds = [c for c in fake.commands if c and c[0] == "pipe-pane"]
        self.assertTrue(pipe_cmds, "pipe-pane must be called when VNX_TMUX_CAPTURE=1")

    def test_pipe_pane_path_contains_dispatch_id(self):
        """pipe-pane shell command embeds the dispatch_id in the log path."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            self._fast_dispatch(lane)

        pipe_cmds = [c for c in fake.commands if c and c[0] == "pipe-pane"]
        self.assertTrue(pipe_cmds)
        # The shell-command argument (last element) must contain the dispatch_id.
        shell_arg = pipe_cmds[0][-1]
        self.assertIn(self.DISPATCH_ID, shell_arg, "pipe-pane shell command must reference dispatch_id in log path")

    def test_pipe_pane_wired_before_launch(self):
        """pipe-pane must appear in command sequence AFTER new-session and BEFORE send-keys -l."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            self._fast_dispatch(lane)

        cmds = fake.commands
        spawn_idx = next((i for i, c in enumerate(cmds) if c and c[0] == "new-session"), None)
        pipe_idx = next((i for i, c in enumerate(cmds) if c and c[0] == "pipe-pane"), None)
        launch_idx = next((i for i, c in enumerate(cmds) if c and c[0] == "send-keys" and "-l" in c), None)

        self.assertIsNotNone(spawn_idx, "new-session must be called")
        self.assertIsNotNone(pipe_idx, "pipe-pane must be called")
        self.assertIsNotNone(launch_idx, "send-keys -l (launch) must be called")
        self.assertGreater(pipe_idx, spawn_idx, "pipe-pane must come after new-session")
        self.assertLess(pipe_idx, launch_idx, "pipe-pane must come before launch send-keys")

    def test_pipe_pane_not_wired_when_capture_disabled(self):
        """VNX_TMUX_CAPTURE=0: pipe-pane is never called."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "0"}):
            self._fast_dispatch(lane)

        pipe_cmds = [c for c in fake.commands if c and c[0] == "pipe-pane"]
        self.assertEqual(pipe_cmds, [], "pipe-pane must NOT be called when VNX_TMUX_CAPTURE=0")

    def test_pipe_pane_flag_off_variants(self):
        """VNX_TMUX_CAPTURE=false/no/off also disable pipe-pane."""
        for flag_val in ("false", "no", "off"):
            with self.subTest(flag=flag_val):
                did = f"{self.DISPATCH_ID}-{flag_val}"
                rf = self.state_dir / f"receipts-{flag_val}.ndjson"
                fake = FakeTmux(receipts_file=rf, dispatch_id=did)
                lane = TmuxInteractiveDispatch(
                    self.state_dir,
                    runner=fake,
                    receipts_file=rf,
                    project_root=self.state_dir,
                )
                with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": flag_val}):
                    lane.dispatch(
                        "Do the thing.", did,
                        model="sonnet",
                        deadline_seconds=5.0,
                        poll_interval=0.01,
                        warmup_timeout=0.5,
                        warmup_poll_interval=0.01,
                        isolated_worktree=False,
                    )
                pipe_cmds = [c for c in fake.commands if c and c[0] == "pipe-pane"]
                self.assertEqual(pipe_cmds, [], f"pipe-pane must not be called with VNX_TMUX_CAPTURE={flag_val}")

    def test_path_traversal_dispatch_id_does_not_write_outside_log_dir(self):
        """A dispatch_id with '../' must not allow pipe-pane to write outside the log dir.

        The sanitizer must reject the unsafe id (capture skipped) so pipe-pane is
        never called with a path that escapes .vnx-data/logs/conversations.
        """
        traversal_ids = [
            "../escape",
            "../../etc/passwd",
            "foo/../bar",
            "valid-prefix/../escape",
            "/absolute/path",
            "id with spaces",
            "id;rm -rf /",
        ]
        for bad_id in traversal_ids:
            with self.subTest(dispatch_id=bad_id):
                rf = self.state_dir / f"receipts-traversal-{hash(bad_id) & 0xFFFF}.ndjson"
                fake = FakeTmux(receipts_file=rf, dispatch_id=self.DISPATCH_ID)
                lane = TmuxInteractiveDispatch(
                    self.state_dir,
                    runner=fake,
                    receipts_file=rf,
                    project_root=self.state_dir,
                )
                with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
                    # Directly call _start_pipe_pane with the malicious id.
                    result = lane._start_pipe_pane("fake-pane", bad_id)

                # Capture must be skipped — no path returned.
                self.assertIsNone(
                    result,
                    f"unsafe dispatch_id {bad_id!r} must cause capture to be skipped (returned {result})",
                )
                # pipe-pane must NOT have been called with a path outside the log dir.
                pipe_cmds = [c for c in fake.commands if c and c[0] == "pipe-pane"]
                for cmd in pipe_cmds:
                    shell_arg = cmd[-1] if cmd else ""
                    log_dir = (self.state_dir.parent / "logs" / "conversations").resolve()
                    # Any path in the shell command must be a child of log_dir.
                    for token in shlex.split(shell_arg):
                        p = Path(token)
                        if p.is_absolute():
                            try:
                                p.relative_to(log_dir)
                            except ValueError:
                                self.fail(
                                    f"pipe-pane shell arg {shell_arg!r} references path outside log_dir: {token}"
                                )


class TestCaptureNormalizerCloseout(_LaneTestCase):
    """Normalizer is called once at close-out; normalizer errors do not fail dispatch."""

    def test_normalizer_called_at_closeout_on_success(self):
        """On successful dispatch, _run_capture_normalizer is called once."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        normalizer_calls = []

        def capturing_normalizer(raw_log, terminal_id, dispatch_id, model):
            normalizer_calls.append((raw_log, terminal_id, dispatch_id, model))

        lane._run_capture_normalizer = capturing_normalizer

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        self.assertEqual(
            len(normalizer_calls), 1,
            f"normalizer must be called exactly once on success; called {len(normalizer_calls)} times",
        )

    def test_normalizer_called_at_closeout_on_timeout(self):
        """On timeout, _run_capture_normalizer is still called once (best-effort audit)."""
        fake = FakeTmux(
            receipts_file=self.receipts_file,
            dispatch_id=self.DISPATCH_ID,
            emit_receipt=False,
        )
        lane = self._make_lane(fake)
        normalizer_calls = []

        def capturing_normalizer(raw_log, terminal_id, dispatch_id, model):
            normalizer_calls.append(dispatch_id)

        lane._run_capture_normalizer = capturing_normalizer

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            result = self._fast_dispatch(lane, deadline_seconds=0.2, poll_interval=0.02)

        self.assertFalse(result.success)
        self.assertEqual(
            len(normalizer_calls), 1,
            f"normalizer must be called once on timeout; called {len(normalizer_calls)} times",
        )

    def test_normalizer_error_does_not_fail_dispatch(self):
        """A normalizer exception must not change dispatch success state."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        def raising_normalizer(raw_log, terminal_id, dispatch_id, model):
            raise RuntimeError("simulated normalizer failure")

        lane._run_capture_normalizer = raising_normalizer

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "1"}):
            with patch("dispatch_govern.govern") as mock_govern:
                from dispatch_govern import GovernedOutcome
                mock_govern.return_value = GovernedOutcome(
                    report_path=self.state_dir.parent / "unified_reports" / f"{self.DISPATCH_ID}.md",
                    contract_status="authored",
                    permission_enforcement="soft",
                )
                # Ensure the report file exists so govern does not degrade
                report_dir = self.state_dir.parent / "unified_reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                (report_dir / f"{self.DISPATCH_ID}.md").write_text("report")
                result = self._fast_dispatch(lane)

        # The raising normalizer must not crash the lane.
        self.assertNotEqual(result.failure_reason, "unexpected_error",
                            "normalizer exception must not propagate as unexpected_error")

    def test_normalizer_not_called_when_capture_disabled(self):
        """VNX_TMUX_CAPTURE=0: normalizer is never called (pipe-pane not started)."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        normalizer_calls = []

        def capturing_normalizer(raw_log, terminal_id, dispatch_id, model):
            normalizer_calls.append(dispatch_id)

        lane._run_capture_normalizer = capturing_normalizer

        with patch.dict(os.environ, {"VNX_TMUX_CAPTURE": "0"}):
            self._fast_dispatch(lane)

        self.assertEqual(
            normalizer_calls, [],
            "normalizer must not be called when VNX_TMUX_CAPTURE=0 (no raw_log)",
        )


if __name__ == "__main__":
    unittest.main()
