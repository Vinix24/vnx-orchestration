"""test_tmux_fail_loud_empty_extraction.py — dispatch 20260711-164747-provider-hardening.

Covers TmuxInteractiveDispatch._fail_loud_on_empty_extraction: the tmux-lane twin of
dispatch_envelope._fail_loud_on_empty_success (tests/test_dispatch_envelope_fail_loud.py).
That guard catches an empty PROVIDER completion at EXECUTE time, before GOVERN. This one
catches an empty WORKER extraction (a self-reported "done" receipt backed by no worktree
diff) at the equivalent point in the tmux lane — BEFORE _govern_report() ever synthesizes
a report from it — so an empty extraction never produces a "done" report/receipt in the
first place, instead of only being corrected after the fact by the govern()-time
phantom_guard backstop.

Two layers:
  1. Unit tests directly on _fail_loud_on_empty_extraction, mocking
     phantom_guard.record_phantom_if_any (the decision + corrective-append is already
     covered by test_phantom_guard.py / test_phantom_guard_inline.py — these tests isolate
     the NEW wiring: does an empty extraction downgrade the receipt, emit an audit event,
     and never raise; does a real extraction pass through unchanged).
  2. End-to-end tests driving the real dispatch() loop (tmux mocked via FakeTmux, phantom
     decision mocked via phantom_guard.record_phantom_if_any) to prove the wiring actually
     changes the dispatch OUTCOME — result.success, result.receipt["status"], and
     result.failure_reason — not just the isolated method's return value.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import get_connection, get_events, init_schema  # noqa: E402
from tmux_interactive_dispatch import TmuxInteractiveDispatch  # noqa: E402
from tmux_worktree import ReapResult, WorktreeHandle  # noqa: E402
import phantom_guard as pg  # noqa: E402

from test_tmux_interactive_dispatch import FakeTmux  # noqa: E402


class _BaseCase(unittest.TestCase):
    DISPATCH_ID = "20260711-failloud-test"

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


# ---------------------------------------------------------------------------
# Layer 1: unit tests on _fail_loud_on_empty_extraction directly
# ---------------------------------------------------------------------------


class TestFailLoudOnEmptyExtractionUnit(_BaseCase):
    def test_none_receipt_passthrough_no_guard_call(self):
        """No receipt (deadline/timeout path) -> returned as-is; guard never invoked."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        with patch("phantom_guard.record_phantom_if_any") as mock_guard:
            result = lane._fail_loud_on_empty_extraction(
                dispatch_id=self.DISPATCH_ID,
                receipt=None,
                role="backend-developer",
                worktree_path=None,
                base_sha=None,
            )
        self.assertIsNone(result)
        mock_guard.assert_not_called()

    def test_empty_extraction_downgrades_receipt_to_failed(self):
        """A phantom verdict must downgrade status to 'failed' and carry the reason —
        this is the "empty -> loud-fail" case the dispatch asked for."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        receipt = {"status": "done", "dispatch_id": self.DISPATCH_ID, "terminal": "ephemeral"}

        with patch(
            "phantom_guard.record_phantom_if_any",
            return_value=pg.PhantomVerdict(True, "PHANTOM: empty extraction, no diff"),
        ) as mock_guard:
            result = lane._fail_loud_on_empty_extraction(
                dispatch_id=self.DISPATCH_ID,
                receipt=receipt,
                role="backend-developer",
                worktree_path=self.state_dir / "wt",
                base_sha="deadbeef",
                pane_tokens={"input": 0, "output": 231, "cache_read": 0},
            )

        mock_guard.assert_called_once()
        call_kwargs = mock_guard.call_args.kwargs
        self.assertEqual(call_kwargs["dispatch_id"], self.DISPATCH_ID)
        self.assertEqual(call_kwargs["status"], "done")
        self.assertEqual(call_kwargs["token_usage"], 231)
        self.assertEqual(call_kwargs["receipts_file"], str(self.receipts_file))
        self.assertEqual(call_kwargs["state_dir"], self.state_dir)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["phantom_rejected"])
        self.assertIn("empty extraction", result["phantom_reason"])
        # original receipt object must not be mutated in place (caller may reuse it)
        self.assertEqual(receipt["status"], "done")

        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        event_types = {e["event_type"] for e in events}
        self.assertIn("interactive_empty_extraction", event_types)

    def test_nonempty_extraction_passes_through_unchanged(self):
        """A non-phantom verdict (real work present) -> receipt returned unchanged —
        the "non-empty -> pass" case the dispatch asked for."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        receipt = {"status": "done", "dispatch_id": self.DISPATCH_ID}

        with patch(
            "phantom_guard.record_phantom_if_any",
            return_value=pg.PhantomVerdict(False, "non-empty worktree diff — work is present"),
        ):
            result = lane._fail_loud_on_empty_extraction(
                dispatch_id=self.DISPATCH_ID,
                receipt=receipt,
                role="backend-developer",
                worktree_path=self.state_dir / "wt",
                base_sha="deadbeef",
            )

        self.assertEqual(result["status"], "done")
        self.assertNotIn("phantom_rejected", result)
        with get_connection(self.state_dir) as conn:
            events = get_events(conn, entity_id=self.DISPATCH_ID)
        event_types = {e["event_type"] for e in events}
        self.assertNotIn("interactive_empty_extraction", event_types)

    def test_review_role_never_downgraded(self):
        """A review role's completion is exempt (phantom_guard's own rule, delegated
        through unchanged) — record_phantom_if_any handles this; verify it still
        propagates a non-phantom verdict correctly for a reviewer receipt."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        receipt = {"status": "done", "dispatch_id": self.DISPATCH_ID}

        with patch(
            "phantom_guard.record_phantom_if_any",
            return_value=pg.PhantomVerdict(False, "review role — a verdict, not a diff"),
        ) as mock_guard:
            result = lane._fail_loud_on_empty_extraction(
                dispatch_id=self.DISPATCH_ID,
                receipt=receipt,
                role="plan-reviewer",
                worktree_path=None,
                base_sha=None,
            )
        self.assertEqual(mock_guard.call_args.kwargs["role"], "plan-reviewer")
        self.assertEqual(result["status"], "done")

    def test_guard_error_is_non_fatal_receipt_passthrough(self):
        """A guard-internal exception must never block a real completion — the receipt
        is returned unchanged, matching the guard's own fail-open/abstain contract."""
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        receipt = {"status": "done", "dispatch_id": self.DISPATCH_ID}

        with patch(
            "phantom_guard.record_phantom_if_any",
            side_effect=RuntimeError("db locked"),
        ):
            result = lane._fail_loud_on_empty_extraction(
                dispatch_id=self.DISPATCH_ID,
                receipt=receipt,
                role="backend-developer",
                worktree_path=self.state_dir / "wt",
                base_sha="deadbeef",
            )
        self.assertEqual(result["status"], "done")


