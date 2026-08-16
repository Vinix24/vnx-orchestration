"""Tests for the plan-reviewer role registration (dispatch 20260816-plan-reviewer-role-register).

The plan-gate panel (scripts/lib/plan_gate_panel.py) seats its claude/tmux lanes
with ``--role plan-reviewer``. Since #1563 (OI-1069 pt.5) ``resolve_worker_profile``
refuses fail-closed on a role present in NO register — and ``plan-reviewer`` was in
none, so every opus seat died at spawn and the panel fell over on liveness quorum.
These tests pin the registration:

  1. ``resolve_worker_profile("plan-reviewer")`` resolves to a REAL profile
     (is_fallback=False) against the repo YAML — not the code-worker fallback;
  2. the profile's file_write_scope admits the unified-reports drop and nothing
     else in the repo (read-only-plus-one-report posture);
  3. a role that exists in NO register still raises UnknownRoleError — the
     #1563 fail-closed guarantee must not be weakened by this registration.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PERMISSIONS_YAML = REPO_ROOT / ".vnx" / "worker_permissions.yaml"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from worker_permissions import (
    UnknownRoleError,
    match_file_write_scope,
    resolve_worker_profile,
)

pytestmark = pytest.mark.skipif(
    not REPO_PERMISSIONS_YAML.exists(),
    reason="repo .vnx/worker_permissions.yaml not present in this checkout",
)


def _profile():
    """Resolve plan-reviewer from the worktree-local YAML (bypasses env steering)."""
    return resolve_worker_profile("plan-reviewer", REPO_PERMISSIONS_YAML)


class TestPlanReviewerResolves:
    def test_resolves_to_a_real_profile_not_fallback(self) -> None:
        profile = _profile()
        assert profile.is_fallback is False
        assert profile.role == "plan-reviewer"

    def test_registered_in_agents_registry(self) -> None:
        """The agents/ registry is the role SSOT — agents/plan-reviewer/CLAUDE.md
        must exist so the role is in BOTH registers."""
        assert (REPO_ROOT / "agents" / "plan-reviewer" / "CLAUDE.md").is_file()

    def test_profile_is_read_only_plus_report(self) -> None:
        """Read posture like the other read roles: no Edit/MultiEdit, no git
        history mutation, no code push — the verdict report is the only write."""
        profile = _profile()
        assert "Edit" in profile.denied_tools
        assert "MultiEdit" in profile.denied_tools
        assert "WebSearch" in profile.denied_tools
        assert "WebFetch" in profile.denied_tools
        deny_set = set(profile.bash_deny_patterns)
        assert {"git add*", "git commit*", "git push*"} <= deny_set
        for pat in profile.bash_allow_patterns:
            assert not pat.startswith(("git add", "git commit", "git push")), pat


class TestPlanReviewerWriteScope:
    def test_unified_reports_drop_in_scope(self) -> None:
        profile = _profile()
        assert match_file_write_scope(
            ".vnx-data/unified_reports/some-dispatch.md", profile, None
        ) is True
        assert match_file_write_scope(
            ".vnx-data/unified_reports/nested/deep/report.md", profile, None
        ) is True

    def test_arbitrary_repo_paths_out_of_scope(self) -> None:
        profile = _profile()
        for path in (
            "scripts/lib/plan_gate_panel.py",
            ".vnx/worker_permissions.yaml",
            "tests/test_plan_reviewer_role.py",
            "agents/plan-reviewer/CLAUDE.md",
            "docs/core/DISPATCH_RULES.md",
            "VERSION",
        ):
            assert match_file_write_scope(path, profile, None) is False, path


class TestFailClosedGuaranteeIntact:
    def test_role_in_no_register_still_raises(self) -> None:
        """#1563 (OI-1069 pt.5): a role absent from BOTH worker_permissions.yaml
        and the agents/ registry must still refuse loudly. This name is
        guaranteed to exist nowhere."""
        with pytest.raises(UnknownRoleError):
            resolve_worker_profile(
                "no-such-role-exists-in-any-register-zzz", REPO_PERMISSIONS_YAML
            )

    def test_unknown_role_error_names_the_role(self) -> None:
        with pytest.raises(
            UnknownRoleError, match="no-such-role-exists-in-any-register-zzz"
        ):
            resolve_worker_profile(
                "no-such-role-exists-in-any-register-zzz", REPO_PERMISSIONS_YAML
            )
