#!/usr/bin/env python3
"""Tests for the fail-closed VNX CI-run check (OI-1216, OI-1387, OI-1613).

Covers the four required states from the original dispatch, each with a
stubbed ``gh`` outcome (no network):
  (a) successful run for the exact head SHA            -> GO
  (b) zero runs                                        -> NO-GO
  (c) run with conclusion=failure for the exact head   -> NO-GO
  (d) success but for a run that (defensively) doesn't  -> NO-GO
      match head_sha even though --commit was used
plus the fail-closed edges the dispatch names: gh missing, gh not
authenticated, and a still-running (in_progress) run.

OI-1387 adds: the query must be scoped to the exact commit (``--commit``, not
``--branch``). See ``TestCommitScopedQuery`` below.

OI-1613 adds: OI-1387's "every run on this commit must succeed" made a run
history immutable — a sha that failed once and was later re-verified via a
reopen-to-retrigger (unchanged head sha, so bound review-gate evidence stays
valid) stayed NO-GO forever. The fix: among completed runs on a commit, the
LATEST one (by ``createdAt``, sorted here — gh's own order is not relied on)
is the current truth; older runs are history. See ``TestLatestRunWins`` and
``TestOrderUnknown`` below. The still-running guard is unaffected: an
in_progress/queued run blocks regardless of any completed run's conclusion.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import merge_preflight_ci_check as mpci
from merge_preflight_ci_check import (
    check_ci_run_for_head,
    DEFAULT_CI_WORKFLOW_NAME,
    CI_WORKFLOW_NAME_ENV_VAR,
    OVERRIDE_ENV_VAR,
)


def _git_head_mock(sha: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=sha + "\n", stderr="")


def _gh_auth_ok() -> MagicMock:
    return MagicMock(returncode=0, stdout="", stderr="")


def _gh_auth_fail(stderr: str = "You are not logged into any GitHub hosts.") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


def _gh_run_output(conclusion, head_sha, status="completed", database_id=12345, created_at="2026-01-01T00:00:00Z"):
    runs = [{
        "conclusion": conclusion,
        "headSha": head_sha,
        "status": status,
        "databaseId": database_id,
        "createdAt": created_at,
    }]
    return MagicMock(returncode=0, stdout=json.dumps(runs), stderr="")


def _gh_multi_run_output(runs):
    return MagicMock(returncode=0, stdout=json.dumps(runs), stderr="")


def _gh_empty_output():
    return MagicMock(returncode=0, stdout=json.dumps([]), stderr="")


def _subprocess_mocks(head_sha):
    """git head -> gh auth (ok) -> gh run list. No branch resolution (OI-1387):
    the query is commit-scoped now, so branch is never resolved by the function
    itself."""
    return [
        _git_head_mock(head_sha),
        _gh_auth_ok(),
    ]


GH_PRESENT = "/usr/local/bin/gh"


class TestCheckCIRunForHead:
    def test_ci_succeeded_for_exact_head(self, tmp_path):
        """(a) successful run for the exact head SHA -> GO. Also the OI-1387
        control case: exactly one run, conclusion=success, still GO."""
        sha = "a" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert result["ci_conclusion"] == "success"
        assert result["ran_on_sha"] is True
        assert result["head_sha"] == sha
        assert "geslaagd" in result["message"]

    def test_zero_runs_is_no_go(self, tmp_path):
        """(b) zero runs -> NO-GO with a message that names the consequence."""
        sha = "b" * 40
        mocks = _subprocess_mocks(sha) + [_gh_empty_output()]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is False
        assert "Geen VNX CI-run gevonden" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_failure_conclusion_is_no_go(self, tmp_path):
        """(c) run with conclusion=failure for the exact head -> NO-GO."""
        sha = "c" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("failure", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ci_conclusion"] == "failure"
        assert result["ran_on_sha"] is True
        assert "conclusion is 'failure'" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_run_with_mismatched_sha_is_no_go(self, tmp_path):
        """(d) defensive filter: a returned run whose headSha does not match
        head_sha (a gh quirk despite --commit scoping) never counts -> NO-GO,
        same message as zero runs."""
        sha = "d" * 40
        other = "e" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", other, database_id=99999)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is False
        assert result["head_sha"] == sha
        assert "Geen VNX CI-run gevonden" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_in_progress_run_is_no_go(self, tmp_path):
        """A still-running (in_progress) run for the exact head is NO-GO, not GO."""
        sha = "f" * 40
        mocks = _subprocess_mocks(sha) + [
            _gh_run_output(None, sha, status="in_progress")
        ]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is True
        assert "draait nog" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_not_available_is_no_go(self, tmp_path):
        """gh missing -> NO-GO with a distinct message, never a silent GO."""
        with patch("merge_preflight_ci_check.shutil.which", return_value=None):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "gh CLI niet beschikbaar" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_not_authenticated_is_no_go(self, tmp_path):
        """gh present but auth status fails -> NO-GO with a distinct message."""
        sha = "g" * 40
        mocks = [
            _git_head_mock(sha),
            _gh_auth_fail(),
        ]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "niet geauthenticeerd" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_run_list_failure_is_no_go(self, tmp_path):
        """gh run list non-zero exit -> NO-GO, never GO."""
        sha = "h" * 40
        run_fail = MagicMock(returncode=1, stdout="", stderr="HTTP 403")
        mocks = _subprocess_mocks(sha) + [run_fail]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "gh run list faalde" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_gh_output_unparseable_is_no_go(self, tmp_path):
        """gh emits non-JSON -> NO-GO, never a silent pass."""
        sha = "i" * 40
        bad_json = MagicMock(returncode=0, stdout="not json", stderr="")
        mocks = _subprocess_mocks(sha) + [bad_json]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "niet te parsen" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_git_head_unresolvable_is_no_go(self, tmp_path):
        """git rev-parse HEAD fails -> NO-GO, never a silent pass."""
        git_fail = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=[git_fail]), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "HEAD-SHA kon niet worden bepaald" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_abbreviated_head_sha_is_no_go(self, tmp_path):
        """A short head_sha is refused before querying: `gh run list --commit`
        silently returns zero rows for an abbreviated sha (measured 2026-08-23,
        PR #1672) which would misread as 'no CI ran' rather than 'wrong query'."""
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, head_sha="abc1234")
        assert result["verdict"] == "NO-GO"
        assert "afgekort" in result["message"]
        assert "niet toetsbaar" in result["message"]
        mock_run.assert_not_called()

    def test_uses_default_workflow_name(self, tmp_path):
        """The gh run list call uses the default workflow name 'VNX CI'."""
        sha = "j" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == DEFAULT_CI_WORKFLOW_NAME

    def test_explicit_workflow_name_wins(self, tmp_path):
        """An explicit workflow_name argument is used verbatim."""
        sha = "k" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, workflow_name="CI/CD Pipeline")
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"

    def test_env_workflow_name_override(self, tmp_path, monkeypatch):
        """VNX_CI_WORKFLOW_NAME is used when no explicit argument is given."""
        monkeypatch.setenv(CI_WORKFLOW_NAME_ENV_VAR, "CI/CD Pipeline")
        sha = "l" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        workflow_idx = gh_call_args.index("--workflow")
        assert gh_call_args[workflow_idx + 1] == "CI/CD Pipeline"


