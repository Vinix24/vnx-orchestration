"""Tests for the deliberation-panelist role registration (OI-1359).

``scripts/panel.py`` seats every deliberation-panel stage (diverge, contrarian,
verify, synthesis) with role="deliberation-panelist" (OI-811). The fabric
already excludes the name by law in two places — ``phantom_guard.REVIEW_ROLES``
(a panel seat produces analysis, not a diff, so an empty worktree-diff is
expected and never evidence of fabrication) and
``dispatch_govern._FREEFORM_ROLES`` (a seat's report is free-form panel
analysis, not a ``## Changes`` / ``## Verification`` report) — but neither
register is the one ``resolve_worker_profile`` checks. Since #1563
(OI-1069 pt.5) that resolver refuses fail-closed on a role present in NO
register, and "deliberation-panelist" was in none: every seat would die at
spawn. These tests pin the registration, mirroring test_plan_reviewer_role.py
for the identical precedent (plan-reviewer is also a panel seat role, not
T0-dispatched, registered for the same reason):

  1. ``resolve_worker_profile("deliberation-panelist")`` resolves to a REAL
     profile (is_fallback=False) against the repo YAML — not the code-worker
     fallback;
  2. the profile's file_write_scope admits the unified-reports drop and
     nothing else in the repo (read-only-plus-one-report posture);
  3. the profile permits no code mutation (Edit/MultiEdit denied, no git
     add/commit/push);
  4. a role that exists in NO register still raises UnknownRoleError — the
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
    """Resolve deliberation-panelist from the worktree-local YAML (bypasses env steering)."""
    return resolve_worker_profile("deliberation-panelist", REPO_PERMISSIONS_YAML)


class TestDeliberationPanelistResolves:
    def test_resolves_to_a_real_profile_not_fallback(self) -> None:
        profile = _profile()
        assert profile.is_fallback is False
        assert profile.role == "deliberation-panelist"

    def test_registered_in_agents_registry(self) -> None:
        """The agents/ registry is the role SSOT — agents/deliberation-panelist/CLAUDE.md
        must exist so the role is in BOTH registers."""
        assert (REPO_ROOT / "agents" / "deliberation-panelist" / "CLAUDE.md").is_file()


class TestDeliberationPanelistDeniesCodeMutation:
    """A panel seat produces analysis, never a diff — the profile must not
    permit the code-mutation surface a build role gets."""

    def test_edit_and_multiedit_denied(self) -> None:
        profile = _profile()
        assert "Edit" in profile.denied_tools
        assert "MultiEdit" in profile.denied_tools

    def test_web_tools_denied(self) -> None:
        profile = _profile()
        assert "WebSearch" in profile.denied_tools
        assert "WebFetch" in profile.denied_tools

    def test_git_history_mutation_denied(self) -> None:
        profile = _profile()
        deny_set = set(profile.bash_deny_patterns)
        assert {"git add*", "git commit*", "git push*"} <= deny_set
        for pat in profile.bash_allow_patterns:
            assert not pat.startswith(("git add", "git commit", "git push")), pat


class TestDeliberationPanelistWriteScope:
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
            "scripts/lib/deliberation_panel.py",
            "scripts/panel.py",
            ".vnx/worker_permissions.yaml",
            "tests/test_deliberation_panelist_role.py",
            "agents/deliberation-panelist/CLAUDE.md",
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
