#!/usr/bin/env python3
"""Tests for worktree handling in headless_dispatch_writer.

The headless worker must operate in the per-dispatch worktree the door created
for it, and branch there — never the main checkout.  Three deliverables, each
covered by a test class below:

1. The worker branches in the worktree, not the main checkout — ``write_dispatch``
   stamps ``worktree_path`` + the worktree's branch into ``dispatch.json``, and
   ``_resolve_dispatch_worktree`` really does land on ``dispatch/<safe_id>``
   while the main checkout stays on ``main``.

2. The OI-1232 occupancy lock is taken (via ``create_dispatch_worktree``) and
   released (via ``remove_dispatch_worktree``) — the writer adds no lock of its
   own and no timeout-based cleanup.

3. An unresolvable worktree refuses the dispatch — fail-closed, no fallback to
   the main checkout (the exact behavior this work removes).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_worktree_isolation  # noqa: E402
import headless_dispatch_writer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_shared_claim_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the claim registry + occupancy lock at a shared temp state root."""
    data_dir = tmp_path / "shared-data"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a real git repo on branch main with one committed seed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "wt-test@vnx"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "VNX WT Test"], check=True
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return repo


def _flock_is_held(lock_path: Path) -> bool:
    """True when a fresh open file description cannot take the exclusive flock.

    flock() locks are per open-file-description, so a second ``open()`` from the
    same process is treated independently and denied while the lock is held.
    """
    fh = open(lock_path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# 1. The worker branches in the worktree, not the main checkout
# ---------------------------------------------------------------------------

class TestWriteDispatchStampsWorktree:
    def test_payload_carries_worktree_path_and_branch_not_main(self, tmp_path, monkeypatch):
        """write_dispatch stamps the worktree path + branch, not the main branch."""
        dispatches_dir = tmp_path / "dispatches"
        (dispatches_dir / "pending").mkdir(parents=True)
        skills_dir = tmp_path / "skills"
        (skills_dir / "backend-developer").mkdir(parents=True)

        fake_root = tmp_path / "consumer-root"
        fake_root.mkdir()
        fake_wt = fake_root / ".vnx-data" / "worktrees" / "dispatch-20260816-p7-stamp"
        fake_wt.mkdir(parents=True)

        monkeypatch.setattr(headless_dispatch_writer, "_dispatch_dir", lambda: dispatches_dir)
        monkeypatch.setattr(headless_dispatch_writer, "_skills_dir", lambda: skills_dir)
        monkeypatch.setattr(
            dispatch_worktree_isolation, "resolve_consumer_project_root", lambda: fake_root
        )
        monkeypatch.setattr(
            dispatch_worktree_isolation,
            "create_dispatch_worktree",
            lambda dispatch_id, **kw: fake_wt,
        )
        monkeypatch.setattr(
            dispatch_worktree_isolation,
            "verify_worktree_identity",
            lambda dispatch_id, wt_path, **kw: {
                "dispatch_id": dispatch_id,
                "worktree_path": str(fake_wt),
                "branch": "dispatch/20260816-p7-stamp",
            },
        )

        path = headless_dispatch_writer.write_dispatch(
            dispatch_id="20260816-p7-stamp",
            terminal="T1",
            track="A",
            role="backend-developer",
            instruction="the worker must branch in the worktree",
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["worktree_path"] == str(fake_wt), (
            "dispatch.json must carry the worktree path so the worker runs there"
        )
        assert payload["branch"] == "dispatch/20260816-p7-stamp", (
            "dispatch.json must carry the worktree branch, not the main branch"
        )
        assert payload["branch"] != "main"


class TestWorkerBranchesInWorktreeNotMain:
    def test_resolve_lands_on_dispatch_branch_and_main_stays_main(self, tmp_path, monkeypatch):
        """_resolve_dispatch_worktree checks out dispatch/<safe_id>; main is untouched."""
        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        dispatch_id = "20260816-p7-real-wt"
        wt_path, branch = headless_dispatch_writer._resolve_dispatch_worktree(
            dispatch_id, project_root=repo
        )

        assert branch == "dispatch/20260816-p7-real-wt"
        assert wt_path.exists()

        wt_branch = subprocess.run(
            ["git", "-C", str(wt_path), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert wt_branch == "dispatch/20260816-p7-real-wt", (
            f"worker checkout must be the worktree branch, got {wt_branch!r}"
        )

        main_branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert main_branch == "main", (
            f"main checkout must stay on main, got {main_branch!r}"
        )

        dispatch_worktree_isolation.remove_dispatch_worktree(dispatch_id, project_root=repo)


# ---------------------------------------------------------------------------
# 2. The OI-1232 occupancy lock is taken and released
# ---------------------------------------------------------------------------

class TestOccupancyLockTakenAndReleased:
    def test_lock_held_after_resolve_and_released_after_remove(self, tmp_path, monkeypatch):
        """create_dispatch_worktree holds the lock; remove_dispatch_worktree releases it."""
        _set_shared_claim_dir(monkeypatch, tmp_path)
        repo = _init_git_repo(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "main")

        dispatch_id = "20260816-p7-lock"
        headless_dispatch_writer._resolve_dispatch_worktree(dispatch_id, project_root=repo)

        safe_id = dispatch_worktree_isolation._sanitize_dispatch_id(dispatch_id)
        lock_path = dispatch_worktree_isolation._occupancy_lock_path(safe_id, repo)

        # Taken: a fresh open file description cannot acquire the exclusive lock.
        assert _flock_is_held(lock_path) is True, (
            "occupancy lock must be held after the worktree is resolved"
        )

        dispatch_worktree_isolation.remove_dispatch_worktree(dispatch_id, project_root=repo)

        # Released: the canonical teardown released it.
        assert _flock_is_held(lock_path) is False, (
            "occupancy lock must be released after remove_dispatch_worktree"
        )


# ---------------------------------------------------------------------------
# 3. An unresolvable worktree refuses the dispatch (fail-closed)
# ---------------------------------------------------------------------------

class TestUnresolvableWorktreeRefuses:
    def test_write_dispatch_refuses_and_writes_nothing(self, tmp_path, monkeypatch):
        """A failed worktree resolution refuses the dispatch with no partial write."""
        dispatches_dir = tmp_path / "dispatches"
        (dispatches_dir / "pending").mkdir(parents=True)
        skills_dir = tmp_path / "skills"
        (skills_dir / "backend-developer").mkdir(parents=True)
        fake_root = tmp_path / "consumer-root"
        fake_root.mkdir()

        def refuse_create(dispatch_id, **kw):
            raise RuntimeError("cannot resolve base_ref 'origin/main'")

        monkeypatch.setattr(headless_dispatch_writer, "_dispatch_dir", lambda: dispatches_dir)
        monkeypatch.setattr(headless_dispatch_writer, "_skills_dir", lambda: skills_dir)
        monkeypatch.setattr(
            dispatch_worktree_isolation, "resolve_consumer_project_root", lambda: fake_root
        )
        monkeypatch.setattr(
            dispatch_worktree_isolation, "create_dispatch_worktree", refuse_create
        )

        with pytest.raises(headless_dispatch_writer.DispatchWorktreeError):
            headless_dispatch_writer.write_dispatch(
                dispatch_id="20260816-p7-refuse",
                terminal="T1",
                track="A",
                role="backend-developer",
                instruction="must refuse — no fallback to main checkout",
            )

        # Fail-closed: no pending dir, no dispatch.json written.
        assert list((dispatches_dir / "pending").iterdir()) == [], (
            "an unresolvable worktree must refuse before writing any dispatch.json"
        )