class TestCommitScopedQuery:
    """OI-1387: the merge-gate and the review-gate (``gh pr checks``, always
    commit-scoped) must agree on the same commit. Regression coverage for the
    fix: query by ``--commit``, and require ALL runs on that commit to have
    succeeded, not just the first one encountered.
    """

    def test_query_uses_commit_not_branch(self, tmp_path):
        """The gh run list call is scoped by --commit <full_head_sha>, never
        --branch, even when a branch is passed through for other callers."""
        sha = "1" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, branch="some-other-branch")
        assert result["verdict"] == "GO"
        gh_call_args = mock_run.call_args_list[2].args[0]
        assert "--branch" not in gh_call_args
        commit_idx = gh_call_args.index("--commit")
        assert gh_call_args[commit_idx + 1] == sha

    def test_running_run_on_other_branch_blocks_go(self, tmp_path):
        """OI-1387 regression, red on main: a sha pushed to two branches (the
        tmux-spawn fix-forward lane pushes the same commit to the target branch
        AND its own per-dispatch auto-branch) has two VNX CI runs — one
        success, one still running. Pre-fix ``check_ci_run_for_head`` returned
        on the FIRST run matching head_sha and ignored the rest, so this exact
        fixture gave GO on main (verdict='GO', measured against the pre-fix
        snapshot before this dispatch's edit). Post-fix it must NO-GO: every
        run for the commit must be conclusion=success, and none may still be
        running.
        """
        sha = "2" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111},
            {"conclusion": None, "headSha": sha, "status": "in_progress", "databaseId": 222},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, branch="feature-branch")
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is True
        assert "draait nog" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_two_successful_runs_on_same_commit_is_go(self, tmp_path):
        """Two runs for the same commit, both conclusion=success (e.g. one per
        branch after a fix-forward push) -> GO. Order is resolvable and the
        latest (222) is success, so it decides -- ci_run_id names that run,
        not a list of every run (OI-1613: only the latest counts)."""
        sha = "3" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T13:00:00Z"},
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert result["ci_conclusion"] == "success"
        assert result["ci_run_id"] == 222


