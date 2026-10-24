"""A gate seat's provider worktree must not outlive its seat.

Field evidence 2026-10-24: eleven ``dispatch-{glm,kimi,deepseek}-gate-*``
worktrees left behind under ``.vnx-data/worktrees/``.  The provider-lane child
(``provider_dispatch.py``) creates its own isolated worktree and removes it in
its own ``finally``; a child SIGKILLed by ``subprocess.run(timeout=...)`` never
reaches that ``finally``, and the parent holds no handle to the tree.  The
parent-side fix is ``plan_gate_panel._run_provider_lane_child``'s ``finally`` ->
``dispatch_worktree_isolation.remove_dispatch_worktree_if_present``.

``test_timed_out_provider_seat_worktree_is_reaped`` is the red-run test: on
``origin/main`` the worktree path still exists after the timeout.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_worktree_isolation as dwi
import plan_gate_panel


def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Bare origin + local clone with one commit; returns the local clone.

    Mirrors ``test_provider_dispatch_worktree_isolation``'s fixture so the real
    ``create_dispatch_worktree`` can resolve ``origin/main``.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    (local / "README.md").write_text("init\n")
    for args in (
        ("checkout", "-b", "main"),
        ("config", "user.email", "test@test.local"),
        ("config", "user.name", "Test"),
        ("add", "README.md"),
        ("commit", "-m", "initial"),
        ("push", "-u", "origin", "main"),
    ):
        subprocess.run(["git", "-C", str(local), *args], check=True, capture_output=True)
    return local


def _point_state_at_tmp(tmp_path: Path, monkeypatch) -> Path:
    """Pin the shared claim registry at a temp state root (never the real one)."""
    data_dir = tmp_path / "vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _dispatch_id_from_cmd(cmd) -> str:
    """The ``--dispatch-id`` the real provider_dispatch child would receive."""
    return cmd[cmd.index("--dispatch-id") + 1]


def _install_fake_seat(monkeypatch, fake_run) -> None:
    """Replace only plan_gate_panel's ``subprocess``, so the fake child's own
    git calls (``create_dispatch_worktree``) still run for real."""
    monkeypatch.setattr(
        plan_gate_panel,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def _dispatcher(data_dir: Path):
    return plan_gate_panel._make_default_dispatcher(str(data_dir), 1)


def _git(local: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(local), *args], capture_output=True, text=True, check=False,
    )


def test_timed_out_provider_seat_worktree_is_reaped(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    data_dir = _point_state_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)

    created = {}

    def fake_run(cmd, **kwargs):
        # Like provider_dispatch: create the isolated worktree from the
        # --dispatch-id handed over, then be SIGKILLed by the parent's timeout.
        created["wt"] = dwi.create_dispatch_worktree(
            _dispatch_id_from_cmd(cmd), project_root=local,
        )
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        _dispatcher(data_dir)("glm", "glm-4.6", "instruction", "glm-gate-timeout-1")

    wt_path = created["wt"]
    assert not wt_path.exists(), f"gate seat's worktree survived its own kill: {wt_path}"
    listed = _git(local, "worktree", "list", "--porcelain").stdout
    assert str(wt_path) not in listed


def test_absent_worktree_returns_false_and_runs_no_git(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    _point_state_at_tmp(tmp_path, monkeypatch)

    git_calls = []
    real_run = subprocess.run

    def counting_run(args, *a, **kwargs):
        git_calls.append(list(args))
        return real_run(args, *a, **kwargs)

    monkeypatch.setattr(dwi.subprocess, "run", counting_run)
    teardowns = []
    monkeypatch.setattr(
        dwi, "remove_dispatch_worktree",
        lambda *a, **kwargs: teardowns.append((a, kwargs)),
    )

    assert dwi.remove_dispatch_worktree_if_present(
        "never-created-gate", project_root=local,
    ) is False
    assert git_calls == [], f"absent worktree must cost no git call: {git_calls}"
    assert teardowns == [], "teardown must not run for an absent worktree"


def test_local_commit_survives_a_timed_out_seat(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    data_dir = _point_state_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)

    created = {}

    def fake_run(cmd, **kwargs):
        wt_path = dwi.create_dispatch_worktree(
            _dispatch_id_from_cmd(cmd), project_root=local,
        )
        (wt_path / "work.txt").write_text("real work\n")
        for args in (("add", "work.txt"), ("commit", "-m", "real local work")):
            subprocess.run(["git", "-C", str(wt_path), *args], check=True, capture_output=True)
        created["head"] = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        _dispatcher(data_dir)("kimi", "kimi-k2", "instruction", "kimi-gate-commit-1")

    branch = f"dispatch/{dwi._sanitize_dispatch_id('kimi-gate-commit-1')}"
    head = _git(local, "rev-parse", branch)
    assert head.returncode == 0, f"committed work lost: {branch} does not resolve: {head.stderr}"
    assert head.stdout.strip() == created["head"]
    assert _git(local, "show", f"{branch}:work.txt").stdout == "real work\n"


def test_helper_swallows_a_teardown_exception(tmp_path, monkeypatch, caplog):
    local = _init_git_repo_with_origin(tmp_path)
    _point_state_at_tmp(tmp_path, monkeypatch)

    dispatch_id = "glm-gate-boom"
    wt_path = dwi._dispatch_worktree_dir(local.resolve(), dispatch_id)
    wt_path.mkdir(parents=True)

    def boom(*_a, **_kwargs):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(dwi, "remove_dispatch_worktree", boom)

    with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
        assert dwi.remove_dispatch_worktree_if_present(
            dispatch_id, project_root=local,
        ) is False
    assert dispatch_id in caplog.text
    assert str(wt_path) in caplog.text


def test_timeout_still_propagates_when_teardown_raises(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    data_dir = _point_state_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)

    created = {}

    def fake_run(cmd, **kwargs):
        created["wt"] = dwi.create_dispatch_worktree(
            _dispatch_id_from_cmd(cmd), project_root=local,
        )
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)

    def boom(*_a, **_kwargs):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(dwi, "remove_dispatch_worktree", boom)

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _dispatcher(data_dir)("glm", "glm-4.6", "instruction", "glm-gate-boom-2")
    finally:
        # The patched teardown never released the occupancy lock it would have.
        dwi._release_occupancy(created["wt"])


def test_normally_exiting_seat_costs_no_second_teardown(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    data_dir = _point_state_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda *a, **k: "panel seat analysis")
    real_remove = dwi.remove_dispatch_worktree

    def fake_run(cmd, **kwargs):
        dispatch_id = _dispatch_id_from_cmd(cmd)
        wt = dwi.create_dispatch_worktree(dispatch_id, project_root=local)
        real_remove(dispatch_id, project_root=local)  # the child's own finally
        assert not wt.exists()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _install_fake_seat(monkeypatch, fake_run)
    teardowns = []
    monkeypatch.setattr(dwi, "remove_dispatch_worktree", lambda *a, **k: teardowns.append(a))

    assert _dispatcher(data_dir)("glm", "glm-4.6", "prompt", "glm-gate-clean-1") == (
        "panel seat analysis"
    )
    assert teardowns == [], "the parent must not re-tear-down a tree the child already removed"
