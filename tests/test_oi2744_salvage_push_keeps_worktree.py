"""OI-2744: the door's salvage push waits as long as a pre-push hook needs
(VNX_SALVAGE_PUSH_TIMEOUT_S), and a worktree whose salvage commit is on no
remote branch is never torn down.

Real git throughout: a bare repo as origin, a clone as the main checkout, a
real ``git worktree`` laid out like a dispatch worktree. No network, no gh.
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch_worktree_isolation as dwi
import pr_enforcement as pe

KEY = "VNX_SALVAGE_PUSH_TIMEOUT_S"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.main = root / "main-checkout"

    def hook(self, body: str) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir(exist_ok=True)
        path = hooks / "pre-push"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        _git(self.main, "config", "core.hooksPath", str(hooks))

    def worktree(self, dispatch_id: str, dirty: bool = False) -> Path:
        wt = self.main / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(self.main, "worktree", "add", "-b", f"dispatch/{dispatch_id}", str(wt), "origin/main")
        if dirty:
            (wt / "alpha.py").write_text("print('uncommitted worker edit')\n")
        return wt


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Repo:
    monkeypatch.delenv(KEY, raising=False)
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


def _enforce(repo: _Repo, wt: Path, dispatch_id: str):
    receipts: "list[dict]" = []
    with patch("append_receipt.append_receipt_payload",
               side_effect=lambda payload, **kw: receipts.append(dict(payload))):
        result = pe.enforce_pr_exists(
            dispatch_id=dispatch_id, branch=f"dispatch/{dispatch_id}", worktree_state="dirty",
            repo_root=repo.main, receipts_file="/tmp/does-not-matter.ndjson",
            pr_title="t", pr_body="b", wt_path=wt, skip_pr=True,
        )
    return result, receipts


def _teardown(repo: _Repo, dispatch_id: str) -> None:
    dwi.remove_dispatch_worktree(dispatch_id, project_root=repo.main, data_dir=repo.root / "data")


def test_slow_hook_beyond_timeout_fails_naming_the_key(repo, monkeypatch):
    repo.hook("sleep 3")
    wt = repo.worktree("t1", dirty=True)
    _git(wt, "commit", "-am", "x")
    monkeypatch.setenv(KEY, "1")
    outcome = pe._push_branch(branch="dispatch/t1", cwd=wt)
    assert outcome.ok is False
    assert "timed out after 1s" in outcome.reason and KEY in outcome.reason


def test_slow_hook_within_timeout_still_pushes(repo, monkeypatch):
    repo.hook("sleep 2")
    wt = repo.worktree("t2", dirty=True)
    _git(wt, "commit", "-am", "x")
    monkeypatch.setenv(KEY, "30")
    assert pe._push_branch(branch="dispatch/t2", cwd=wt).ok is True


def test_default_timeout_is_900(repo):
    proc = subprocess.CompletedProcess([], 0, "", "")
    with patch.object(pe.subprocess, "run", return_value=proc) as run:
        assert pe._push_branch(branch="dispatch/t3", cwd=repo.main).ok is True
    assert run.call_args.kwargs["timeout"] == 900


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "1.5"])
def test_invalid_value_falls_back_to_default(repo, monkeypatch, bad):
    monkeypatch.setenv(KEY, bad)
    assert pe._salvage_push_timeout_s() == 900


def test_failed_salvage_push_keeps_worktree_and_receipt_names_it(repo):
    repo.hook("echo refused >&2; exit 1")
    wt = repo.worktree("t4", dirty=True)
    result, receipts = _enforce(repo, wt, "t4")
    assert result.ok is False and result.pushed is False
    assert receipts[0]["worktree_kept"] == str(wt)
    assert "not on origin" in receipts[0]["worktree_kept_reason"]
    _teardown(repo, "t4")
    assert wt.exists(), "the only checkout of the salvage commit must survive teardown"


def test_successful_salvage_push_removes_worktree_as_before(repo):
    wt = repo.worktree("t5", dirty=True)
    result, receipts = _enforce(repo, wt, "t5")
    assert result.pushed is True
    assert all("worktree_kept" not in r for r in receipts)
    _teardown(repo, "t5")
    assert not wt.exists()


def test_plain_committed_worktree_is_still_removed(repo):
    """Only a salvage commit triggers the keep; an ordinary local commit is reaped as before."""
    wt = repo.worktree("t6", dirty=True)
    _git(wt, "commit", "-am", "worker: edit")
    _teardown(repo, "t6")
    assert not wt.exists()


def test_unpublished_salvage_commit_fails_closed_on_unreadable_remote_check(repo):
    wt = repo.worktree("t7", dirty=True)
    _git(wt, "commit", "-am", f"{pe.SALVAGE_COMMIT_SUBJECT_PREFIX} test")
    assert pe.unpublished_salvage_commit(wt) == _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert pe.unpublished_salvage_commit(repo.root / "missing") is None
