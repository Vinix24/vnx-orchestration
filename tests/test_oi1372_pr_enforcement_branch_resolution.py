"""test_oi1372_pr_enforcement_branch_resolution.py — OI-1372 regression.

On 20-08 two dispatches delivered real, complete work (green tests, a commit,
a pushed branch, a real PR) and were BOTH booked as ``status=failure`` with
``error=dispatch_branch_no_pr``, because PR enforcement only ever looked at
``dispatch/<dispatch-id>`` — the branch a fresh worktree starts on — while the
dispatch instruction had prescribed its own branch name and the worker pushed
THAT branch instead. ``gh pr create --head dispatch/<id>`` then failed with
"No commits between main and dispatch/<id>", and the guard rejected a real
success.

The fix (mirrors ``envelope_govern_support._resolve_phantom_diff``'s OI-870
model): read the worktree's ACTUAL checked-out branch instead of trusting the
dispatch-id-derived name unconditionally — ``tmux_worktree.resolve_effective_branch``
and the ``classify_path`` refactor it shares logic with.

Real git repos throughout (mirrors test_tmux_worktree.py); only
``gh_pr_ensure.ensure_pr`` is mocked — nothing here touches GitHub.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tmux_worktree
from tmux_worktree import allocate, classify, resolve_effective_branch


# ---------------------------------------------------------------------------
# Shared real-git fixture (mirrors test_tmux_worktree.py::_init_git_repo_with_origin)
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


def _commit_on_new_branch(wt_path: Path, branch: str, filename: str) -> None:
    """Simulate a dispatch instruction that prescribed its own branch name:
    the worker checks out *branch* (never touching dispatch/<id> again) and
    commits real work there."""
    subprocess.run(
        ["git", "-C", str(wt_path), "checkout", "-b", branch], check=True, capture_output=True,
    )
    (wt_path / filename).write_text("real work\n")
    subprocess.run(["git", "-C", str(wt_path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt_path), "commit", "-m", "worker commit"], check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Mechanism level: resolve_effective_branch / classify_path
# ---------------------------------------------------------------------------

def test_resolve_effective_branch_returns_custom_name_when_pushed(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-a", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/report-wrapper-red-tests", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/report-wrapper-red-tests"],
        check=True, capture_output=True,
    )

    effective = resolve_effective_branch(
        wt=handle.path, expected_branch=handle.branch, dispatch_id="oi1372-a",
    )
    assert effective == "fix/report-wrapper-red-tests"


def test_resolve_effective_branch_preserves_cross_dispatch_drift_safety(tmp_path):
    """OI-1124 safety net stays intact: a worktree checked out on a DIFFERENT
    dispatch's branch must NOT be redirected — resolve_effective_branch keeps
    returning the expected (dispatch/<id>) name so classify_path's own guard
    still classifies it 'dirty', never silently substituting the foreign
    identity."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-victim", repo_root=local)
    subprocess.run(
        ["git", "-C", str(handle.path), "checkout", "-b", "dispatch/oi1372-donor"],
        check=True, capture_output=True,
    )

    effective = resolve_effective_branch(
        wt=handle.path, expected_branch=handle.branch, dispatch_id="oi1372-victim",
    )
    assert effective == handle.branch  # NOT redirected to dispatch/oi1372-donor
    assert classify(handle) == "dirty"  # classify_path's own guard still fires