# ---------------------------------------------------------------------------
# Layer 2: end-to-end through dispatch()
# ---------------------------------------------------------------------------


class _WorktreeCase(_BaseCase):
    def _make_handle(self) -> WorktreeHandle:
        wt_path = self.state_dir / "worktrees" / f"dispatch-{self.DISPATCH_ID}"
        wt_path.mkdir(parents=True, exist_ok=True)
        return WorktreeHandle(
            path=wt_path,
            branch=f"dispatch/{self.DISPATCH_ID}",
            base_sha="deadbeef" * 5,
            base_ref="origin/main",
            dispatch_id=self.DISPATCH_ID,
        )

    def _fast_dispatch(self, lane: TmuxInteractiveDispatch, **overrides):
        import os

        kwargs = dict(
            role="backend-developer",
            model="sonnet",
            deadline_seconds=5.0,
            poll_interval=0.01,
            warmup_timeout=0.5,
            warmup_poll_interval=0.01,
            isolated_worktree=True,
        )
        kwargs.update(overrides)
        _env = {
            "VNX_TMUX_PASTE_SETTLE_SECONDS": "0",
            "VNX_TMUX_SUBMIT_RETRY_DELAY": "0",
            "VNX_TMUX_SUBMIT_VERIFY_TIMEOUT": "0.1",
            "VNX_TMUX_WORK_START_TIMEOUT": "0.1",
            "VNX_TMUX_WORK_START_POLL": "0.02",
        }
        with patch.dict(os.environ, _env):
            return lane.dispatch("Do the thing.", self.DISPATCH_ID, **kwargs)


class TestEndToEndEmptyExtractionFailsLoud(_WorktreeCase):
    def test_worker_done_with_empty_extraction_fails_the_dispatch(self):
        """A worker self-reports 'done' but the phantom decision says empty extraction:
        dispatch() must return success=False, receipt status downgraded to 'failed',
        and the failure surfaced in failure_reason — never a silent 'done' outcome."""
        handle = self._make_handle()
        fake = FakeTmux(
            receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID, receipt_status="done"
        )
        lane = self._make_lane(fake)

        with patch("tmux_interactive_dispatch.allocate", return_value=handle), \
             patch("tmux_interactive_dispatch.classify", return_value="clean"), \
             patch("tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)), \
             patch(
                 "phantom_guard.record_phantom_if_any",
                 return_value=pg.PhantomVerdict(
                     True, "PHANTOM: status='done' claims completion but the worktree diff is EMPTY"
                 ),
             ):
            result = self._fast_dispatch(lane)

        self.assertFalse(result.success, "empty extraction must not report success")
        self.assertIsNotNone(result.receipt)
        self.assertEqual(result.receipt["status"], "failed")
        self.assertTrue(result.receipt.get("phantom_rejected"))
        self.assertIn("worker_status: failed", result.failure_reason)

    def test_worker_done_with_real_extraction_still_succeeds(self):
        """Guard rail: a real (non-empty) extraction must not be false-flagged —
        dispatch() proceeds to success exactly as before this change."""
        handle = self._make_handle()
        fake = FakeTmux(
            receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID, receipt_status="done"
        )
        lane = self._make_lane(fake)

        with patch("tmux_interactive_dispatch.allocate", return_value=handle), \
             patch("tmux_interactive_dispatch.classify", return_value="clean"), \
             patch("tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)), \
             patch(
                 "phantom_guard.record_phantom_if_any",
                 return_value=pg.PhantomVerdict(False, "non-empty worktree diff — work is present"),
             ):
            result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        self.assertEqual(result.receipt["status"], "done")
        self.assertNotIn("phantom_rejected", result.receipt)

    def test_unresolvable_worktree_abstains_real_phantom_guard(self):
        """No mocking of phantom_guard at all — the REAL guard runs against a
        WorktreeHandle pointing at a plain (non-git) directory, which
        compute_worktree_diff cannot diff. It must ABSTAIN (never false-reject),
        preserving the pre-existing dispatch outcome for every test/caller that
        does not set up a real git worktree."""
        handle = self._make_handle()  # plain mkdir, not a git repo
        fake = FakeTmux(
            receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID, receipt_status="done"
        )
        lane = self._make_lane(fake)

        with patch("tmux_interactive_dispatch.allocate", return_value=handle), \
             patch("tmux_interactive_dispatch.classify", return_value="clean"), \
             patch("tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)):
            result = self._fast_dispatch(lane)

        self.assertTrue(result.success, result.failure_reason)
        self.assertEqual(result.receipt["status"], "done")


if __name__ == "__main__":
    unittest.main()
