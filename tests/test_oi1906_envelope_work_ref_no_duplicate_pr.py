"""test_oi1906_envelope_work_ref_no_duplicate_pr.py — OI-1906 regression.

A fix-forward pushes ``HEAD:<work_ref>`` onto the real PR branch while its
worktree stays on a local name. ``dispatch_envelope._enforce_push_pr`` did not
pass ``work_ref`` to ``pr_enforcement.enforce_pr_exists``, so the envelope found
no PR for the local name and opened a second PR on the same sha.

Real git repos (bare origin); only ``gh_pr_ensure.find_open_pr`` / ``create_pr``
are patched — nothing here touches GitHub, the network or a ``~/.vnx-data`` store.
No central DB is read or written, so the ADR-007 project_id colliding-id case does
not apply to this test.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tmux_worktree
from dispatch_envelope import _enforce_push_pr
from envelope_types import _AdapterResult
from tmux_worktree import allocate

REAL_PR_BRANCH = "work/ff-real-pr"
LOCAL_NAME = "work-ff6"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    _git("checkout", "-b", "main", cwd=local)
    _git("config", "user.email", "test@test.local", cwd=local)
    _git("config", "user.name", "Test", cwd=local)
    (local / "README.md").write_text("init\n")
    _git("add", "README.md", cwd=local)
    _git("commit", "-m", "initial", cwd=local)
    _git("push", "-u", "origin", "main", cwd=local)
    return local


def _remote_branches(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {line.split("refs/heads/", 1)[1] for line in out.splitlines() if line.strip()}


def _worktree_on_local_name(tmp_path: Path, dispatch_id: str, push_to: "str | None"):
    local = _init_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate(dispatch_id, repo_root=local)
    _git("checkout", "-b", LOCAL_NAME, cwd=handle.path)
    (handle.path / "fix.txt").write_text("fix-forward\n")
    _git("add", "fix.txt", cwd=handle.path)
    _git("commit", "-m", "fix-forward commit", cwd=handle.path)
    if push_to:
        _git("push", "origin", f"HEAD:refs/heads/{push_to}", cwd=handle.path)
    return local, handle


def _run_enforce(tmp_path: Path, local: Path, handle, dispatch_id: str, work_ref):
    ok = _AdapterResult(returncode=0, completion_text="done", status="success", token_usage={})
    result = _enforce_push_pr(
        dispatch_id=dispatch_id,
        branch=handle.branch,
        wt_path=handle.path,
        repo_root=local,
        receipts_file=tmp_path / "state" / "t0_receipts.ndjson",
        result=ok,
        work_ref=work_ref,
    )
    return result


def test_fix_forward_with_open_pr_on_work_ref_opens_no_second_pr(tmp_path):
    dispatch_id = "oi1906-a"
    local, handle = _worktree_on_local_name(tmp_path, dispatch_id, push_to=REAL_PR_BRANCH)

    def _open_pr(branch, *a, **kw):
        return 1984 if branch == REAL_PR_BRANCH else None

    with patch("gh_pr_ensure.find_open_pr", side_effect=_open_pr), \
         patch("gh_pr_ensure.create_pr", return_value=9999) as mock_create:
        result = _run_enforce(tmp_path, local, handle, dispatch_id, work_ref=REAL_PR_BRANCH)

    assert result.status == "success"
    mock_create.assert_not_called()
    assert _remote_branches(local) == {"main", REAL_PR_BRANCH}, "local name must never be pushed"


def test_without_work_ref_behaviour_is_unchanged(tmp_path):
    dispatch_id = "oi1906-b"
    local, handle = _worktree_on_local_name(tmp_path, dispatch_id, push_to=REAL_PR_BRANCH)

    with patch("gh_pr_ensure.find_open_pr", return_value=None), \
         patch("gh_pr_ensure.create_pr", return_value=7777) as mock_create:
        result = _run_enforce(tmp_path, local, handle, dispatch_id, work_ref=None)

    assert result.status == "success"
    mock_create.assert_called_once()
    created = mock_create.call_args.args[0] if mock_create.call_args.args \
        else mock_create.call_args.kwargs.get("branch")
    assert created == LOCAL_NAME
    assert LOCAL_NAME in _remote_branches(local)


def test_work_ref_without_open_pr_creates_exactly_one_pr_for_work_ref(tmp_path):
    dispatch_id = "oi1906-c"
    local, handle = _worktree_on_local_name(tmp_path, dispatch_id, push_to=REAL_PR_BRANCH)

    with patch("gh_pr_ensure.find_open_pr", return_value=None), \
         patch("gh_pr_ensure.create_pr", return_value=8888) as mock_create:
        result = _run_enforce(tmp_path, local, handle, dispatch_id, work_ref=REAL_PR_BRANCH)

    assert result.status == "success"
    mock_create.assert_called_once()
    created = mock_create.call_args.args[0] if mock_create.call_args.args \
        else mock_create.call_args.kwargs.get("branch")
    assert created == REAL_PR_BRANCH
    assert LOCAL_NAME not in _remote_branches(local)