def test_classify_reports_pushed_not_committed_on_custom_branch(tmp_path):
    """THE bug, at the classification layer: before OI-1372, classify_path
    checked ls-remote against dispatch/<id> (never pushed) and misclassified a
    pushed custom branch as 'committed' — pr_enforcement then tried to push+PR
    dispatch/<id> itself, which carries no commits (the literal "No commits
    between main and dispatch/<id>" gh error from the field report)."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-b", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/report-wrapper-red-tests", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/report-wrapper-red-tests"],
        check=True, capture_output=True,
    )
    assert classify(handle) == "pushed"


# ---------------------------------------------------------------------------
# Chokepoint level, tmux lane (the DEFAULT lane) — _enforce_pr_exists
# ---------------------------------------------------------------------------

def _tmux_dispatch_instance(tmp_path: Path, project_root: Path):
    from tmux_interactive_dispatch import TmuxInteractiveDispatch
    return TmuxInteractiveDispatch(
        project_root=project_root,
        state_dir=tmp_path / "state",
        receipts_file=tmp_path / "state" / "t0_receipts.ndjson",
    )


def test_tmux_lane_accepts_pushed_custom_branch_no_dispatch_branch_no_pr(tmp_path):
    """Requirement 1: a dispatch that pushed to a DIFFERENT branch with a real
    PR must NOT be status=failure, and dispatch_branch_no_pr must not occur."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-c", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/report-wrapper-red-tests", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/report-wrapper-red-tests"],
        check=True, capture_output=True,
    )

    inst = _tmux_dispatch_instance(tmp_path, local)
    state = classify(handle)
    assert state == "pushed"

    with patch(
        "gh_pr_ensure.ensure_pr",
        return_value={"pr_number": 4242, "created": True, "reason": None},
    ) as mock_ensure:
        result = inst._enforce_pr_exists(
            dispatch_id="oi1372-c", label="T1", worktree_handle=handle, worktree_state=state,
        )

    assert result.applicable is True
    assert result.ok is True, f"must not reject as dispatch_branch_no_pr: {result.reason}"
    assert result.pr_number == 4242
    mock_ensure.assert_called_once()
    called_branch = mock_ensure.call_args.args[0] if mock_ensure.call_args.args \
        else mock_ensure.call_args.kwargs.get("branch")
    assert called_branch == "fix/report-wrapper-red-tests", (
        f"PR must be opened against the branch actually pushed, got {called_branch!r}"
    )


def test_tmux_lane_pr_creation_failure_on_custom_branch_is_still_loud(tmp_path):
    """Requirement 2 (guard preservation): the fix only relocates WHERE the
    guard looks — a real PR-creation failure on the (correctly resolved)
    custom branch must still be a loud, receipt-visible failure, not silently
    swallowed."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-e", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/report-wrapper-red-tests", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/report-wrapper-red-tests"],
        check=True, capture_output=True,
    )

    inst = _tmux_dispatch_instance(tmp_path, local)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    state = classify(handle)
    assert state == "pushed"

    with patch(
        "gh_pr_ensure.ensure_pr",
        return_value={"pr_number": None, "created": False, "reason": "gh auth expired"},
    ):
        result = inst._enforce_pr_exists(
            dispatch_id="oi1372-e", label="T1", worktree_handle=handle, worktree_state=state,
        )

    assert result.applicable is True
    assert result.ok is False
    assert result.reason and "gh auth expired" in result.reason


# ---------------------------------------------------------------------------
# Chokepoint level, envelope lane (codex/claude-subprocess) — _enforce_push_pr
# ---------------------------------------------------------------------------

def test_envelope_lane_accepts_pushed_custom_branch_no_dispatch_branch_no_pr(tmp_path):
    """Same requirement 1, exercised through dispatch_envelope._enforce_push_pr
    (the envelope-lane chokepoint pointed at by the dispatch instruction)."""
    import dispatch_envelope
    from envelope_types import _AdapterResult

    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("oi1372-f", repo_root=local)
    _commit_on_new_branch(handle.path, "fix/report-wrapper-red-tests", "work.txt")
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", "fix/report-wrapper-red-tests"],
        check=True, capture_output=True,
    )

    success_result = _AdapterResult(returncode=0, completion_text="done", status="success")

    with patch(
        "gh_pr_ensure.ensure_pr",
        return_value={"pr_number": 7777, "created": True, "reason": None},
    ) as mock_ensure:
        outcome = dispatch_envelope._enforce_push_pr(
            dispatch_id="oi1372-f",
            branch=handle.branch,
            wt_path=handle.path,
            repo_root=local,
            receipts_file=tmp_path / "t0_receipts.ndjson",
            result=success_result,
        )

    assert outcome.status == "success", (
        f"must not reject as dispatch_branch_no_pr: {outcome.error!r}"
    )
    mock_ensure.assert_called_once()
    called_branch = mock_ensure.call_args.args[0] if mock_ensure.call_args.args \
        else mock_ensure.call_args.kwargs.get("branch")
    assert called_branch == "fix/report-wrapper-red-tests"
