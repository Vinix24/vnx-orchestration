#!/usr/bin/env python3
"""tests/test_worktree_liveness_guard.py — PR-4a: liveness is alive/dead/unknown
and the occupancy slot is held through the removal.

Each test is one round-1/2/3 finding of the closed PR-4 (#2221, OI-2365):

  1. every ``getpgid`` probe raising ``PermissionError`` must classify the
     group ``unknown`` and keep the worktree (round 3);
  2. a process holding the dispatch occupancy slot makes the guard skip, and a
     process that tries to take the slot while the guard holds it cannot write
     into the tree under removal (round 2);
  3. a live gate seat's lifetime flock makes the guard refuse; after the
     seat's teardown the guard removes (round 1);
  4. a tree whose HEAD moved between the assessment and the removal is refused
     with ``head_moved``;
  5. ``dry_run=True`` (the default) removes nothing.

Real git repos under ``tmp_path`` and real second processes; only
``os.getpgid`` is patched, where the process boundary is the thing under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import dispatch_worktree_isolation as dwi  # noqa: E402  (sys.path set above)


# ---------------------------------------------------------------------------
# Helpers — real repos, real processes, no sleeps
# ---------------------------------------------------------------------------

def _git(args: "list[str]", cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"git {args} failed in {cwd}: {result.stderr}"
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.invalid"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture
def pinned_state(monkeypatch, tmp_path):
    """Pin the claim/occupancy registry to a temp state root.

    ``dispatch_worktree_isolation`` resolves it through ``vnx_paths``; without
    the pin the tests would read the ambient central store.  The children this
    suite spawns inherit the same environment, so they see the same registry.
    """
    data_dir = tmp_path / "state-root"
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _make_dispatch_worktree(tmp_path: Path, dispatch_id: str) -> "tuple[Path, Path]":
    """A real dispatch worktree, claimed and with its occupancy slot released.

    Mirrors what the creation lane leaves behind: a linked git worktree, an
    atomic claim for the dispatch, and an existing (unheld) occupancy lock file.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-m", "seed"], root)

    wt = root / ".vnx-data" / "worktrees" / f"dispatch-{dwi._sanitize_dispatch_id(dispatch_id)}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", f"dispatch/{dispatch_id}", str(wt), "HEAD"], root)
    dwi._write_claim_atomic(dispatch_id, worktree_path=wt, project_root=root)
    dwi._acquire_occupancy(dispatch_id, wt, root)
    dwi._release_occupancy(wt)
    return root, wt


