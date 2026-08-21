"""test_oi1392_pr_enforcement_work_ref.py — OI-1392 regression.

A dispatch spec can carry a ``work_ref`` — the branch a fix-forward dispatch
delivers onto. ``dispatch_bridge.py`` has supported that field since OI-1137,
and phantom_guard already honors it. PR *enforcement*
(``pr_enforcement.enforce_pr_exists``, the tmux lane's ``_enforce_pr_exists``)
never did: it always enforced push+PR against the worktree's own branch — the
checked-out name OI-1372 (#1623) resolves, which is STILL ``dispatch/<id>``
when the worker never checks out anything else locally.

On 21-08 that produced #1631 and #1632: a worker committed on its own
``dispatch/<id>`` branch AND separately pushed the same sha onto its declared
``work_ref``. Enforcement classified ``dispatch/<id>`` as ``committed`` (never
pushed under that name), pushed it, and opened a SECOND PR against a base
that was already behind main — merging it reverted already-merged work.

This is the inversion PR #1623 (OI-1372) does NOT cover: before #1623 a
worker delivering off ``dispatch/<id>`` produced a false FAILURE (safe: no
merge risk). After #1623, given a work_ref target, it can produce a false
SUCCESS with a real second PR (unsafe: reverts work on merge). Fixing this
must not regress #1623 — test 3 below is the explicit proof it doesn't.

Real git repos throughout (mirrors test_oi1372_pr_enforcement_branch_resolution.py);
only ``gh_pr_ensure``'s gh-boundary functions are mocked — nothing here
touches GitHub.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tmux_worktree
from tmux_worktree import allocate, classify


# ---------------------------------------------------------------------------
# Shared real-git fixture (mirrors test_oi1372_pr_enforcement_branch_resolution.py)
# ---------------------------------------------------------------------------

def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "checkout", "-b", "main"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    readme = local / "README.md"
    readme.write_text("init\n")
    subprocess.run(["git", "-C", str(local), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return local


def _commit_on_own_branch(wt_path: Path, filename: str) -> None:
    """Worker commits WITHOUT checking out anything else — stays on the
    worktree's own dispatch/<id> branch, exactly the case OI-1372's
    checked-out-branch resolution cannot see."""
    (wt_path / filename).write_text("real work\n")
    subprocess.run(["git", "-C", str(wt_path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "worker commit"], check=True, capture_output=True,
    )


def _commit_on_new_branch(wt_path: Path, branch: str, filename: str) -> None:
    """OI-1372 case: the worker checks out *branch* and commits real work there."""
    subprocess.run(
        ["git", "-C", str(wt_path), "checkout", "-b", branch], check=True, capture_output=True,
    )
    (wt_path / filename).write_text("real work\n")
    subprocess.run(["git", "-C", str(wt_path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "worker commit"], check=True, capture_output=True,
    )


def _remote_branch_exists(repo_root: Path, branch: str) -> bool:
    ls = subprocess.run(
        ["git", "-C", str(repo_root), "ls-remote", "origin", f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    )
    return bool(ls.stdout.strip())


def _tmux_dispatch_instance(tmp_path: Path, project_root: Path):
    from tmux_interactive_dispatch import TmuxInteractiveDispatch
    return TmuxInteractiveDispatch(
        project_root=project_root,
        state_dir=tmp_path / "state",
        receipts_file=tmp_path / "state" / "t0_receipts.ndjson",
    )


# ---------------------------------------------------------------------------
# Test 1 (verplicht): work_ref set, worker commits on OWN dispatch branch AND
# pushes the same sha to the doelbranch — exactly one branch, exactly one PR.
# ---------------------------------------------------------------------------

def test_work_ref_present_worker_delivers_to_target_exactly_one_branch_one_pr(
    tmp_path, monkeypatch,
):
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1392-a", repo_root=local)

    # Worker commits on its own dispatch/<id> branch (never checks out
    # anything else) ...
    _commit_on_own_branch(handle.path, "work.txt")
    # ... AND separately pushes that exact sha onto the doelbranch (work_ref) —
    # dispatch/oi1392-a itself is never pushed.
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "origin", "HEAD:refs/heads/work/oi1392-target"],
        check=True, capture_output=True,
    )

    inst = _tmux_dispatch_instance(tmp_path, local)
    state = classify(handle)
    assert state == "committed", "dispatch/oi1392-a was never pushed under its own name"

    monkeypatch.setenv("VNX_WORK_REF", "work/oi1392-target")

    with patch("gh_pr_ensure.find_open_pr", return_value=None), \
         patch("gh_pr_ensure.create_pr", return_value=6001) as mock_create:
        result = inst._enforce_pr_exists(
            dispatch_id="oi1392-a", label="T1", worktree_handle=handle, worktree_state=state,
        )

    assert result.applicable is True
    assert result.ok is True, result.reason
    assert result.pr_number == 6001
    assert result.created is True

    mock_create.assert_called_once()
    created_branch = mock_create.call_args.args[0] if mock_create.call_args.args \
        else mock_create.call_args.kwargs.get("branch")
    assert created_branch == "work/oi1392-target", (
        f"PR must be opened against work_ref, got {created_branch!r}"
    )

    # Precisely ONE branch beyond main: dispatch/oi1392-a must NEVER have been
    # pushed — that would be the second branch this fix forbids.
    assert not _remote_branch_exists(local, "dispatch/oi1392-a"), (
        "a second branch (the worktree's own dispatch/<id>) must never be pushed "
        "when work_ref is set"
    )
    assert _remote_branch_exists(local, "work/oi1392-target")


# ---------------------------------------------------------------------------
# Test 2 (verplicht): work_ref set, a PR is ALREADY open for it — nothing
# is created.
# ---------------------------------------------------------------------------

def test_work_ref_pr_already_open_creates_nothing(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1392-b", repo_root=local)

    _commit_on_own_branch(handle.path, "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "origin", "HEAD:refs/heads/work/oi1392-existing"],
        check=True, capture_output=True,
    )

    inst = _tmux_dispatch_instance(tmp_path, local)
    state = classify(handle)
    assert state == "committed"

    monkeypatch.setenv("VNX_WORK_REF", "work/oi1392-existing")

    def _boom(*a, **kw):
        raise AssertionError("gh pr create must never run when a PR already exists for work_ref")

    with patch("gh_pr_ensure.find_open_pr", return_value=4321), \
         patch("gh_pr_ensure.create_pr", side_effect=_boom) as mock_create:
        result = inst._enforce_pr_exists(
            dispatch_id="oi1392-b", label="T1", worktree_handle=handle, worktree_state=state,
        )

    assert result.applicable is True
    assert result.ok is True, result.reason
    assert result.pr_number == 4321
    assert result.created is False
    mock_create.assert_not_called()

    assert not _remote_branch_exists(local, "dispatch/oi1392-b"), (
        "no second branch may be pushed even when a PR already exists for work_ref"
    )


# ---------------------------------------------------------------------------
# Test 3 (verplicht): WITHOUT work_ref, behavior is byte-identical to today —
# including the exact case #1623 (OI-1372) fixed: a worker that pushes to a
# branch other than dispatch/<id> must NOT be rejected as dispatch_branch_no_pr.
# ---------------------------------------------------------------------------

def test_no_work_ref_unchanged_behavior_oi1623_custom_branch_still_fixed(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("VNX_WORK_REF", raising=False)

    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1392-c", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/oi1392-no-work-ref", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/oi1392-no-work-ref"],
        check=True, capture_output=True,
    )

    inst = _tmux_dispatch_instance(tmp_path, local)
    state = classify(handle)
    assert state == "pushed"

    with patch(
        "gh_pr_ensure.ensure_pr",
        return_value={"pr_number": 8181, "created": True, "reason": None},
    ) as mock_ensure:
        result = inst._enforce_pr_exists(
            dispatch_id="oi1392-c", label="T1", worktree_handle=handle, worktree_state=state,
        )

    assert result.applicable is True
    assert result.ok is True, (
        f"must not regress to dispatch_branch_no_pr (OI-1372/#1623): {result.reason}"
    )
    assert result.pr_number == 8181

    mock_ensure.assert_called_once()
    called_branch = mock_ensure.call_args.args[0] if mock_ensure.call_args.args \
        else mock_ensure.call_args.kwargs.get("branch")
    assert called_branch == "fix/oi1392-no-work-ref", (
        f"PR must still be opened against the branch actually pushed, got {called_branch!r}"
    )
