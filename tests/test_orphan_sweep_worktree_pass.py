"""tests/test_orphan_sweep_worktree_pass.py — characterisation of kind 2.

``orphan_sweep.sweep`` runs three kinds after its setup; the second — worktrees
whose tmux session is gone — is the pass this file pins before that block moves
into its own helpers (golf-2-hardenen, G2-10). Every branch of the pass gets one
test, each asserting the exact ``OrphanSweepResult`` lists and the log records
``caplog`` captured, so the move has a baseline to stay green against.

Fixtures and injectables mirror ``tests/test_orphan_sweep.py``: real git only
inside ``tmp_path`` (classify/reap run their real code), injected liveness and
a pinned tmp data/state dir. No real tmux.
"""

from __future__ import annotations

import json
import logging
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
from git_maintenance_guard import disable_auto_maintenance  # noqa: E402

_LOGGER = "orphan_sweep"


@pytest.fixture
def env(tmp_path):
    """Pin runtime dirs to a per-test tmp tree; clear the current-dispatch fence.

    Mirrors ``tests/test_orphan_sweep.py::env``: the current-dispatch fence
    (``VNX_CURRENT_DISPATCH_ID``) is exported by the dispatch worker, so it is
    cleared here or the default would protect a real dispatch id in every test.
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
    # Keep the commit below from spawning a detached `git maintenance run
    # --auto` child that can outlive this repo's temp dir (CI 39971696706).
    disable_auto_maintenance(local)
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


def _write_register_event(env: Path, event: str, dispatch_id: str) -> None:
    """Append a raw register event, bypassing dispatch_register.append_event's
    identity resolution/VALID_EVENTS gate — test data setup, not the write API."""
    path = env / "state" / "dispatch_register.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-08-19T00:00:00.000000Z",
            "event": event,
            "dispatch_id": dispatch_id,
        }) + "\n")


def _worktree_records(caplog, level: int | None = None) -> list[tuple[int, str]]:
    """(levelno, message) of every per-entry kind-2 record.

    The run-completion footer also counts worktrees; it is not a per-entry
    record and is filtered out so an assertion names the branch under test.
    """
    return [
        (rec.levelno, rec.getMessage())
        for rec in caplog.records
        if rec.name == _LOGGER
        and "worktree" in rec.getMessage()
        and not rec.getMessage().startswith("orphan_sweep: complete")
        and (level is None or rec.levelno == level)
    ]


# ---------------------------------------------------------------------------
# Entry filtering — not a directory / not a dispatch name
# ---------------------------------------------------------------------------

def test_non_directory_and_foreign_name_entries_are_ignored(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    worktrees_dir = local / ".vnx-data" / "worktrees"
    worktrees_dir.mkdir(parents=True)
    (worktrees_dir / "stray-file.txt").write_text("not a worktree\n")
    (worktrees_dir / "not-a-dispatch").mkdir()

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == []
    assert res.worktrees_removed == []
    assert res.worktrees_preserved == []
    assert res.worktrees_skipped_live == []
    assert res.errors == []
    assert _worktree_records(caplog) == []


# ---------------------------------------------------------------------------
# Skips — unmeasurable listing, surviving session, in-flight/unmeasurable register
# ---------------------------------------------------------------------------

def test_unmeasurable_listing_skips_every_worktree(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "unmeasured-1")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: None)

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_skipped_live == [str(handle.path)]
    assert res.worktrees_removed == []
    assert res.worktrees_preserved == []
    assert [e["kind"] for e in res.errors] == ["tmux"]
    assert handle.path.is_dir()
    # The per-entry skip branch is silent; the only worktree record is the
    # kind-1 listing warning recorded before the pass runs.
    assert _worktree_records(caplog) == [(
        logging.WARNING,
        "orphan_sweep: tmux session listing unmeasurable — skipping worktree "
        "reap this run (fail-open)",
    )]


def test_surviving_session_id_skips_its_worktree(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "survivor-1")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-survivor-1"],
        probe_liveness=lambda s: True,
    )

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_skipped_live == [str(handle.path)]
    assert res.worktrees_removed == []
    assert res.errors == []
    assert handle.path.is_dir()
    assert _worktree_records(caplog) == []


def test_register_in_flight_skips_worktree_with_log(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "inflight-1")
    _write_register_event(env, "dispatch_created", "inflight-1")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_skipped_live == [str(handle.path)]
    assert res.worktrees_removed == []
    assert res.errors == []
    assert handle.path.is_dir()
    assert _worktree_records(caplog, logging.INFO) == [(
        logging.INFO,
        "orphan_sweep: SKIP worktree dispatch-inflight-1 — no tmux session "
        "but register shows it in flight",
    )]


def test_register_unmeasurable_skips_worktree_with_log(env, tmp_path, caplog, monkeypatch):
    import dispatch_register

    def _raise(*, state_dir=None):
        raise RuntimeError("register read blew up")

    monkeypatch.setattr(dispatch_register, "read_events", _raise)
    local = _git_repo(tmp_path)
    handle = _alloc(local, "unmeasurable-reg-1")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_skipped_live == [str(handle.path)]
    assert res.worktrees_removed == []
    assert [e["kind"] for e in res.errors] == ["completed_check"]
    assert handle.path.is_dir()
    assert _worktree_records(caplog, logging.INFO) == [(
        logging.INFO,
        "orphan_sweep: SKIP worktree dispatch-unmeasurable-reg-1 — no tmux "
        "session but register was unmeasurable",
    )]


# ---------------------------------------------------------------------------
# classify_path — exception lands in errors, loop continues
# ---------------------------------------------------------------------------

def test_classify_exception_recorded_and_loop_continues(env, tmp_path, monkeypatch):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "classify-boom-1")

    def _raise(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tmux_worktree, "classify_path", _raise)

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_removed == []
    assert res.worktrees_preserved == []
    assert res.errors == [
        {"kind": "worktree", "path": str(handle.path), "error": "classify: boom"},
    ]
    assert handle.path.is_dir()


# ---------------------------------------------------------------------------
# Dry-run — classification decides the bucket, nothing is reaped
# ---------------------------------------------------------------------------

def test_dry_run_dirty_preserved_and_clean_removed_without_reaping(env, tmp_path, caplog, monkeypatch):
    local = _git_repo(tmp_path)
    clean = _alloc(local, "dry-clean-1")
    dirty = _alloc(local, "dry-dirty-1")
    (dirty.path / "uncommitted.txt").write_text("wip\n")
    reaped: list[str] = []

    def _record(handle, classification):
        reaped.append(handle.dispatch_id)
        raise AssertionError("dry-run must never reap")

    monkeypatch.setattr(tmux_worktree, "reap", _record)

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [], dry_run=True)

    # sorted(worktrees_dir.iterdir()): dry-clean-1 before dry-dirty-1.
    assert res.worktrees_scanned == [str(clean.path), str(dirty.path)]
    assert res.worktrees_removed == [str(clean.path)]
    assert res.worktrees_preserved == [str(dirty.path)]
    assert res.worktrees_skipped_live == []
    assert res.errors == []
    assert reaped == []
    assert clean.path.is_dir() and dirty.path.is_dir()
    assert _worktree_records(caplog, logging.INFO) == [
        (logging.INFO,
         "orphan_sweep: DRY-RUN worktree dispatch-dry-clean-1 classified clean (no mutation)"),
        (logging.INFO,
         "orphan_sweep: DRY-RUN worktree dispatch-dry-dirty-1 classified dirty (no mutation)"),
    ]


# ---------------------------------------------------------------------------
# reap — removes / preserves / raises / returns errors
# ---------------------------------------------------------------------------

def test_reap_removes_clean_worktree(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "reap-clean-1")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_removed == [str(handle.path)]
    assert res.worktrees_preserved == []
    assert res.errors == []
    assert not handle.path.exists()
    assert _worktree_records(caplog, logging.INFO) == [
        (logging.INFO, "orphan_sweep: reaped orphan worktree dispatch-reap-clean-1"),
    ]


def test_reap_preserves_dirty_worktree(env, tmp_path, caplog):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "reap-dirty-1")
    (handle.path / "uncommitted.txt").write_text("wip\n")

    caplog.set_level(logging.INFO, logger=_LOGGER)
    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_scanned == [str(handle.path)]
    assert res.worktrees_removed == []
    assert res.worktrees_preserved == [str(handle.path)]
    assert res.errors == []
    assert handle.path.is_dir()
    assert (handle.path / "uncommitted.txt").exists()
    assert _worktree_records(caplog, logging.INFO) == [
        (logging.INFO,
         "orphan_sweep: preserved (marked) dirty worktree dispatch-reap-dirty-1"),
    ]


def test_reap_exception_recorded(env, tmp_path, monkeypatch):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "reap-boom-1")

    def _raise(handle, classification):
        raise RuntimeError("reap blew up")

    monkeypatch.setattr(tmux_worktree, "reap", _raise)

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_removed == []
    assert res.worktrees_preserved == []
    assert res.errors == [
        {"kind": "worktree", "path": str(handle.path), "error": "reap blew up"},
    ]
    assert handle.path.is_dir()


def test_reap_returned_errors_recorded(env, tmp_path, monkeypatch):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "reap-errors-1")

    def _partial(handle, classification):
        return tmux_worktree.ReapResult(
            removed=False, preserved_path=None, errors=["git worktree remove failed"],
        )

    monkeypatch.setattr(tmux_worktree, "reap", _partial)

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert res.worktrees_removed == []
    assert res.worktrees_preserved == []
    assert res.errors == [
        {"kind": "worktree", "path": str(handle.path), "error": "git worktree remove failed"},
    ]
