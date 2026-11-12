#!/usr/bin/env python3
"""DR-7 (requirement 9): the merge-tree record — which commit and which tree
landed on main versus what the gates tested — computed under one short budget.

The audit-impact study (``20261108-res-graafaudit``) could not answer "which tree
landed on main" for PR #2457 (gap M8). These tests call ``_merge_tree_record`` and
the helpers it draws its budget through directly against a throwaway git
repository, with only the ``gh`` merge-commit lookup patched: no merge is driven
and no receipt is written here. Wiring the record into ``pr_merged`` and
``merge_pr`` follows in split 2.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge  # noqa: E402
from merge_target import MergeTarget  # noqa: E402

PR = 2457


# --- a real throwaway repository -------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T")):
        _git(repo, *args)
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _feature_head(repo: Path, branch: str = "feature") -> str:
    """A feature branch off main with one commit; the checkout returns to main."""
    _git(repo, "checkout", "-q", "-b", branch)
    head = _commit(repo, "feature.py", "feature\n", "feat: feature")
    _git(repo, "checkout", "-q", "main")
    return head


def _squash(repo: Path, branch: str = "feature") -> str:
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", branch)
    _git(repo, "commit", "-q", "-m", f"squash {branch}")
    return _git(repo, "rev-parse", "HEAD")


def _tree(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}")


# --- the narrow git/gh seam ------------------------------------------------


def _real_git_in_target(args: List[str], timeout: float | None = None):
    """The production git runner, over the merge target this test pinned."""
    return subprocess.run(
        ["git", "-C", str(pr_merge._target_root()), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _completed(args: List[str], returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(list(args), returncode, stdout=stdout, stderr=stderr)


def _record_for(repo: Path, monkeypatch, tested_head: str, *, gh):
    """A merge-tree record over a throwaway repo: real git, the given ``gh``."""
    monkeypatch.setattr(
        pr_merge, "_ACTIVE_TARGET", MergeTarget(project_root=repo, repo="acme/widgets"))
    return pr_merge._merge_tree_record(PR, tested_head, gh=gh, git=_real_git_in_target)


def _gh_returning(oid: str):
    def fake_gh(args, timeout=None):
        return _completed(list(args), 0, json.dumps({"mergeCommit": {"oid": oid}}))
    return fake_gh


# --- the three record outcomes, called directly ----------------------------


def test_record_squash_on_unmoved_main_marks_trees_equal(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    merge_sha = _squash(repo)

    record = _record_for(repo, monkeypatch, tested_head, gh=_gh_returning(merge_sha))

    assert record["merge_sha"] == merge_sha
    assert record["merged_tree"] == _tree(repo, merge_sha)
    assert record["tested_head"] == tested_head
    assert record["tested_tree"] == _tree(repo, tested_head)
    assert record["tree_equal"] is True
    assert "merge_tree_unavailable" not in record


def test_record_squash_after_main_moved_marks_trees_unequal(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    _commit(repo, "other.py", "other\n", "chore: main moved")  # main moved between review and merge
    merge_sha = _squash(repo)

    record = _record_for(repo, monkeypatch, tested_head, gh=_gh_returning(merge_sha))

    assert record["merge_sha"] == merge_sha
    assert record["merged_tree"] == _tree(repo, merge_sha)
    assert record["tested_tree"] == _tree(repo, tested_head)
    assert record["merged_tree"] != record["tested_tree"]
    assert record["tree_equal"] is False


def test_record_gh_failure_names_the_reason_and_never_says_equal(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    _squash(repo)

    def failing_gh(args, timeout=None):
        return _completed(list(args), 1, stderr="gh: not authenticated")

    record = _record_for(repo, monkeypatch, tested_head, gh=failing_gh)

    assert "merge_sha" not in record and "merged_tree" not in record
    assert "gh_error:" in record[pr_merge.MERGE_TREE_UNAVAILABLE]
    assert "not authenticated" in record[pr_merge.MERGE_TREE_UNAVAILABLE]
    # What could be read is booked; unknown is never equal.
    assert record["tested_head"] == tested_head
    assert record["tested_tree"] == _tree(repo, tested_head)
    assert "tree_equal" not in record


# --- DR-7 round 2: one short budget for the whole record -------------------

MERGE_SHA, TESTED_HEAD = "a" * 40, "b" * 40


def _gh_returning_merge_commit():
    def fake_gh(args, timeout=None):
        return _completed(list(args), 0, json.dumps({"mergeCommit": {"oid": MERGE_SHA}}))
    return fake_gh


def test_budget_exhausted_after_a_timeout_skips_the_rest():
    """(a) A timed-out read spends the budget: the record returns at once, named."""
    def fake_git(args, timeout=None):
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=timeout)

    started = time.monotonic()
    record = pr_merge._merge_tree_record(
        PR, TESTED_HEAD, gh=_gh_returning_merge_commit(), git=fake_git,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 20.0, "the record must return well inside the budget"
    assert "budget_exhausted" in record[pr_merge.MERGE_TREE_UNAVAILABLE]
    # The merge commit itself was read before the budget ran out; no tree was.
    assert record["merge_sha"] == MERGE_SHA
    assert "merged_tree" not in record and "tested_tree" not in record
    assert "tree_equal" not in record
    assert pr_merge._MERGE_TREE_BUDGET_S == 20


def test_every_call_gets_the_remaining_budget_as_its_timeout():
    """(b) One deadline: each call's timeout is the remaining budget, never more."""
    timeouts: List[float] = []

    def fake_gh(args, timeout=None):
        timeouts.append(timeout)
        return _completed(list(args), 0, json.dumps({"mergeCommit": {"oid": MERGE_SHA}}))

    def fake_git(args, timeout=None):
        timeouts.append(timeout)
        if args[0] == "cat-file":
            return _completed(list(args), 0)
        if args[0] == "fetch":
            return _completed(list(args), 0)
        return _completed(list(args), 0, stdout=MERGE_SHA)

    record = pr_merge._merge_tree_record(
        PR, TESTED_HEAD, gh=fake_gh, git=fake_git,
    )

    assert "tree_equal" in record
    assert all(t is not None and 0 < t <= pr_merge._MERGE_TREE_BUDGET_S for t in timeouts)
    # Every call draws from one deadline, so the remaining time never grows.
    assert timeouts == sorted(timeouts, reverse=True)


def test_at_most_one_fetch_across_the_whole_record():
    """(c) Neither sha local: the second lookup reuses the first fetch."""
    fetches: List[List[str]] = []

    def fake_git(args, timeout=None):
        if args[0] == "cat-file":
            return _completed(list(args), 128, stderr="not local")
        if args[0] == "fetch":
            fetches.append(list(args))
            return _completed(list(args), 0)
        return _completed(list(args), 128, stderr="still not local")

    record = pr_merge._merge_tree_record(
        PR, TESTED_HEAD, gh=_gh_returning_merge_commit(), git=fake_git,
    )

    assert len(fetches) == 1
    assert "merged_tree" not in record and "tested_tree" not in record
