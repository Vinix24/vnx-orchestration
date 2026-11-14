"""OI-2811: a dispatch worktree whose HEAD no remote branch contains is kept,
whoever made the commit. Real git: a bare origin in tmp_path, a clone as the
main checkout, real ``git worktree`` layouts. No network, no gh."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import dispatch_worktree_isolation as dwi
import pr_enforcement as pe
from vnx_cli.commands import status as status_cmd


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.main = root / "main-checkout"

    def worktree(self, dispatch_id: str, subject: str) -> Path:
        wt = self.main / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(self.main, "worktree", "add", "-b", f"dispatch/{dispatch_id}", str(wt), "origin/main")
        (wt / "alpha.py").write_text("print('builder edit')\n")
        _git(wt, "commit", "-am", subject)
        return wt


@pytest.fixture()
def repo(tmp_path: Path) -> _Repo:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], check=True, capture_output=True)
    r = _Repo(tmp_path)
    subprocess.run(["git", "clone", str(bare), str(r.main)], check=True, capture_output=True)
    _git(r.main, "config", "user.email", "t@test.local")
    _git(r.main, "config", "user.name", "Test")
    if _git(r.main, "branch", "--show-current").stdout.strip() != "main":
        _git(r.main, "checkout", "-b", "main")
    (r.main / "alpha.py").write_text("print('alpha')\n")
    _git(r.main, "add", "alpha.py")
    _git(r.main, "commit", "-m", "initial")
    _git(r.main, "push", "-u", "origin", "main")
    return r


def _teardown(repo: _Repo, dispatch_id: str) -> None:
    dwi.remove_dispatch_worktree(dispatch_id, project_root=repo.main, data_dir=repo.root / "data")


def test_builder_commit_not_on_origin_is_kept(repo):
    wt = repo.worktree("b1", "feat: builder work")
    _teardown(repo, "b1")
    assert wt.exists(), "the only checkout of an unpublished builder commit must survive teardown"
    assert dwi._keep_unpublished_head(wt, "committed") == "dirty"


def test_builder_commit_after_push_is_not_kept(repo):
    wt = repo.worktree("b2", "feat: builder work")
    _git(wt, "push", "origin", "dispatch/b2")
    assert dwi._keep_unpublished_head(wt, "committed") == "committed"
    assert pe.unpublished_head_commit(wt) is None
    _teardown(repo, "b2")
    assert not wt.exists()


def test_salvage_commit_not_on_origin_is_still_kept(repo):
    wt = repo.worktree("b3", f"{pe.SALVAGE_COMMIT_SUBJECT_PREFIX} x")
    assert dwi._keep_unpublished_head(wt, "committed") == "dirty"


def test_remote_check_failure_keeps_the_worktree(repo):
    wt = repo.worktree("b4", "feat: builder work")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if "--contains" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: boom")
        return real_run(cmd, *a, **kw)

    _git(wt, "push", "origin", "dispatch/b4")  # published, yet the check cannot say so
    with patch.object(pe.subprocess, "run", side_effect=fake_run):
        assert pe.unpublished_head_commit(wt) == _git(wt, "rev-parse", "HEAD").stdout.strip()
        assert dwi._keep_unpublished_head(wt, "committed") == "dirty"


def test_remote_check_raising_keeps_the_worktree(repo):
    wt = repo.worktree("b5", "feat: builder work")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if "--contains" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return real_run(cmd, *a, **kw)

    with patch.object(pe.subprocess, "run", side_effect=fake_run):
        assert pe.unpublished_head_commit(wt) is not None


def test_unreadable_head_and_other_classifications_are_untouched(repo):
    assert pe.unpublished_head_commit(repo.root / "missing") is None
    wt = repo.worktree("b6", "feat: builder work")
    assert dwi._keep_unpublished_head(wt, "clean") == "clean"
    assert dwi._keep_unpublished_head(wt, "dirty") == "dirty"


def test_head_commit_subject_is_cut_at_60(repo):
    wt = repo.worktree("b7", "x" * 80)
    assert pe.head_commit_subject(wt) == "x" * 60
    assert pe.head_commit_subject(repo.root / "missing") == ""


def test_status_line_shows_count_and_newest_id(repo):
    repo.worktree("old1", "feat: a")
    repo.worktree("new2", "feat: b")
    publish = repo.worktree("pub3", "feat: c")
    _git(publish, "push", "origin", "dispatch/pub3")
    line = status_cmd._held_worktrees_line(repo.main)
    assert line.startswith("held worktrees: 2 (newest ")
    assert "pub3" not in line


def test_status_line_none_when_nothing_held(repo):
    assert status_cmd._held_worktrees_line(repo.main) == "held worktrees: 0"
