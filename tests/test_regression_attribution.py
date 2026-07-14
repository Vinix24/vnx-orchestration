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
