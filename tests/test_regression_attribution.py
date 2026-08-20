"""Tests for the regression-attribution primitive (track: regression-attribution-canary, PR-1).

Builds a tiny throwaway git fixture repo per test and exercises the real
`git bisect` mechanism end-to-end — no mocking of git subprocess calls.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

from regression_attribution import (  # noqa: E402
    AttributionResult,
    DirtyWorkingTreeError,
    RegressionAttributionError,
    _require_bisect_converged,
    attribute_regression,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _head_branch(root: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _bisect_in_progress(root: Path) -> bool:
    result = subprocess.run(
        ["git", "bisect", "log"], cwd=str(root), capture_output=True, text=True,
    )
    return result.returncode == 0


@pytest.fixture()
def fixture_repo(tmp_path):
    """A tiny repo: good commit -> unrelated commit -> BREAKING commit -> unrelated commit (HEAD)."""
    root = tmp_path / "fixture-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "VNX Test")
    _git(root, "config", "user.email", "vnx-test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "check.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / "README.md").write_text("v1\n")
    commit1 = _commit(root, "commit1: initial passing check")

    (root / "README.md").write_text("v2\n")
    commit2 = _commit(root, "commit2: unrelated doc change")

    (root / "check.sh").write_text("#!/bin/bash\nexit 1\n")
    commit3 = _commit(root, "commit3: BREAKS check.sh")

    (root / "README.md").write_text("v3\n")
    commit4 = _commit(root, "commit4: unrelated change after break")

    return {
        "root": root,
        "commit1": commit1,
        "commit2": commit2,
        "commit3": commit3,
        "commit4": commit4,
    }


@pytest.fixture()
def divergent_fixture_repo(tmp_path):
    """Diverged history: the breaking commit lives on a side branch, `good`
    sits on the mainline, and neither is an ancestor of the other.

    Regression fixture for the range-check fix: the old check required
    good_sha to be an ancestor of the reported sha, which a diverged history
    never satisfies even though `git bisect` itself converges correctly
    (its real lower boundary is the merge-base of good_sha and bad_sha).
    """
    root = tmp_path / "divergent-fixture-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "VNX Test")
    _git(root, "config", "user.email", "vnx-test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "check.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / "README.md").write_text("v1\n")
    root_commit = _commit(root, "root: initial passing check")

    _git(root, "checkout", "--quiet", "-b", "side")
    (root / "check.sh").write_text("#!/bin/bash\nexit 1\n")
    break_commit = _commit(root, "side: BREAKS check.sh")
    (root / "README.md").write_text("side-v2\n")
    bad_commit = _commit(root, "side: unrelated change after break")

    _git(root, "checkout", "--quiet", root_commit)
    _git(root, "checkout", "--quiet", "-b", "main-line")
    (root / "README.md").write_text("main-v2\n")
    good_commit = _commit(root, "main-line: unrelated doc change, check still passing")

    return {
        "root": root,
        "root_commit": root_commit,
        "break_commit": break_commit,
        "bad_commit": bad_commit,
        "good_commit": good_commit,
    }


class TestAttributesExactBreakingCommit:
    def test_names_exact_breaking_commit_and_files(self, fixture_repo):
        root = fixture_repo["root"]
        result = attribute_regression(
            check_cmd="bash check.sh",
            good_ref=fixture_repo["commit2"],
            bad_ref="HEAD",
            repo_root=root,
        )
        assert isinstance(result, AttributionResult)
        assert result.status == "attributed"
        assert result.commit_sha == fixture_repo["commit3"]
        assert result.author == "VNX Test"
        assert result.subject == "commit3: BREAKS check.sh"
        assert result.changed_files == ["check.sh"]
        assert result.check_cmd == "bash check.sh"
        assert result.good_sha == fixture_repo["commit2"]
        assert result.bad_sha == fixture_repo["commit4"]


class TestNotARegressionGuard:
    def test_check_fails_at_good_ref_is_inconclusive_and_skips_bisect(self, fixture_repo):
        root = fixture_repo["root"]
        # good_ref points at a commit where the check ALSO fails (commit3) —
        # not a regression within (commit3, HEAD].
        result = attribute_regression(
            check_cmd="bash check.sh",
            good_ref=fixture_repo["commit3"],
            bad_ref="HEAD",
            repo_root=root,
        )
        assert result.status == "inconclusive"
        assert "also failed at good_ref" in result.reason
        assert result.commit_sha is None
        assert result.changed_files == []
        assert not _bisect_in_progress(root)

    def test_check_passes_at_bad_ref_is_inconclusive_and_skips_bisect(self, fixture_repo):
        root = fixture_repo["root"]
        # bad_ref points at commit2, where the check still passes -> nothing to attribute.
        result = attribute_regression(
            check_cmd="bash check.sh",
            good_ref=fixture_repo["commit1"],
            bad_ref=fixture_repo["commit2"],
            repo_root=root,
        )
        assert result.status == "inconclusive"
        assert "nothing to attribute" in result.reason
        assert result.commit_sha is None
        assert not _bisect_in_progress(root)


class TestHeadRestoration:
    def test_restores_original_branch_and_sha_after_attribution(self, fixture_repo):
        root = fixture_repo["root"]
        original_branch = _head_branch(root)
        original_sha = _head_sha(root)
        assert original_sha == fixture_repo["commit4"]

        attribute_regression(
            check_cmd="bash check.sh",
            good_ref=fixture_repo["commit2"],
            bad_ref="HEAD",
            repo_root=root,
        )

        assert _head_branch(root) == original_branch
        assert _head_sha(root) == original_sha
        assert not _bisect_in_progress(root)

    def test_restores_original_ref_even_on_inconclusive_path(self, fixture_repo):
        root = fixture_repo["root"]
        original_branch = _head_branch(root)
        original_sha = _head_sha(root)

        attribute_regression(
            check_cmd="bash check.sh",
            good_ref=fixture_repo["commit3"],
            bad_ref="HEAD",
            repo_root=root,
        )

        assert _head_branch(root) == original_branch
        assert _head_sha(root) == original_sha


class TestBisectConvergenceIsReadStructurally:
    """OI-1366: the bisect result must not depend on git's human-readable wording.

    git's "first bad commit" prose differs by git version: older git writes
    `<sha> is the first bad commit`, newer git quotes the (configurable)
    term: `<sha> is the first 'bad' commit`. The two blocks below are the
    literal outputs of both forms (local git 2.50.1 and a GitHub runner,
    verbatim from the OI-1366 dispatch). They need no real git: the
    interpretation layer must accept both identically, because it never
    reads the prose — only the exit status is structural.
    """

    # Literal output of git 2.50.1 (macOS), unquoted term.
    GIT_2_50_UNQUOTED_OUTPUT = (
        "51254e9df4a19eb1dbd402c46af696706ae1430b is the first bad commit\n"
        "bisect found first bad commit\n"
    )

    # Literal output from the GitHub runner (CI log of run 32297581293,
    # attempt 2, sha 37ad9d47, 2026-08-19T23:39:31Z), quoted term.
    CI_RUNNER_QUOTED_OUTPUT = (
        "running 'bash' '-c' 'bash check.sh'\n"
        "118c07cf8d2dc11d6c3ae032a39aa4923e0ed8ed is the first 'bad' commit\n"
        "commit 118c07cf8d2dc11d6c3ae032a39aa4923e0ed8ed\n"
        "Author: VNX Test <vnx-test@example.invalid>\n"
        "...\n"
        "bisect found first 'bad' commit\n"
    )

    # Literal tail of a real non-converging run (every candidate skipped).
    NON_CONVERGING_OUTPUT = (
        "We cannot bisect more!\n"
        "error: bisect run cannot continue any more\n"
    )

    def test_unquoted_first_bad_prose_is_accepted(self):
        _require_bisect_converged(0, self.GIT_2_50_UNQUOTED_OUTPUT)

    def test_quoted_first_bad_prose_is_accepted(self):
        _require_bisect_converged(0, self.CI_RUNNER_QUOTED_OUTPUT)

    def test_prose_wording_is_not_the_acceptance_signal(self):
        # The same prose that reads "first bad commit found" must STILL fail
        # when the structural signal (exit status) says the run failed —
        # proving acceptance comes from the exit status, never the wording.
        for prose in (self.GIT_2_50_UNQUOTED_OUTPUT, self.CI_RUNNER_QUOTED_OUTPUT):
            with pytest.raises(RegressionAttributionError, match="did not converge"):
                _require_bisect_converged(1, prose)

    def test_real_non_converging_output_raises(self):
        with pytest.raises(RegressionAttributionError, match="did not converge"):
            _require_bisect_converged(2, self.NON_CONVERGING_OUTPUT)


class TestBisectNonConvergenceEndToEnd:
    """A real `git bisect run` that cannot converge must fail loudly."""

    def test_all_candidates_skipped_raises_and_restores(self, fixture_repo):
        root = fixture_repo["root"]
        # check.sh passes through commit2 and fails from commit3 on. Mapping
        # every check failure to skip (125) makes the bad boundary count as
        # failing while every interior bisect candidate is skipped, so git
        # genuinely cannot converge ("We cannot bisect more!").
        check_cmd = "bash check.sh && exit 0 || exit 125"

        with pytest.raises(RegressionAttributionError, match="did not converge"):
            attribute_regression(
                check_cmd=check_cmd,
                good_ref=fixture_repo["commit2"],
                bad_ref="HEAD",
                repo_root=root,
            )

        assert _head_sha(root) == fixture_repo["commit4"]
        assert not _bisect_in_progress(root)


class TestDivergentHistoryUsesMergeBaseLowerBound:
    """OI-1617: good_sha need not be an ancestor of the bisected sha.

    Before the fix, the range check required good_sha itself to be an
    ancestor of the reported sha — false whenever history has diverged,
    even though git bisect converges correctly. The fix moves the lower
    bound to merge-base(good_sha, bad_sha), which is git's real boundary.
    """

    def test_attributes_breaking_commit_on_side_branch(self, divergent_fixture_repo):
        root = divergent_fixture_repo["root"]

        # Sanity check the fixture actually diverges: good_commit must NOT
        # be an ancestor of bad_commit, or this test would not exercise the
        # merge-base fix at all.
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor",
             divergent_fixture_repo["good_commit"], divergent_fixture_repo["bad_commit"]],
            cwd=str(root),
        ).returncode != 0

        result = attribute_regression(
            check_cmd="bash check.sh",
            good_ref=divergent_fixture_repo["good_commit"],
            bad_ref=divergent_fixture_repo["bad_commit"],
            repo_root=root,
        )

        assert result.status == "attributed"
        assert result.commit_sha == divergent_fixture_repo["break_commit"]
        assert result.subject == "side: BREAKS check.sh"


class TestDirtyWorkingTreeRefused:
    def test_refuses_dirty_working_tree(self, fixture_repo):
        root = fixture_repo["root"]
        (root / "README.md").write_text("uncommitted change\n")

        with pytest.raises(DirtyWorkingTreeError):
            attribute_regression(
                check_cmd="bash check.sh",
                good_ref=fixture_repo["commit2"],
                bad_ref="HEAD",
                repo_root=root,
            )

        # Guard must fire before any checkout — the dirty change and HEAD
        # position are untouched.
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True,
        ).stdout
        assert "README.md" in status
        assert _head_sha(root) == fixture_repo["commit4"]
        assert not _bisect_in_progress(root)

    def test_refuses_dirty_tree_with_untracked_file(self, fixture_repo):
        root = fixture_repo["root"]
        (root / "untracked.txt").write_text("new file\n")

        with pytest.raises(DirtyWorkingTreeError):
            attribute_regression(
                check_cmd="bash check.sh",
                good_ref=fixture_repo["commit2"],
                bad_ref="HEAD",
                repo_root=root,
            )

        assert (root / "untracked.txt").exists()
