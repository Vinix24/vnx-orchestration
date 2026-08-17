"""tests/test_orphan_sweep_fail_open.py — OI-1286 fail-closed listing regression tests.

``orphan_sweep._real_list_sessions()`` collapses EVERY non-zero ``tmux
list-sessions`` outcome to an empty list: tmux not installed, an
OSError/TimeoutExpired from the runner, AND a legitimate "no server running"
(real zero sessions) all read as ``[]``, indistinguishable from each other.
``sweep()`` then treats that empty list as "no tmux sessions exist" and
proceeds to reap every non-protected worktree it finds under
``.vnx-data/worktrees/`` — even though the listing was never actually taken.

The module docstring's fail-open promise ("'cannot measure' is never read as
'dead'") does not hold for this path. These tests pin the two directions the
eventual fix must satisfy without collapsing into each other:

  * NOT MEASURABLE (tmux missing; OSError/TimeoutExpired from the runner) ->
    worktrees must be LEFT ALONE and an error must land in the result. RED on
    main: today they get reaped and no error is recorded.
  * REALLY ZERO (rc=1, stderr contains "no server running") -> sweep proceeds
    exactly as it does today. GREEN on main and must stay green after any
    fix — the regression guard against a fix that fails closed too broadly.

Per the dispatch: ``list_sessions`` is NOT injected here. Injecting it would
bypass ``_real_list_sessions`` entirely and test nothing about the defect.
Instead the process boundary is stubbed: ``shutil.which`` and the tmux runner
(``orphan_sweep._adapter_run_tmux``, i.e. ``tmux_adapter._run_tmux``) — the
real ``_real_list_sessions`` runs in every test below.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
# Unconditional inserts (mirrors test_orphan_sweep.py): scripts/lib is
# frequently ALREADY on sys.path (installed package + conftest), so a
# `not in sys.path` guard would skip the front-insert and leave scripts/ ahead,
# resolving `orphan_sweep` to the CLI instead of the lib module.
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

import orphan_sweep as osweep  # noqa: E402  (the lib module)
import tmux_worktree  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """Pin runtime dirs to a per-test tmp tree; clear the current-dispatch fence.

    Mirrors ``tests/test_orphan_sweep.py::env``. The current-dispatch fence
    (``VNX_CURRENT_DISPATCH_ID``) is exported by the dispatch worker and
    inherited by pytest, so it must be cleared here or the default would
    protect a real dispatch id in every test.
    """
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    active = data_dir / "dispatches" / "active"
    for d in (data_dir, state_dir, reports_dir, active):
        d.mkdir(parents=True)
    keys = (
        "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR",
        "VNX_REPORTS_DIR", "VNX_PROJECT_ID", "VNX_CURRENT_DISPATCH_ID",
    )
    orig = {k: os.environ.get(k) for k in keys}
    os.environ["VNX_DATA_DIR"] = str(data_dir)
    os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
    os.environ["VNX_STATE_DIR"] = str(state_dir)
    os.environ["VNX_REPORTS_DIR"] = str(reports_dir)
    os.environ["VNX_PROJECT_ID"] = "vnx-dev"
    os.environ.pop("VNX_CURRENT_DISPATCH_ID", None)
    yield data_dir
    for k, v in orig.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _git_repo(tmp_path: Path) -> Path:
    """Bare origin + local clone with an initial commit (mirrors test_tmux_worktree)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (local / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(local), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return local


def _alloc(local: Path, dispatch_id: str) -> tmux_worktree.WorktreeHandle:
    return tmux_worktree.allocate(dispatch_id, repo_root=local)


# ---------------------------------------------------------------------------
# Direction 1 — NOT MEASURABLE: worktrees must be left alone, error recorded.
# RED on main: today these get silently reaped, no error recorded.
# ---------------------------------------------------------------------------

def test_tmux_not_installed_worktree_survives_and_is_errored(env, tmp_path, monkeypatch):
    """rc=127 'tmux not found' is unmeasurable, not 'zero sessions' — must not reap."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "no-tmux-1")
    assert handle.path.is_dir()

    monkeypatch.setattr(osweep.shutil, "which", lambda name: None)

    res = osweep.sweep(repo_root=local, data_dir=env)

    assert handle.path.is_dir(), (
        "worktree was reaped even though the tmux listing was never "
        "measurable (tmux not installed) — fail-open contract violated"
    )
    assert str(handle.path) not in res.worktrees_removed
    assert res.errors, (
        "an unmeasurable tmux listing must surface as an error in the "
        "result, not pass silently as 'zero sessions'"
    )


def test_tmux_runner_oserror_worktree_survives_and_is_errored(env, tmp_path, monkeypatch):
    """An OSError from the tmux runner is unmeasurable, not 'zero sessions' — must not reap."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "runner-oserror-1")
    assert handle.path.is_dir()

    def fake_run_tmux(*args, **kwargs):
        raise OSError("Resource temporarily unavailable")

    monkeypatch.setattr(osweep.shutil, "which", lambda name: "/usr/local/bin/tmux")
    monkeypatch.setattr(osweep, "_adapter_run_tmux", fake_run_tmux)

    res = osweep.sweep(repo_root=local, data_dir=env)

    assert handle.path.is_dir(), (
        "worktree was reaped even though the tmux runner raised OSError — "
        "fail-open contract violated"
    )
    assert str(handle.path) not in res.worktrees_removed
    assert res.errors, (
        "a tmux runner OSError must surface as an error in the result, "
        "not pass silently as 'zero sessions'"
    )


def test_tmux_runner_timeout_worktree_survives_and_is_errored(env, tmp_path, monkeypatch):
    """A TimeoutExpired from the tmux runner is unmeasurable, not 'zero sessions' — must not reap."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "runner-timeout-1")
    assert handle.path.is_dir()

    def fake_run_tmux(*args, timeout=10, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["tmux", "list-sessions"], timeout=timeout)

    monkeypatch.setattr(osweep.shutil, "which", lambda name: "/usr/local/bin/tmux")
    monkeypatch.setattr(osweep, "_adapter_run_tmux", fake_run_tmux)

    res = osweep.sweep(repo_root=local, data_dir=env)

    assert handle.path.is_dir(), (
        "worktree was reaped even though the tmux runner raised "
        "TimeoutExpired — fail-open contract violated"
    )
    assert str(handle.path) not in res.worktrees_removed
    assert res.errors, (
        "a tmux runner TimeoutExpired must surface as an error in the "
        "result, not pass silently as 'zero sessions'"
    )


# ---------------------------------------------------------------------------
# Direction 2 — REALLY ZERO: rc=1 "no server running" is a real empty listing.
# GREEN on main; must stay green — regression guard against an over-eager
# fail-closed fix that also fences off a legitimately empty listing.
# ---------------------------------------------------------------------------

def test_no_server_running_is_real_zero_sessions_worktree_still_reaped(env, tmp_path, monkeypatch):
    """rc=1 with 'no server running' in stderr means truly zero sessions — sweep proceeds."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "no-server-1")

    def fake_run_tmux(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=("tmux", *args),
            returncode=1,
            stdout="",
            stderr="no server running on /tmp/tmux-501/default",
        )

    monkeypatch.setattr(osweep.shutil, "which", lambda name: "/usr/local/bin/tmux")
    monkeypatch.setattr(osweep, "_adapter_run_tmux", fake_run_tmux)

    res = osweep.sweep(repo_root=local, data_dir=env)

    assert str(handle.path) in res.worktrees_removed
    assert not handle.path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
