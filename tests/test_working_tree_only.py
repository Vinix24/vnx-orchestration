"""Tests for D2.2 — working-tree-only enforcement.

Two layers:
  - the SLOT: build_claude_scope_args / _default_launch_command emit a
    `--disallowedTools Bash(git commit...)/Bash(git push...)` deny when
    working_tree_only=True (the commit/push deny binds at the tool-permission
    layer, not just the instruction preamble). This only binds in the scoped
    posture (VNX_WORKER_SCOPED=1) since the blanket default carries no
    allow/deny lists at all.
  - the SCOPING PRECONDITION (fail-closed): TmuxInteractiveDispatch.dispatch
    rejects a working_tree_only dispatch on any unscoped path (attached, the
    blanket default with VNX_WORKER_SCOPED unset, or explicit
    VNX_WORKER_SCOPED=0) where the deny would not bind. Only an explicit
    VNX_WORKER_SCOPED=1 satisfies the precondition.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import worker_permissions as wp  # noqa: E402
import tmux_interactive_dispatch as tid  # noqa: E402


class _StubRunner:
    """Minimal runner — the scoping precondition returns before any tmux call."""

    def available(self) -> bool:
        return True


def _lane(tmp_path: Path) -> "tid.TmuxInteractiveDispatch":
    return tid.TmuxInteractiveDispatch(
        tmp_path,
        runner=_StubRunner(),
        project_root=tmp_path,
        receipts_file=tmp_path / "receipts.ndjson",
    )


# ── The slot: scope-args / launch-command git-deny ────────────────────────────

class TestGitDenySlot:
    def test_scope_args_add_git_deny_when_working_tree_only(self):
        prof = wp.resolve_worker_profile(None)
        args = wp.build_claude_scope_args(prof, working_tree_only=True)
        joined = " ".join(args)
        assert "--disallowedTools" in args
        assert "Bash(git push:*)" in joined
        assert "Bash(git commit:*)" in joined
        assert "Bash(git push)" in joined
        assert "Bash(git commit)" in joined

    def test_scope_args_no_git_deny_by_default(self):
        prof = wp.resolve_worker_profile(None)
        joined = " ".join(wp.build_claude_scope_args(prof, working_tree_only=False))
        assert "git push" not in joined
        assert "git commit" not in joined

    def test_launch_command_includes_git_deny(self, monkeypatch):
        # The git-commit/push deny only binds in the scoped posture (it rides on
        # --disallowedTools inside build_claude_scope_args); the blanket default
        # ignores working_tree_only entirely, so opt into scoping explicitly.
        monkeypatch.setenv("VNX_WORKER_SCOPED", "1")
        cmd = tid._default_launch_command(
            "sonnet", skip_permissions=True, working_tree_only=True
        )
        assert "git push" in cmd
        assert "git commit" in cmd

    def test_launch_command_no_git_deny_by_default(self):
        cmd = tid._default_launch_command(
            "sonnet", skip_permissions=True, working_tree_only=False
        )
        assert "git push" not in cmd
        assert "git commit" not in cmd


# ── The fail-closed scoping precondition ──────────────────────────────────────

class TestScopingPrecondition:
    def test_attached_working_tree_only_is_rejected(self, tmp_path):
        # attach=True -> skip_permissions=False -> the deny would not bind -> reject.
        lane = _lane(tmp_path)
        result = lane.dispatch(
            "noop", "wt-attach", working_tree_only=True, attach=True,
        )
        assert result.success is False
        assert "working_tree_only" in (result.failure_reason or "")

    def test_unscoped_env_working_tree_only_is_rejected(self, tmp_path, monkeypatch):
        # Detached but VNX_WORKER_SCOPED=0 (explicit-off, same posture as the
        # default) -> blanket --dangerously-skip-permissions, no scope args ->
        # the deny would not bind -> reject.
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        lane = _lane(tmp_path)
        result = lane.dispatch(
            "noop", "wt-unscoped", working_tree_only=True, skip_permissions=True,
        )
        assert result.success is False
        assert "working_tree_only" in (result.failure_reason or "")

    def test_default_env_working_tree_only_is_rejected(self, tmp_path, monkeypatch):
        # Detached with VNX_WORKER_SCOPED unset -> the new blanket-by-default
        # posture -> no scope args -> the deny would not bind -> reject. Only an
        # explicit VNX_WORKER_SCOPED=1 satisfies the precondition.
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        lane = _lane(tmp_path)
        result = lane.dispatch(
            "noop", "wt-default-unscoped", working_tree_only=True, skip_permissions=True,
        )
        assert result.success is False
        assert "working_tree_only" in (result.failure_reason or "")

    def test_scoped_opt_in_working_tree_only_is_accepted_by_precondition(
        self, tmp_path, monkeypatch
    ):
        # Detached with VNX_WORKER_SCOPED=1 satisfies the precondition (the
        # scoping check itself passes; dispatch may still fail later for other
        # reasons such as the stub runner/tmux plumbing — the precondition
        # message must simply not be the failure reason).
        monkeypatch.setenv("VNX_WORKER_SCOPED", "1")
        lane = _lane(tmp_path)
        result = lane.dispatch(
            "noop", "wt-scoped", working_tree_only=True, skip_permissions=True,
        )
        assert "working_tree_only requires" not in (result.failure_reason or "")

    def test_non_working_tree_only_attached_is_not_rejected_by_precondition(self, tmp_path):
        # A normal attached dispatch must NOT be rejected by the wt-only precondition.
        # (It may fail later for other reasons, but not with the wt-only message.)
        lane = _lane(tmp_path)
        result = lane.dispatch(
            "noop", "normal-attach", working_tree_only=False, attach=True,
        )
        assert "working_tree_only requires" not in (result.failure_reason or "")
