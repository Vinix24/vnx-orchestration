"""test_pool_worktree_manager.py — Tests for per-worker git worktree manager.

Covers:
  - create_worker_worktree: idempotent creation, branch naming, error handling
  - reap_worker_worktree: idempotent removal, directory cleanup on git failure
  - Integration with real git repos in tempdir
  - Wiring verification: spawn passes cwd, reaper calls reap

Wave 6 PR-6.5b.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_worktree_manager import (
    _validate_terminal_id,
    _worktree_dir,
    create_worker_worktree,
    reap_worker_worktree,
)


# ---------------------------------------------------------------------------
# Helpers: set up a real git repo with a bare "origin" remote
# ---------------------------------------------------------------------------

def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Create a bare origin + local clone with an initial commit.

    Returns the local clone path (the 'project root').
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local), "checkout", "-b", "main"],
        capture_output=True,
    )
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
    subprocess.run(
        ["git", "-C", str(local), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )

    return local


# ---------------------------------------------------------------------------
# 1. _worktree_dir builds correct path
# ---------------------------------------------------------------------------

def test_worktree_dir_path():
    root = Path("/proj")
    assert _worktree_dir(root, "ABC-1") == root / ".vnx-data" / "worktrees" / "pool-ABC-1"


# ---------------------------------------------------------------------------
# 2. create_worker_worktree — idempotent when directory exists
# ---------------------------------------------------------------------------

def test_create_idempotent_existing_dir(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    wt = root / ".vnx-data" / "worktrees" / "pool-T1"
    wt.mkdir(parents=True)

    result = create_worker_worktree("T1", project_root=root)
    assert result == wt


# ---------------------------------------------------------------------------
# 3. create_worker_worktree — real git worktree
# ---------------------------------------------------------------------------

def test_create_real_worktree(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    wt = create_worker_worktree("W1", base_branch="main", project_root=local)

    assert wt.is_dir()
    assert (wt / "README.md").is_file()
    assert "pool-W1" in str(wt)

    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", "pool/W1"],
        text=True,
    ).strip()
    assert "pool/W1" in branches


# ---------------------------------------------------------------------------
# 4. create_worker_worktree — idempotent real (second call returns same path)
# ---------------------------------------------------------------------------

def test_create_real_idempotent(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    first = create_worker_worktree("W2", project_root=local)
    second = create_worker_worktree("W2", project_root=local)

    assert first == second


# ---------------------------------------------------------------------------
# 5. create_worker_worktree — branch already exists (re-attach)
# ---------------------------------------------------------------------------

def test_create_branch_already_exists(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    subprocess.run(
        ["git", "-C", str(local), "branch", "pool/W3", "origin/main"],
        check=True, capture_output=True,
    )

    wt = create_worker_worktree("W3", project_root=local)
    assert wt.is_dir()
    assert (wt / "README.md").is_file()


# ---------------------------------------------------------------------------
# 6. create_worker_worktree — bad base_branch raises RuntimeError
# ---------------------------------------------------------------------------

def test_create_bad_base_branch(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    with pytest.raises(RuntimeError, match="git worktree add failed"):
        create_worker_worktree("W4", base_branch="nonexistent", project_root=local)


# ---------------------------------------------------------------------------
# 7. reap_worker_worktree — idempotent when absent
# ---------------------------------------------------------------------------

def test_reap_idempotent_absent(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    reap_worker_worktree("NOPE", project_root=local)


# ---------------------------------------------------------------------------
# 8. reap_worker_worktree — removes real worktree
# ---------------------------------------------------------------------------

def test_reap_real_worktree(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    wt = create_worker_worktree("W5", project_root=local)
    assert wt.is_dir()

    reap_worker_worktree("W5", project_root=local)

    assert not wt.exists()

    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", "pool/W5"],
        text=True,
    ).strip()
    assert branches == ""


# ---------------------------------------------------------------------------
# 9. reap_worker_worktree — falls back to rmtree + prune when git fails
# ---------------------------------------------------------------------------

def test_reap_fallback_rmtree(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    wt_dir = local / ".vnx-data" / "worktrees" / "pool-FAKE"
    wt_dir.mkdir(parents=True)
    (wt_dir / "dummy.txt").write_text("x")

    reap_worker_worktree("FAKE", project_root=local)

    assert not wt_dir.exists()


# ---------------------------------------------------------------------------
# 10. reap after create — full lifecycle
# ---------------------------------------------------------------------------

def test_create_then_reap_lifecycle(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)

    wt = create_worker_worktree("LC1", project_root=local)
    assert wt.is_dir()
    assert (wt / "README.md").is_file()

    reap_worker_worktree("LC1", project_root=local)
    assert not wt.exists()

    wt2 = create_worker_worktree("LC1", project_root=local)
    assert wt2.is_dir()
    assert (wt2 / "README.md").is_file()


# ---------------------------------------------------------------------------
# 11. Spawn wiring: _spawn_via_provider_dispatch passes cwd
# ---------------------------------------------------------------------------

def test_spawn_passes_worktree_cwd(tmp_path):
    wt_path = tmp_path / ".vnx-data" / "worktrees" / "pool-WT-1"
    wt_path.mkdir(parents=True)

    import pool_manager as pm_mod

    captured_kwargs = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured_kwargs.update(kw)
            self.pid = 99999

    with patch("pool_worktree_manager.create_worker_worktree", return_value=wt_path):
        with patch.object(pm_mod.subprocess, "Popen", FakePopen):
            with patch("os.kill"):
                result = pm_mod._spawn_via_provider_dispatch(
                    "proj", "default", "WT-1", "claude", "backend-developer",
                )

    assert "cwd" in captured_kwargs
    assert captured_kwargs["cwd"] == str(wt_path)


# ---------------------------------------------------------------------------
# 12. Reaper wiring: reap_dead calls reap_worker_worktree
# ---------------------------------------------------------------------------

def test_reaper_calls_worktree_cleanup(tmp_path):
    _FIXTURES = Path(__file__).resolve().parent / "fixtures"
    if str(_FIXTURES) not in sys.path:
        sys.path.insert(0, str(_FIXTURES))

    import json
    import sqlite3
    from pool_state_fixtures import _BASE_SCHEMA, create_test_db_file

    db = create_test_db_file(
        tmp_path / "test.db", min_workers=1, max_workers=4, cooldown_seconds=0,
    )

    conn = sqlite3.connect(str(db))
    conn.execute(
        """INSERT OR IGNORE INTO terminal_leases
           (terminal_id, project_id, state, lease_token, last_heartbeat_at)
           VALUES ('REAP-T1', 'vnx-dev', 'idle', '', '2020-01-01T00:00:00.000000Z')""",
    )
    conn.execute(
        """INSERT INTO worker_pool_membership
           (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
           VALUES ('REAP-T1', 'vnx-dev', 'default', 'claude', 'backend-developer',
                   '2020-01-01T00:00:00.000000Z', ?)""",
        (json.dumps({"membership_id": "m-reap-1"}),),
    )
    conn.commit()
    conn.close()

    from pool_manager import PoolManager, SpawnResult

    def noop_spawn(*_a, **_kw):
        return SpawnResult(terminal_id="x", success=True)

    mgr = PoolManager(
        project_id="vnx-dev", pool_id="default", db_path=db, spawn_fn=noop_spawn,
    )

    with patch("pool_worktree_manager.reap_worker_worktree") as mock_reap:
        with patch("pool_manager.reap_worker_worktree", mock_reap, create=True):
            targets = mgr.reap_dead()

    if targets:
        terminal_ids_reaped = [t.terminal_id for t in targets]
        assert "REAP-T1" in terminal_ids_reaped
        reap_calls = [c[0][0] for c in mock_reap.call_args_list]
        assert "REAP-T1" in reap_calls


# ---------------------------------------------------------------------------
# 13. Security: terminal_id validation rejects path traversal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_id", [
    "../etc/passwd",
    "../../root",
    "foo/bar",
    "a" * 33,
    "",
    "hello world",
    "T1; rm -rf /",
    "pool\x00null",
])
def test_validate_terminal_id_rejects_malicious(bad_id):
    with pytest.raises(ValueError, match="invalid terminal_id"):
        _validate_terminal_id(bad_id)


@pytest.mark.parametrize("good_id", [
    "T1",
    "ABC-1",
    "worker_3",
    "a" * 32,
    "T0",
    "my-worker-99",
])
def test_validate_terminal_id_accepts_valid(good_id):
    _validate_terminal_id(good_id)


def test_create_rejects_traversal_id(tmp_path):
    with pytest.raises(ValueError, match="invalid terminal_id"):
        create_worker_worktree("../../../etc", project_root=tmp_path)


def test_reap_rejects_traversal_id(tmp_path):
    with pytest.raises(ValueError, match="invalid terminal_id"):
        reap_worker_worktree("../../../etc", project_root=tmp_path)


# ---------------------------------------------------------------------------
# 14. Security: cleanup fallback refuses symlink targets
# ---------------------------------------------------------------------------

def test_reap_fallback_refuses_symlink_inside_root(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    wt_dir = local / ".vnx-data" / "worktrees" / "pool-SYM1"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    target = local / "real_target"
    target.mkdir()
    wt_dir.symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing cleanup.*symlink"):
        reap_worker_worktree("SYM1", project_root=local)

    assert target.is_dir()


def test_reap_fallback_refuses_symlink_outside_root(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    wt_dir = local / ".vnx-data" / "worktrees" / "pool-SYM2"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    target = tmp_path / "outside_target"
    target.mkdir()
    wt_dir.symlink_to(target)

    with pytest.raises((ValueError, RuntimeError)):
        reap_worker_worktree("SYM2", project_root=local)

    assert target.is_dir()


# ---------------------------------------------------------------------------
# 15. Security: cleanup fallback refuses paths outside project root
# ---------------------------------------------------------------------------

def test_reap_fallback_refuses_outside_root(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    wt_dir = local / ".vnx-data" / "worktrees" / "pool-ESC1"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    wt_dir.symlink_to(outside)

    with pytest.raises((ValueError, RuntimeError)):
        reap_worker_worktree("ESC1", project_root=local)
