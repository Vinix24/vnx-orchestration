"""Tests for D2.2 — working-tree-only scope args.

The SLOT: ``build_claude_scope_args`` emits a
``--disallowedTools Bash(git commit...)/Bash(git push...)`` deny when
``working_tree_only=True`` (the commit/push deny binds at the tool-permission
layer, not just the instruction preamble). This binds in the scoped posture,
which is the default since the 14-08 flip (the blanket opt-out carries no
allow/deny lists at all).

The tmux lane's launch-command tests and its fail-closed scoping precondition
(``TmuxInteractiveDispatch.dispatch`` rejecting a working_tree_only dispatch on a
full opt-out path) went with that lane on 2026-09-18. Note that nothing in
production passes ``working_tree_only`` to ``build_claude_scope_args`` any more.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import worker_permissions as wp  # noqa: E402


# ── The slot: scope-args git-deny ─────────────────────────────────────────────

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
