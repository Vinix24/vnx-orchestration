#!/usr/bin/env python3
"""test_a3_tmux_skip_pr.py — A3 regression: the tmux-spawn lane never wired
``skip_pr`` (OI-1115) into ``pr_enforcement.enforce_pr_exists``, so a dispatch
built on an existing dispatch branch (``base_ref="origin/dispatch/<id>"``)
always opened a SECOND, duplicate PR. Measured 26-08 on main 7f93f681:
#1685/#1686/#1687, all three closed by hand.

The envelope lanes (``dispatch_envelope._enforce_push_pr``) already derived
``skip_pr`` from ``base_ref`` via ``_is_dispatch_branch_ref`` and passed it
through. The tmux lane (``TmuxInteractiveDispatch.dispatch`` /
``_enforce_pr_exists``) never computed or passed it at all, so
``enforce_pr_exists``'s default (``skip_pr=False``) always won.

These tests assert on BEHAVIOR, not on the presence of the ``skip_pr`` symbol:
they run the REAL ``pr_enforcement.enforce_pr_exists`` (never mocked) and only
patch the ``gh_pr_ensure`` GitHub boundary, poisoning it with an
``AssertionError`` side-effect for the skip_pr case — the test fails with an
assertion error unless the auto-PR step is never even entered, not merely
"returns nothing".

Also covers OI-1113 parity: the tmux lane never passed ``target_remote_head``
either, so the post-push containment check never ran there.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import init_schema
from tmux_interactive_dispatch import TmuxInteractiveDispatch
from tmux_worktree import ReapResult, WorktreeHandle

# Reuse the FakeTmux worker-completion stub from the main tmux-dispatch test
# module rather than re-implementing it — it is the shared fixture every other
# tmux-lane test in this repo already relies on.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tmux_interactive_dispatch import FakeTmux  # noqa: E402


class _LaneTestCase(unittest.TestCase):
    DISPATCH_ID = "20260826-a3-skippr-test"

    def setUp(self) -> None:
        import tempfile

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


class TestSkipPrWiredFromBaseRef(_LaneTestCase):
    """The forced-red/green pair (dispatch's Deliverable 1)."""

    def test_existing_dispatch_branch_base_ref_suppresses_pr_creation(self):
        """FORCED CONDITION: base_ref names an existing dispatch branch AND the
        worktree is in the ``pushed`` state that today always triggers a PR.

        Behavioral assertion: gh_pr_ensure.ensure_pr (the sole gh-pr-create
        entry point pr_enforcement calls) must NEVER be invoked — not called-
        and-ignored, never entered at all. A test that only checked
        ``result.success`` or grepped for the word ``skip_pr`` would pass on
        the pre-fix code too (the dispatch still 'succeeds', it just also opens
        a duplicate PR) — this asserts on the actual gh-boundary call.
        """
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)

        def _boom(*args, **kwargs):
            raise AssertionError(
                "gh_pr_ensure.ensure_pr must NEVER be called when base_ref names "
                "an existing dispatch branch (origin/dispatch/<id>) — the PR for "
                "that branch already exists; a second PR is the OI-1115 duplicate "
                "this test guards against."
            )

        with patch("tmux_interactive_dispatch.allocate", return_value=handle):
            with patch("tmux_interactive_dispatch.classify", return_value="pushed"):
                with patch(
                    "tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)
                ):
                    with patch("gh_pr_ensure.ensure_pr", side_effect=_boom) as mock_ensure_pr:
                        result = self._fast_dispatch(
                            lane,
                            base_ref="origin/dispatch/20260825-some-prior-dispatch",
                        )

        mock_ensure_pr.assert_not_called()
        self.assertTrue(
            result.success,
            f"dispatch must still succeed (push already done, PR suppressed "
            f"on purpose): {result.failure_reason}",
        )

    def test_normal_base_ref_still_opens_a_pr(self):
        """MIRROR (mandatory per dispatch instructions): base_ref=origin/main
        (a fresh branch, not built on an existing dispatch branch) must still
        open a PR exactly as before. Without this half, a lane that stopped
        opening PRs entirely would pass the test above and go undetected —
        that is a worse defect than the one being fixed."""
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        mock_ensure_pr = MagicMock(
            return_value={"pr_number": 4242, "created": True, "reason": None}
        )

        with patch("tmux_interactive_dispatch.allocate", return_value=handle):
            with patch("tmux_interactive_dispatch.classify", return_value="pushed"):
                with patch(
                    "tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)
                ):
                    with patch("gh_pr_ensure.ensure_pr", mock_ensure_pr):
                        result = self._fast_dispatch(lane, base_ref="origin/main")

        mock_ensure_pr.assert_called_once()
        self.assertTrue(result.success, result.failure_reason)


class TestTargetRemoteHeadWiredToo(_LaneTestCase):
    """OI-1113 parity: the tmux lane now also passes target_remote_head, so a
    containment check can actually run. Verified via the adapter-level call
    (mirrors test_enforcement_receives_wt_path_when_worktree_exists' style for
    wt_path) rather than re-deriving a real force-push scenario end-to-end —
    the containment logic itself is already covered in test_pr_enforcement.py;
    this only pins that the tmux lane forwards the value it captures."""

    def test_enforce_pr_exists_receives_target_remote_head_kwarg(self):
        handle = self._make_handle()
        fake = FakeTmux(receipts_file=self.receipts_file, dispatch_id=self.DISPATCH_ID)
        lane = self._make_lane(fake)
        mock_enforce = MagicMock(
            return_value=__import__("pr_enforcement").PrEnforcementResult(
                applicable=True, ok=True, pr_number=1, created=True,
            )
        )

        with patch("tmux_interactive_dispatch.allocate", return_value=handle):
            with patch("tmux_interactive_dispatch.classify", return_value="pushed"):
                with patch(
                    "tmux_interactive_dispatch.reap", return_value=ReapResult(removed=True)
                ):
                    with patch("pr_enforcement.enforce_pr_exists", mock_enforce):
                        result = self._fast_dispatch(lane, base_ref="origin/main")

        self.assertTrue(result.success)
        mock_enforce.assert_called_once()
        call_kwargs = mock_enforce.call_args.kwargs
        self.assertIn(
            "target_remote_head", call_kwargs,
            "tmux lane must pass target_remote_head through to enforce_pr_exists "
            "(OI-1113 parity) — previously this kwarg was never sent at all",
        )
        self.assertIn("skip_pr", call_kwargs)
        self.assertFalse(
            call_kwargs["skip_pr"],
            "base_ref=origin/main must NOT set skip_pr",
        )


if __name__ == "__main__":
    unittest.main()