class TestLatestRunWins:
    """OI-1613: among multiple completed runs on the same commit, the LATEST
    one (by ``createdAt``) is the current truth. An older run on the same sha
    never stands as a permanent veto once the same commit has been
    re-verified — nor does an older success paper over a newer failure.
    """

    def test_older_failure_newer_success_is_go(self, tmp_path):
        """OI-1613 regression, measured on PR #1743 (2026-09-02): an older
        failing run (14:46) sits next to a newer successful run (18:25) on the
        SAME head sha, because the tmux-spawn lane reopens the PR to
        retrigger CI on an unchanged head so bound review-gate evidence for
        that sha stays valid. The pre-fix rule ("every run must succeed")
        gave NO-GO here forever; the fix reads the latest run as the current
        truth -> GO."""
        sha = "5" * 40
        runs = [
            {"conclusion": "failure", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T14:46:00Z"},
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:25:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert result["ci_conclusion"] == "success"
        assert result["ci_run_id"] == 222
        assert "geslaagd" in result["message"]

    def test_older_success_newer_failure_is_no_go(self, tmp_path):
        """Mirror case: an older success must NOT paper over a newer failure
        on the same commit. The latest run decides in both directions."""
        sha = "6" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T13:00:00Z"},
            {"conclusion": "failure", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ci_conclusion"] == "failure"
        assert result["ci_run_id"] == 222
        assert "conclusion is 'failure'" in result["message"]
        assert "niet toetsbaar" in result["message"]

    def test_still_running_blocks_even_beside_a_newer_success(self, tmp_path):
        """Point (a), reaffirmed under OI-1613: a still-running run on the
        commit is NOT "an older run that lost to the latest" -- it blocks
        unconditionally, even when a *newer* completed run already succeeded.
        The still-running guard runs before any ordering/latest-wins logic."""
        sha = "7" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T13:00:00Z"},
            {"conclusion": None, "headSha": sha, "status": "in_progress", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert result["ran_on_sha"] is True
        assert "draait nog" in result["message"]


class TestOrderUnknown:
    """OI-1613 third branch: when order among completed runs cannot be
    established, that is its own named NO-GO -- never a silent fall-through
    to "first in the list" or back to the old all-must-succeed rule.
    """

    def test_missing_created_at_is_no_go(self, tmp_path):
        """One of two completed runs has no createdAt field at all."""
        sha = "8" * 40
        runs = [
            {"conclusion": "failure", "headSha": sha, "status": "completed", "databaseId": 111},
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "kan de volgorde" in result["message"]
        assert "niet vaststellen" in result["message"]
        assert "niet toetsbaar" in result["message"]
        # Must not fall back to the old "any failure anywhere = NO-GO because
        # it failed" wording, nor silently pick a "first" run's conclusion.
        assert "conclusion is" not in result["message"]

    def test_unparseable_created_at_is_no_go(self, tmp_path):
        """One of two completed runs has a createdAt that isn't valid ISO8601."""
        sha = "9" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "not-a-timestamp"},
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "kan de volgorde" in result["message"]
        assert "onparsebaar" in result["message"]

    def test_identical_created_at_is_no_go(self, tmp_path):
        """Two completed runs sharing the exact same createdAt timestamp: a
        tie is not "latest is green", it is order-unknown."""
        sha = "0" * 40
        runs = [
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T18:00:00Z"},
            {"conclusion": "failure", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "kan de volgorde" in result["message"]
        assert "dezelfde createdAt" in result["message"]

    def test_resolvable_order_is_not_flagged(self, tmp_path):
        """Positive control: two completed runs with distinct, parseable
        timestamps resolve normally and never hit the order-unknown path."""
        sha = "1a" + "1" * 38
        runs = [
            {"conclusion": "failure", "headSha": sha, "status": "completed", "databaseId": 111, "createdAt": "2026-09-02T13:00:00Z"},
            {"conclusion": "success", "headSha": sha, "status": "completed", "databaseId": 222, "createdAt": "2026-09-02T18:00:00Z"},
        ]
        mocks = _subprocess_mocks(sha) + [_gh_multi_run_output(runs)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert "kan de volgorde" not in result["message"]


class TestOverrideEscapeHatch:
    """The escape hatch: override with a required, visible reason.

    The reason IS the override. A non-empty reason skips the check with a
    visible GO; an empty/whitespace reason (or empty env value) is a refusal,
    never a silent bypass. Overrides short-circuit before any gh/git call.
    """

    def test_override_with_reason_is_go_and_visible(self, tmp_path):
        """Non-empty reason -> GO, flagged overridden, reason in the message."""
        reason = "hotfix: VNX CI flaked, run re-verified manually"
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, override_reason=reason)
        assert result["verdict"] == "GO"
        assert result["overridden"] is True
        assert result["override_reason"] == reason
        assert "OVERRIDE" in result["message"]
        assert "hotfix" in result["message"]
        mock_run.assert_not_called()

    def test_override_without_reason_is_refused(self, tmp_path):
        """Empty reason -> NO-GO: the escape hatch requires a reason."""
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path, override_reason="")
        assert result["verdict"] == "NO-GO"
        assert "reden" in result["message"]
        assert "niet toetsbaar" not in result["message"]
        mock_run.assert_not_called()

    def test_override_whitespace_reason_is_refused(self, tmp_path):
        """Whitespace-only reason is treated as no reason -> NO-GO."""
        result = check_ci_run_for_head(tmp_path, override_reason="   ")
        assert result["verdict"] == "NO-GO"
        assert "reden" in result["message"]

    def test_override_env_var_with_reason(self, tmp_path, monkeypatch):
        """VNX_MERGE_OVERRIDE_REASON with a reason -> visible GO override."""
        monkeypatch.setenv(OVERRIDE_ENV_VAR, "operator-approved: manual verify")
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "GO"
        assert result["overridden"] is True
        assert "operator-approved" in result["message"]
        mock_run.assert_not_called()

    def test_override_env_var_empty_is_refused(self, tmp_path, monkeypatch):
        """An empty env value is an override attempt without a reason -> NO-GO."""
        monkeypatch.setenv(OVERRIDE_ENV_VAR, "")
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            result = check_ci_run_for_head(tmp_path)
        assert result["verdict"] == "NO-GO"
        assert "reden" in result["message"]
        mock_run.assert_not_called()


class TestCLI:
    def test_cli_override_reason_flag_go(self, tmp_path, capsys):
        """--override-reason with a reason exits 0 and reports overridden."""
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main([
                "--project-root", str(tmp_path), "--json",
                "--override-reason", "manual verify after flake",
            ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "GO"
        assert out["overridden"] is True
        assert "manual verify" in out["message"]
        mock_run.assert_not_called()

    def test_cli_override_reason_empty_refused(self, tmp_path, capsys):
        """--override-reason with an empty value exits 1 and refuses."""
        with patch("merge_preflight_ci_check.subprocess.run") as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main([
                "--project-root", str(tmp_path), "--json",
                "--override-reason", "",
            ])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "NO-GO"
        assert "reden" in out["message"]
        mock_run.assert_not_called()

    def test_cli_json_go_exits_zero(self, tmp_path, capsys):
        sha = "m" * 40
        mocks = _subprocess_mocks(sha) + [_gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main(["--project-root", str(tmp_path), "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "GO"

    def test_cli_json_no_go_exits_nonzero(self, tmp_path, capsys):
        sha = "n" * 40
        mocks = _subprocess_mocks(sha) + [_gh_empty_output()]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks), \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main(["--project-root", str(tmp_path), "--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "NO-GO"
        assert "niet toetsbaar" in out["message"]

    def test_cli_branch_flag_accepted_but_unused_in_query(self, tmp_path, capsys):
        """--branch is still a valid CLI flag (backward compat for callers that
        pass it) but must not appear in the gh run list invocation (OI-1387).
        An explicit --head-sha skips git rev-parse HEAD entirely, so only the
        gh auth + gh run list calls are mocked."""
        sha = "o" * 40
        mocks = [_gh_auth_ok(), _gh_run_output("success", sha)]
        with patch("merge_preflight_ci_check.subprocess.run", side_effect=mocks) as mock_run, \
             patch("merge_preflight_ci_check.shutil.which", return_value=GH_PRESENT):
            rc = mpci.main([
                "--project-root", str(tmp_path), "--json",
                "--head-sha", sha, "--branch", "some-branch",
            ])
        assert rc == 0
        gh_call_args = mock_run.call_args_list[-1].args[0]
        assert "--branch" not in gh_call_args
        assert "--commit" in gh_call_args
