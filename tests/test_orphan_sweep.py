"""tests/test_orphan_sweep.py — OI-1192 orphan-sweep contract tests.

Covers the three requirements from the dispatch, per kind:

  * an orphan is RECOGNIZED and cleaned/marked (dead tmux pane -> killed;
    session-gone clean worktree -> reaped; dead-PID active manifest -> recovered),
  * a LIVE dispatch is LEFT ALONE (alive/unknown pane; surviving session;
    protected invoking-dispatch id),
  * a worktree with uncommitted work is MARKED (git worktree lock), never deleted.

The tmux-facing backends (list_sessions / probe_liveness / kill_session) and the
PID predicate are injected, so kind-1 and kind-3 tests are deterministic and
never touch real tmux or real process IDs. Kind-2 tests use a REAL git repo in a
tmpdir (mirroring test_tmux_worktree.py) so classify_path/reap run their real
code path — the exact teardown path the sweep reuses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
# Unconditional inserts (mirrors test_crash_recovery_sweep.py): scripts/lib is
# frequently ALREADY on sys.path (installed package + conftest), so a
# `not in sys.path` guard would skip the front-insert and leave scripts/ ahead,
# resolving `orphan_sweep` to the CLI instead of the lib module.
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

import orphan_sweep as osweep  # noqa: E402  (the lib module)
import tmux_worktree  # noqa: E402

# scripts/ and scripts/lib both define an orphan_sweep module; the lib one wins
# for `import orphan_sweep` (scripts/lib is inserted last). Import the CLI
# explicitly by file to test main().
import importlib.util as _ilu  # noqa: E402

_cli_spec = _ilu.spec_from_file_location(
    "orphan_sweep_cli", str(_SCRIPT_DIR / "orphan_sweep.py")
)
osweep_cli = _ilu.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(osweep_cli)


@pytest.fixture
def env(tmp_path):
    """Pin runtime dirs to a per-test tmp tree; clear the current-dispatch fence.

    The current-dispatch fence (VNX_CURRENT_DISPATCH_ID) is exported by the
    dispatch worker and inherited by pytest, so it must be cleared here or the
    default would protect a real dispatch id in every test.
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


def _make_active(env: Path, dispatch_id: str, *, worker_pid=None):
    d = env / "dispatches" / "active" / dispatch_id
    d.mkdir(parents=True)
    manifest = {"dispatch_id": dispatch_id, "terminal": "T1", "model": "sonnet"}
    if worker_pid is not None:
        manifest["worker_pid"] = worker_pid
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


# ---------------------------------------------------------------------------
# Kind 1 — tmux sessions (pure fakes)
# ---------------------------------------------------------------------------

def test_dead_pane_session_killed(env):
    killed = []
    res = osweep.sweep(
        data_dir=env,
        list_sessions=lambda: ["vnx-abc-1", "NotVnx"],
        probe_liveness=lambda s: False,
        kill_session=lambda s: (killed.append(s), True)[1],
    )
    assert res.tmux_killed == ["vnx-abc-1"]
    assert killed == ["vnx-abc-1"]
    assert res.tmux_skipped_alive == []
    # Non-vnx session names are not VNX dispatch sessions -> ignored.
    assert "NotVnx" not in res.tmux_sessions_scanned


def test_alive_session_left_alone(env):
    res = osweep.sweep(
        data_dir=env,
        list_sessions=lambda: ["vnx-abc-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: True,
    )
    assert res.tmux_killed == []
    assert res.tmux_skipped_alive == ["vnx-abc-1"]


def test_unknown_liveness_fails_open(env):
    """'Cannot measure' (None) is never read as 'dead'."""
    res = osweep.sweep(
        data_dir=env,
        list_sessions=lambda: ["vnx-abc-1"],
        probe_liveness=lambda s: None,
        kill_session=lambda s: True,
    )
    assert res.tmux_killed == []
    assert res.tmux_skipped_alive == ["vnx-abc-1"]


def test_protected_session_left_alone(env):
    res = osweep.sweep(
        data_dir=env,
        current_dispatch_id="abc-1",
        list_sessions=lambda: ["vnx-abc-1"],
        probe_liveness=lambda s: False,  # even provably dead
        kill_session=lambda s: True,
    )
    assert res.tmux_killed == []
    assert res.tmux_skipped_protected == ["vnx-abc-1"]


# ---------------------------------------------------------------------------
# Kind 2 — worktrees (real git repo + real classify/reap)
# ---------------------------------------------------------------------------

def test_clean_orphan_worktree_reaped(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "clean-1")
    assert handle.path.is_dir()

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert str(handle.path) in res.worktrees_removed
    assert not handle.path.exists()
    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", "dispatch/clean-1"],
        text=True,
    ).strip()
    assert "dispatch/clean-1" not in branches