_HOLDER_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dispatch_worktree_isolation as dwi
dwi._acquire_occupancy(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
print("ready", flush=True)
sys.stdin.readline()
"""

_WRITER_SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dispatch_worktree_isolation as dwi
try:
    dwi._acquire_occupancy(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
except dwi.WorktreeOccupied:
    print("occupied", flush=True)
else:
    Path(sys.argv[5]).write_text("written\\n", encoding="utf-8")
    print("wrote", flush=True)
"""


def _start_slot_holder(dispatch_id: str, wt: Path, root: Path) -> subprocess.Popen:
    """A second process that holds *dispatch_id*'s occupancy slot until told to stop."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(LIB_DIR), dispatch_id, str(wt), str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "ready", f"slot holder did not start: {line!r} {proc.stderr.read()!r}"
    return proc


def _stop_slot_holder(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def _run_writer_attempt(dispatch_id: str, wt: Path, root: Path, payload: Path) -> str:
    """One real second process that tries to take the slot and only writes on success."""
    proc = subprocess.run(
        [sys.executable, "-c", _WRITER_SCRIPT, str(LIB_DIR), dispatch_id, str(wt), str(root), str(payload)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"writer attempt failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def gate_repo(tmp_path):
    """An 'origin' with a feature branch and a 'local' clone, for create_gate_worktree."""
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "marker.txt").write_text("BASE\n", encoding="utf-8")
    _git(["add", "marker.txt"], origin)
    _git(["commit", "-m", "base"], origin)

    local = tmp_path / "local"
    _git(["clone", str(origin), str(local)], tmp_path)
    _git(["config", "user.email", "test@example.invalid"], local)
    _git(["config", "user.name", "Test"], local)

    _git(["checkout", "-b", "feature/guard"], origin)
    (origin / "marker.txt").write_text("FEATURE\n", encoding="utf-8")
    _git(["add", "marker.txt"], origin)
    _git(["commit", "-m", "feature"], origin)
    _git(["checkout", "main"], origin)
    return local


# ---------------------------------------------------------------------------
# 1. round 3 — an unmeasurable group is unknown, and unknown keeps the tree
# ---------------------------------------------------------------------------

def test_permission_error_is_unknown_and_keeps_the_tree(tmp_path, pinned_state, monkeypatch):
    from dispatch_process_registry import live_process_groups, record_dispatch_pgids
    from worktree_removal_guard import REASON_LIVENESS_UNKNOWN, guarded_remove

    dispatch_id = "dlv-guard-perm"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    own_pgid = os.getpgid(0)
    record_dispatch_pgids(dispatch_id, [own_pgid], repo_root=root)

    def _denied(pid):
        raise PermissionError(f"not allowed to inspect pid {pid}")

    monkeypatch.setattr(os, "getpgid", _denied)

    report = live_process_groups(dispatch_id, repo_root=root)
    assert report.alive == ()
    assert own_pgid in report.unknown, (
        "a PermissionError is not 'dead': the group must be reported unknown — "
        f"alive={report.alive} dead={report.dead} unknown={report.unknown}"
    )

    result = guarded_remove(wt, dispatch_id=dispatch_id, repo_root=root, dry_run=False)
    assert result.removed is False
    assert result.reason == REASON_LIVENESS_UNKNOWN, result.detail
    assert wt.exists(), "an unmeasurable process group always keeps the worktree"


# ---------------------------------------------------------------------------
# 2. round 2 — the occupancy slot is held through the removal
# ---------------------------------------------------------------------------

def test_second_process_holding_the_slot_makes_the_guard_skip(tmp_path, pinned_state):
    from worktree_removal_guard import REASON_OCCUPIED, guarded_remove

    dispatch_id = "dlv-guard-occupied"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    holder = _start_slot_holder(dispatch_id, wt, root)
    try:
        result = guarded_remove(wt, dispatch_id=dispatch_id, repo_root=root, dry_run=False)
        assert result.removed is False
        assert result.reason == REASON_OCCUPIED, result.detail
        assert wt.exists()
    finally:
        _stop_slot_holder(holder)


def test_writer_cannot_take_the_slot_while_the_guard_holds_it(tmp_path, pinned_state):
    from worktree_removal_guard import guarded_remove

    dispatch_id = "dlv-guard-writer"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    payload = wt / "injected.txt"
    attempts: "list[str]" = []

    def hook(label: str) -> None:
        assert label == "before_revalidation"
        attempts.append(_run_writer_attempt(dispatch_id, wt, root, payload))

    result = guarded_remove(
        wt, dispatch_id=dispatch_id, repo_root=root, dry_run=False, pause_hook=hook,
    )
    assert attempts == ["occupied"], (
        f"the writer must be refused the slot the guard holds, got {attempts}"
    )
    assert not payload.exists(), "the uncommitted file must never be written under the guard"
    assert result.removed is True, result.detail
    assert not wt.exists()


# ---------------------------------------------------------------------------
# 3. round 1 — a live gate seat's lifetime flock blocks the guard
# ---------------------------------------------------------------------------

def test_gate_seat_lifetime_lock_blocks_until_its_teardown(gate_repo):
    from gate_worktree import (
        _release_lifetime_lock,
        create_gate_worktree,
        gate_checkout_liveness,
        remove_gate_worktree,
    )
    from worktree_removal_guard import REASON_GATE_SEAT_LIVE, REASON_REMOVED, guarded_remove

    local = gate_repo
    wt = create_gate_worktree(
        branch="feature/guard", gate="codex_gate", identifier="seat", project_root=local,
    )
    try:
        assert gate_checkout_liveness(wt) == "live"
        result = guarded_remove(wt, dispatch_id="", repo_root=local, dry_run=False)
        assert result.removed is False
        assert result.reason == REASON_GATE_SEAT_LIVE, result.detail
        assert wt.exists(), "a live gate seat's checkout must never be removed"

        _release_lifetime_lock(wt)  # the seat's own teardown; the checkout stays
        result = guarded_remove(wt, dispatch_id="", repo_root=local, dry_run=False)
        assert result.removed is True, result.detail
        assert result.reason == REASON_REMOVED
        assert not wt.exists()
    finally:
        _release_lifetime_lock(wt)
        remove_gate_worktree(wt, project_root=local)


# ---------------------------------------------------------------------------
# 4. HEAD moved between the assessment and the removal
# ---------------------------------------------------------------------------

def test_head_moved_between_assessment_and_removal_is_refused(tmp_path, pinned_state):
    from worktree_removal_guard import REASON_HEAD_MOVED, guarded_remove

    dispatch_id = "dlv-guard-head"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    moved: "list[bool]" = []

    def hook(label: str) -> None:
        assert label == "before_revalidation"
        _git(["commit", "--allow-empty", "-m", "HEAD moved under the guard"], wt)
        moved.append(True)

    result = guarded_remove(
        wt, dispatch_id=dispatch_id, repo_root=root, dry_run=False, pause_hook=hook,
    )
    assert moved == [True]
    assert result.removed is False
    assert result.reason == REASON_HEAD_MOVED, result.detail
    assert wt.exists()


# ---------------------------------------------------------------------------
# 5. the default is a dry run
# ---------------------------------------------------------------------------

def test_dry_run_removes_nothing(tmp_path, pinned_state):
    from worktree_removal_guard import REASON_DRY_RUN, guarded_remove

    dispatch_id = "dlv-guard-dry"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    result = guarded_remove(wt, dispatch_id=dispatch_id, repo_root=root)
    assert result.removed is False
    assert result.reason == REASON_DRY_RUN
    assert result.dry_run is True
    assert result.would_remove is True
    assert wt.exists(), "dry_run=True must remove nothing"


# ---------------------------------------------------------------------------
# Fail-closed clean check: an uncommitted file is refused, never force-removed
# ---------------------------------------------------------------------------

def test_uncommitted_file_is_refused_and_kept(tmp_path, pinned_state):
    from worktree_removal_guard import REASON_DIRTY, guarded_remove

    dispatch_id = "dlv-guard-dirty"
    root, wt = _make_dispatch_worktree(tmp_path, dispatch_id)
    kept = wt / "uncommitted.txt"
    kept.write_text("must survive\n", encoding="utf-8")

    result = guarded_remove(wt, dispatch_id=dispatch_id, repo_root=root, dry_run=False)
    assert result.removed is False
    assert result.reason == REASON_DIRTY, result.detail
    assert kept.read_text(encoding="utf-8") == "must survive\n"
