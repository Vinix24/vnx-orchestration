"""OI-2716: a detached HEAD never stands in for the dispatch branch; its push names a full ref.

Real git on a bare origin in tmp_path. No network, no gh.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import pr_enforcement as pe
import tmux_worktree as tw

DID = "oi2716"
BRANCH = f"dispatch/{DID}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)
    main = tmp_path / "main"
    subprocess.run(["git", "clone", str(bare), str(main)], check=True, capture_output=True)
    _git(main, "config", "user.email", "t@test.local")
    _git(main, "config", "user.name", "Test")
    _git(main, "checkout", "-B", "main")
    (main / "a.py").write_text("a\n")
    _git(main, "add", "a.py")
    _git(main, "commit", "-m", "init")
    _git(main, "push", "-u", "origin", "main")
    return main


def _detached(repo: Path, tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "--detach", str(wt), "origin/main")
    (wt / "b.py").write_text("b\n")
    _git(wt, "add", "b.py")
    _git(wt, "commit", "-m", "work")
    return wt


def test_pushed_detached_head_reads_pushed_and_runs_no_push(repo, tmp_path):
    wt = _detached(repo, tmp_path)
    _git(wt, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    base = _git(wt, "rev-parse", "origin/main")
    assert tw.resolve_effective_branch(wt=wt, expected_branch=BRANCH, dispatch_id=DID) == BRANCH
    assert tw.classify_path(wt=wt, branch=BRANCH, dispatch_id=DID, base_sha=base) == "pushed"
    # classification is read-only: origin's branch is untouched
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()[0] == _git(wt, "rev-parse", "HEAD")


def test_pushed_detached_head_behind_origin_reads_pushed(repo, tmp_path):
    wt = _detached(repo, tmp_path)
    _git(wt, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    base = _git(wt, "rev-parse", "origin/main")
    (wt / "c.py").write_text("c\n")
    _git(wt, "add", "c.py")
    _git(wt, "commit", "-m", "more")
    _git(wt, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    _git(wt, "reset", "-q", "--hard", "HEAD~1")
    assert tw.classify_path(wt=wt, branch=BRANCH, dispatch_id=DID, base_sha=base) == "pushed"


def test_unpushed_detached_head_is_committed_and_push_lands(repo, tmp_path):
    wt = _detached(repo, tmp_path)
    base = _git(wt, "rev-parse", "origin/main")
    assert tw.classify_path(wt=wt, branch=BRANCH, dispatch_id=DID, base_sha=base) == "committed"
    outcome = pe._push_branch(branch=BRANCH, cwd=wt)
    assert outcome.ok, outcome.reason
    assert _git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").split()[0] == _git(wt, "rev-parse", "HEAD")


def test_attached_worktree_keeps_push_dash_u(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", BRANCH, str(wt), "origin/main")
    calls = []

    def fake(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch("pr_enforcement.subprocess.run", side_effect=fake):
        # symbolic-ref answers 0 on the fake, so HEAD reads attached
        assert pe._push_branch(branch=BRANCH, cwd=wt).ok
    assert calls[-1][-4:] == ["push", "-u", "origin", BRANCH]


def test_custom_branch_still_wins(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "fix/foo", str(wt), "origin/main")
    assert tw.resolve_effective_branch(wt=wt, expected_branch=BRANCH, dispatch_id=DID) == "fix/foo"


def test_drift_safe_detached_returns_expected_branch():
    assert tw._drift_safe_effective_branch(current="HEAD", branch=BRANCH, dispatch_id=DID) == (BRANCH, False)