def test_live_worktree_left_alone(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "live-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-live-1"],
        probe_liveness=lambda s: True,
    )

    assert str(handle.path) in res.worktrees_skipped_live
    assert handle.path.is_dir()
    assert res.worktrees_removed == []


def test_dirty_orphan_worktree_marked_not_deleted(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "dirty-1")
    (handle.path / "uncommitted.txt").write_text("wip\n")

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    # Marked (locked), never deleted: uncommitted work is preserved verbatim.
    assert str(handle.path) in res.worktrees_preserved
    assert handle.path.is_dir()
    assert (handle.path / "uncommitted.txt").exists()
    assert res.worktrees_removed == []
    porcelain = subprocess.check_output(
        ["git", "-C", str(local), "worktree", "list", "--porcelain"],
        text=True,
    )
    assert "locked" in porcelain


def test_protected_worktree_left_alone(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "prot-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        current_dispatch_id="prot-1",
        list_sessions=lambda: [],  # no session -> would be orphan without the fence
    )

    assert str(handle.path) in res.worktrees_skipped_live
    assert handle.path.is_dir()


def test_sweep_is_idempotent(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "idem-1")

    first = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])
    assert str(handle.path) in first.worktrees_removed

    second = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])
    assert second.worktrees_scanned == []
    assert second.worktrees_removed == []
    assert second.errors == []


# ---------------------------------------------------------------------------
# Kind 3 — active manifests (delegated to crash_recovery_sweep)
# ---------------------------------------------------------------------------

def test_active_manifest_recovered_via_delegation(env):
    _make_active(env, "act-1", worker_pid=999999)

    res = osweep.sweep(
        data_dir=env, list_sessions=lambda: [], pid_alive=lambda pid: False,
    )

    assert res.active_recovered == ["act-1"]
    assert not (env / "dispatches" / "active" / "act-1").exists()
    assert (env / "dispatches" / "dead_letter" / "act-1" / "manifest.json").exists()


def test_active_manifest_protected(env):
    _make_active(env, "act-1", worker_pid=999999)

    res = osweep.sweep(
        data_dir=env,
        current_dispatch_id="act-1",
        list_sessions=lambda: [],
        pid_alive=lambda pid: False,  # even provably dead
    )

    assert res.active_skipped_protected == ["act-1"]
    assert (env / "dispatches" / "active" / "act-1").exists()


# ---------------------------------------------------------------------------
# Dry-run + CLI
# ---------------------------------------------------------------------------

def test_dry_run_mutates_nothing(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "dry-1")
    _make_active(env, "dry-act", worker_pid=999999)
    killed = []

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        dry_run=True,
        list_sessions=lambda: ["vnx-dead-1"],
        probe_liveness=lambda s: False,
        kill_session=lambda s: (killed.append(s), True)[1],
        pid_alive=lambda pid: False,
    )

    assert res.dry_run
    assert res.tmux_killed == ["vnx-dead-1"]       # reported as would-kill
    assert killed == []                             # but not actually killed
    assert handle.path.is_dir()                     # worktree untouched
    assert (env / "dispatches" / "active" / "dry-act").exists()  # manifest untouched


def test_cli_dry_run_json(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "cli-1")
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = osweep_cli.main([
            "--repo-root", str(local),
            "--data-dir", str(env),
            "--dry-run", "--json",
        ])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["dry_run"] is True
    assert str(handle.path) in payload["worktrees_removed"]
    # Dry-run wrote nothing.
    assert handle.path.is_dir()


if __name__ == "__main__":
    import unittest

    raise SystemExit(pytest.main([__file__, "-q"]))
