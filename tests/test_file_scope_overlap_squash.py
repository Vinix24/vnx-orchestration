"""test_file_scope_overlap_squash.py — OI-1641: squash-aware merge detection + bounded scan.

warn_overlaps() used to `git fetch origin <branch>` once per remote dispatch/* branch just to
learn whether it was merged, then fetch AGAIN per "open" branch to diff it. On the live remote
(370 branches) that was ~715 serial fetches at ~0.59s each — ~7 minutes of silence on every
merge. Worse: `_is_merged` used only `git merge-base --is-ancestor`, and VNX squashes every PR,
so a squash-merged branch's tip is never main's ancestor and stayed "open" forever (345/370
measured as open, only 25 recognized via ancestry).

This file covers the three fixes: (1) `_is_merged` recognizes a squash-merged branch via a
gh-PR-status check with a patch-equivalence fallback, (2) a single bulk fetch replaces the
per-branch fetches, (3) a hard timeout on `_run` plus a scan ceiling so the "an overlap check
must never block a merge" contract in pr_merge.py is actually true under load.

Companion to tests/test_file_scope_overlap.py (ancestry-only path + pr_merge.py wiring), which
this file does not duplicate.
"""
from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import file_scope_overlap as fso


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_fixture(tmp_path: Path):
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    bare.mkdir()
    _run_git(bare, "init", "--bare", "-b", "main")

    work.mkdir()
    _run_git(work, "init", "-b", "main")
    _run_git(work, "config", "user.email", "test@example.com")
    _run_git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(work, "add", "README.md")
    _run_git(work, "commit", "-m", "base commit")
    _run_git(work, "remote", "add", "origin", str(bare))
    _run_git(work, "push", "origin", "main")
    return work


def _push_branch(work: Path, branch: str, filename: str, content: str) -> None:
    """Push a branch off main carrying one new file, then drop the local branch ref."""
    _run_git(work, "checkout", "-b", branch, "main")
    (work / filename).write_text(content, encoding="utf-8")
    _run_git(work, "add", filename)
    _run_git(work, "commit", "-m", f"add {filename}")
    _run_git(work, "push", "origin", branch)
    _run_git(work, "checkout", "main")
    _run_git(work, "branch", "-D", branch)


def _squash_merge_into_main(work: Path, filename: str, content: str) -> None:
    """Simulate a GitHub squash-merge: land the branch's file change on main as a brand-new
    commit with no ancestry link back to the branch tip, then push main. The branch ref itself
    is left in place on the remote (VNX's normal state before dispatch-reap deletes it) — the
    exact shape that defeats `merge-base --is-ancestor`.
    """
    _run_git(work, "checkout", "main")
    (work / filename).write_text(content, encoding="utf-8")
    _run_git(work, "add", filename)
    _run_git(work, "commit", "-m", f"squash: add {filename} (#1)")
    _run_git(work, "push", "origin", "main")


# ---------------------------------------------------------------------------
# 1. _is_merged must recognize a squash-merged branch (the ROOD case pre-fix)
# ---------------------------------------------------------------------------


def test_is_merged_true_for_squash_merged_branch(git_fixture):
    _push_branch(git_fixture, "dispatch/squash-me", "squash.txt", "squashed content\n")
    _squash_merge_into_main(git_fixture, "squash.txt", "squashed content\n")
    _run_git(git_fixture, "fetch", "origin")
    # sanity: ancestry alone must NOT see this as merged — otherwise the test proves nothing.
    assert fso._run(
        ["git", "merge-base", "--is-ancestor", "origin/dispatch/squash-me", "origin/main"],
        repo=git_fixture, check=False,
    ).returncode != 0
    assert fso._is_merged(
        "dispatch/squash-me", base_ref="origin/main", repo=git_fixture, run_timeout=5,
    ) is True


# ---------------------------------------------------------------------------
# 2. _is_merged stays False for a branch with genuinely unique, unmerged work
# ---------------------------------------------------------------------------


def test_is_merged_false_for_unmerged_branch(git_fixture):
    _push_branch(git_fixture, "dispatch/unmerged", "unmerged.txt", "still open\n")
    _run_git(git_fixture, "fetch", "origin")
    assert fso._is_merged(
        "dispatch/unmerged", base_ref="origin/main", repo=git_fixture, run_timeout=5,
    ) is False


# ---------------------------------------------------------------------------
# 3. _run() honors a timeout instead of hanging or raising
# ---------------------------------------------------------------------------


def test_run_honors_timeout_without_raising():
    start = time.perf_counter()
    result = fso._run(["sleep", "5"], repo=None, check=False, timeout=1)
    elapsed = time.perf_counter() - start
    assert elapsed < 4
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# 4. A branch count above the ceiling short-circuits to [] with zero git calls
# ---------------------------------------------------------------------------


def test_open_dispatch_branches_bails_above_max_branches(monkeypatch):
    fake_branches = [f"dispatch/fake-{i}" for i in range(500)]
    monkeypatch.setattr(fso, "_list_remote_dispatch_branches", lambda repo, **kw: fake_branches)

    def _no_run_expected(cmd, **kw):
        raise AssertionError(f"no _run call expected once the branch-count ceiling is hit: {cmd}")

    monkeypatch.setattr(fso, "_run", _no_run_expected)

    capture = io.StringIO()
    result = fso.open_dispatch_branches(repo=None, max_branches=10, stream=capture)
    assert result == []
    emitted = capture.getvalue()
    assert "500" in emitted
    assert "10" in emitted


# ---------------------------------------------------------------------------
# 5. Exactly one `git fetch` regardless of branch count
# ---------------------------------------------------------------------------


def test_open_dispatch_branches_does_a_single_bulk_fetch(monkeypatch):
    fake_branches = [f"dispatch/fake-{i}" for i in range(12)]
    monkeypatch.setattr(fso, "_list_remote_dispatch_branches", lambda repo, **kw: fake_branches)
    monkeypatch.setattr(fso, "_is_merged", lambda branch, **kw: False)

    fetch_calls = []

    def _counting_run(cmd, **kw):
        if cmd[:2] == ["git", "fetch"]:
            fetch_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(fso, "_run", _counting_run)

    result = fso.open_dispatch_branches(repo=None, max_branches=100)
    assert len(fetch_calls) == 1
    assert result == fake_branches
