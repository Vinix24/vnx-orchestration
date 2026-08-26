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
import vnx_paths  # noqa: E402

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


def _write_register_event(env: Path, event: str, dispatch_id: str) -> None:
    """Append a raw register event, bypassing dispatch_register.append_event's
    identity resolution/VALID_EVENTS gate — this is test data setup, not a
    call through the write API under test."""
    path = env / "state" / "dispatch_register.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-08-19T00:00:00.000000Z",
            "event": event,
            "dispatch_id": dispatch_id,
        }) + "\n")


def _write_register_event_in_state_dir(state_dir: Path, event: str, dispatch_id: str) -> None:
    """Like _write_register_event, but writes directly under a given
    state_dir instead of an ``env``-fixture data_dir — for tests that pin an
    explicit state_dir independent of data_dir/central resolution."""
    path = state_dir / "dispatch_register.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-08-21T00:00:00.000000Z",
            "event": event,
            "dispatch_id": dispatch_id,
        }) + "\n")


def _write_receipt(env: Path, dispatch_id: str, *, event_type: str, status: str) -> None:
    path = env / "state" / "t0_receipts.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "dispatch_id": dispatch_id,
            "event_type": event_type,
            "status": status,
        }) + "\n")


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
# OI-1427 — a headless dispatch's worktree must survive a sweep even though
# it opens NO tmux session at all (the kind-1 "surviving_ids" signal is
# entirely tmux-derived, so a headless dispatch fails that check from second
# one). The register knows this dispatch (a dispatch_created event is written
# by the door for EVERY lane -- tmux, headless, and provider -- before the
# worktree is even created) and shows no terminal outcome yet: that is the
# life-sign this class of worktree actually has. The control test proves the
# tmux-session path is untouched -- without it, a broken probe that always
# "protects" would pass the headless test for the wrong reason.
# ---------------------------------------------------------------------------

def test_headless_inflight_worktree_survives_sweep_without_tmux_session(env, tmp_path):
    """On main (pre-fix) this fails on BEHAVIOR, not a missing symbol: sweep()
    runs to completion with no tmux session and no register knowledge of the
    worktree's liveness, classifies the freshly-allocated (clean) worktree as
    an orphan, and reaps it -- exactly the OI-1427 incident (a live headless
    dispatch's worktree deleted out from under it)."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "headless-1")
    _write_register_event(env, "dispatch_created", "headless-1")  # known, in flight

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: [],  # headless dispatch opens NO tmux session, ever
    )

    assert str(handle.path) in res.worktrees_skipped_live
    assert handle.path.is_dir()
    assert res.worktrees_removed == []


def test_headless_worktree_reaped_once_register_shows_terminal(env, tmp_path):
    """The flip side: once the register proves the dispatch reached a terminal
    outcome, the new criterion must NOT protect it forever -- otherwise a
    genuinely orphaned headless worktree (crashed wrapper, but one that got as
    far as writing dispatch_completed) could never be swept."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "headless-done-1")
    _write_register_event(env, "dispatch_created", "headless-done-1")
    _write_register_event(env, "dispatch_completed", "headless-done-1")

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert str(handle.path) in res.worktrees_removed
    assert not handle.path.exists()


def test_headless_unknown_worktree_unaffected_by_new_criterion(env, tmp_path):
    """A worktree whose dispatch id the register has never heard of (e.g. a
    genuinely stale leftover) must still be reaped exactly as before -- the
    new criterion only ever ADDS protection, never removes it, and must never
    turn 'register says nothing' into a reason to preserve."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "never-registered-1")

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert str(handle.path) in res.worktrees_removed
    assert not handle.path.exists()


def test_headless_register_unmeasurable_fails_open_on_worktree(env, tmp_path, monkeypatch):
    """'Cannot measure the register' must never collapse into 'not in flight'
    -- mirrors the existing listing_unmeasurable contract for kind 1/2."""
    import dispatch_register

    def _raise(*, state_dir=None):
        raise RuntimeError("register read blew up")

    monkeypatch.setattr(dispatch_register, "read_events", _raise)
    local = _git_repo(tmp_path)
    handle = _alloc(local, "unmeasurable-1")

    res = osweep.sweep(repo_root=local, data_dir=env, list_sessions=lambda: [])

    assert str(handle.path) in res.worktrees_skipped_live
    assert handle.path.is_dir()
    assert res.worktrees_removed == []
    assert any(e.get("kind") == "completed_check" for e in res.errors)


def test_headless_control_tmux_session_still_protects_worktree(env, tmp_path):
    """Control for the tests above: a dispatch WITH a live tmux session must
    still be protected via the pre-existing, unrelated tmux path -- proves
    the measurement instrument still works and the headless tests above are
    not passing because every worktree is now unconditionally preserved."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "tmux-ctrl-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-tmux-ctrl-1"],
        probe_liveness=lambda s: True,
    )

    assert str(handle.path) in res.worktrees_skipped_live
    assert handle.path.is_dir()
    assert res.worktrees_removed == []


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
# Kind 1b — completed-dispatch orphan conjunction (OI-1353)
#
# The conjunction: (a) name matches _SESSION_RE [enforced by sweep()'s scan
# loop, not re-tested here], (b) dispatch id known to this project's own
# register, (c) dispatch provably completed (register event OR terminal
# receipt), (d) worktree already gone, (e) pane not working/awaiting_permission,
# (f) not the invoking dispatch [enforced by the pre-existing `protected`
# fence]. Unit tests below hit ``_evaluate_completed_orphan`` directly for
# (b)-(e); integration tests hit ``sweep()`` end-to-end, including (a)/(f).
# ---------------------------------------------------------------------------

def test_evaluate_completed_orphan_register_unmeasurable(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids=None,
        register_completed_ids=None,
        receipts_index={},
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "register_unmeasurable")


def test_evaluate_completed_orphan_unknown_project(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids=set(),  # measured — this project's register is empty
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "unknown_project")


def test_evaluate_completed_orphan_receipts_unmeasurable(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids=set(),  # not completed via register
        receipts_index=None,           # and receipts could not be read
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "receipts_unmeasurable")


def test_evaluate_completed_orphan_not_completed(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids=set(),
        receipts_index={},  # measured — no receipt at all for this id
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "not_completed")


def test_evaluate_completed_orphan_worktree_still_exists(tmp_path):
    (tmp_path / "dispatch-x-1").mkdir()
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "worktree_still_exists")


def test_evaluate_completed_orphan_pane_working(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=lambda s: "working...\n(12s · ↓ 500 tokens · esc to interrupt)",
    )
    assert (is_orphan, reason) == (False, "pane_working")


def test_evaluate_completed_orphan_pane_awaiting_permission(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=lambda s: "Do you want to proceed?\n1. Yes\n2. No",
    )
    assert (is_orphan, reason) == (False, "pane_awaiting_permission")


def test_evaluate_completed_orphan_pane_unmeasurable(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=lambda s: None,
    )
    assert (is_orphan, reason) == (False, "pane_unmeasurable")


def test_evaluate_completed_orphan_pane_capture_raises_fails_open(tmp_path):
    def _raise(session):
        raise RuntimeError("tmux capture-pane blew up")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=_raise,
    )
    assert (is_orphan, reason) == (False, "pane_unmeasurable")


def test_evaluate_completed_orphan_satisfied_via_register(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids={"x-1"},
        receipts_index={},
        capture_pane=lambda s: "",  # empty pane -> classify_worker_pane -> dead
    )
    assert is_orphan is True
    assert reason == "register:dispatch_completed;pane=dead"


def test_evaluate_completed_orphan_satisfied_via_receipt(tmp_path):
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},
        register_completed_ids=set(),  # not completed via register
        receipts_index={"x-1": [{"event_type": "task_complete", "status": "success"}]},
        capture_pane=lambda s: "",
    )
    assert is_orphan is True
    assert reason == "receipt:success;pane=dead"


# ---------------------------------------------------------------------------
# Kind 1b — end-to-end via sweep()
# ---------------------------------------------------------------------------

def test_sweep_kills_completed_orphan_via_register_event(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "done-1")  # no worktree -> (d) trivially holds
    killed = []

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-done-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: (killed.append(s), True)[1],
        capture_pane=lambda s: "",
    )

    assert killed == ["vnx-done-1"]
    assert len(res.tmux_completed_orphans_killed) == 1
    entry = res.tmux_completed_orphans_killed[0]
    assert entry["session"] == "vnx-done-1"
    assert entry["dispatch_id"] == "done-1"
    assert entry["reason"].startswith("register:dispatch_completed")
    assert res.tmux_completed_orphans_preserved == []
    assert "vnx-done-1" not in res.tmux_skipped_alive
    assert "vnx-done-1" not in res.tmux_killed  # separate key from the dead-pane class


def test_sweep_kills_completed_orphan_via_terminal_receipt(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_created", "recv-1")  # known, NOT completed via register
    _write_receipt(env, "recv-1", event_type="task_complete", status="success")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-recv-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert len(res.tmux_completed_orphans_killed) == 1
    assert res.tmux_completed_orphans_killed[0]["reason"].startswith("receipt:success")


def test_sweep_preserves_alive_orphan_with_live_worktree(env, tmp_path):
    local = _git_repo(tmp_path)
    handle = _alloc(local, "wt-1")
    _write_register_event(env, "dispatch_completed", "wt-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-wt-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert len(res.tmux_completed_orphans_preserved) == 1
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "worktree_still_exists"
    assert "vnx-wt-1" in res.tmux_skipped_alive
    assert handle.path.is_dir()


def test_sweep_preserves_alive_known_but_not_completed(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_created", "inflight-1")  # known, still in flight

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-inflight-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "not_completed"
    assert "vnx-inflight-1" in res.tmux_skipped_alive


def test_sweep_preserves_alive_session_unknown_to_project(env, tmp_path):
    """A live session on the shared tmux server that this project's register
    has never heard of — e.g. a different project's dispatch — must never be
    judged, let alone killed, by this project's sweep (condition b)."""
    local = _git_repo(tmp_path)

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-other-project-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "unknown_project"


def test_sweep_preserves_alive_completed_orphan_awaiting_permission(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "perm-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-perm-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "Do you want to proceed?\n1. Yes\n2. No",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "pane_awaiting_permission"
    assert "vnx-perm-1" in res.tmux_skipped_alive


def test_sweep_preserves_alive_completed_orphan_pane_unmeasurable(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "unmeas-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-unmeas-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: None,
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "pane_unmeasurable"


def test_sweep_preserves_protected_completed_dispatch(env, tmp_path):
    """(f): even a session that would otherwise satisfy the full conjunction
    is never touched when it is the invoking dispatch itself."""
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "prot-done-1")

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        current_dispatch_id="prot-done-1",
        list_sessions=lambda: ["vnx-prot-done-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved == []
    assert res.tmux_skipped_protected == ["vnx-prot-done-1"]


def test_sweep_dry_run_completed_orphan_shows_ground_no_mutation(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "dry-done-1")
    killed = []

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        dry_run=True,
        list_sessions=lambda: ["vnx-dry-done-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: (killed.append(s), True)[1],
        capture_pane=lambda s: "",
    )

    assert res.dry_run
    assert killed == []  # would-kill only, nothing actually run
    assert len(res.tmux_completed_orphans_killed) == 1
    assert res.tmux_completed_orphans_killed[0]["dispatch_id"] == "dry-done-1"


def test_sweep_completed_orphan_kill_is_idempotent(env, tmp_path):
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "idem-done-1")
    killed = []

    first = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: ["vnx-idem-done-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: (killed.append(s), True)[1],
        capture_pane=lambda s: "",
    )
    assert killed == ["vnx-idem-done-1"]
    assert len(first.tmux_completed_orphans_killed) == 1

    second = osweep.sweep(
        repo_root=local,
        data_dir=env,
        list_sessions=lambda: [],  # session is gone now, as real tmux would report
        probe_liveness=lambda s: True,
        kill_session=lambda s: (killed.append(s), True)[1],
        capture_pane=lambda s: "",
    )
    assert second.tmux_completed_orphans_killed == []
    assert second.tmux_sessions_scanned == []
    assert killed == ["vnx-idem-done-1"]  # not killed twice


def test_to_dict_includes_completed_orphan_keys(env):
    res = osweep.sweep(data_dir=env, list_sessions=lambda: [])
    d = res.to_dict()
    assert d["tmux_completed_orphans_killed"] == []
    assert d["tmux_completed_orphans_preserved"] == []


# ---------------------------------------------------------------------------
# Central-store resolution (OI-1353 follow-up) — sweep() reads the register/
# receipts/active-manifest store via the fabric's canonical resolver when
# neither data_dir nor state_dir is given, NOT via a repo-relative guess.
# Worktrees stay repo-relative regardless of where that store resolves.
# ---------------------------------------------------------------------------

def test_resolve_central_paths_uses_fabric_resolver(monkeypatch, tmp_path):
    """_resolve_central_paths must defer entirely to vnx_paths.resolve_paths()
    — not read $VNX_DATA_DIR itself — so it stays in lockstep with every
    other canonical caller (dispatch_register._register_path included)."""
    fake_data_dir = tmp_path / "central" / "vnx-dev"
    fake_state_dir = fake_data_dir / "some-other-state-subdir"
    monkeypatch.setattr(
        vnx_paths, "resolve_paths",
        lambda: {"VNX_DATA_DIR": str(fake_data_dir), "VNX_STATE_DIR": str(fake_state_dir)},
    )
    # A real $VNX_DATA_DIR is ALSO set (by the autouse isolation fixture) to a
    # DIFFERENT path — proves the resolver's answer wins, not a raw env read.
    assert os.environ.get("VNX_DATA_DIR") != str(fake_data_dir)

    data_dir, state_dir, error = osweep._resolve_central_paths(tmp_path / "some-repo")

    assert (data_dir, state_dir, error) == (fake_data_dir, fake_state_dir, None)


def test_resolve_central_paths_failure_records_error_and_falls_back(monkeypatch, tmp_path):
    def _raise():
        raise RuntimeError("no vnx runtime installed")

    monkeypatch.setattr(vnx_paths, "resolve_paths", _raise)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    repo_root = tmp_path / "repo"

    data_dir, state_dir, error = osweep._resolve_central_paths(repo_root)

    assert error is not None and "no vnx runtime installed" in error
    assert data_dir == repo_root / ".vnx-data"
    assert state_dir == repo_root / ".vnx-data" / "state"


def test_sweep_resolves_register_from_central_store_when_dirs_omitted(env, tmp_path):
    """Reproduces the measured bug (main 4b7d7b2f): a repo_root with NO
    .vnx-data/state of its own must still find a completed dispatch's
    register event, because sweep() resolves the register from the fabric's
    central store (here, the `env` fixture's pinned VNX_DATA_DIR_EXPLICIT=1)
    when --data-dir/--state-dir are both omitted — never from a repo-local
    fallback that has never heard of this project's dispatches."""
    local = _git_repo(tmp_path)  # repo_root has no .vnx-data/state of its own
    _write_register_event(env, "dispatch_completed", "central-1")

    res = osweep.sweep(
        repo_root=local,
        dry_run=True,
        list_sessions=lambda: ["vnx-central-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert len(res.tmux_completed_orphans_killed) == 1
    assert res.tmux_completed_orphans_killed[0]["dispatch_id"] == "central-1"
    assert res.tmux_completed_orphans_preserved == []
    assert not any(e.get("kind") == "data_dir_resolution" for e in res.errors)


def test_sweep_preserves_unknown_project_when_dirs_omitted_and_register_elsewhere(env, tmp_path):
    """The flip side of the reproduction above: a session whose dispatch id
    is NOT in the (correctly-resolved) central register must still read as
    unknown_project — proving the fix does not just turn every read into a
    hit, it resolves to the correct store and applies the same conjunction."""
    local = _git_repo(tmp_path)

    res = osweep.sweep(
        repo_root=local,
        dry_run=True,
        list_sessions=lambda: ["vnx-never-registered-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "unknown_project"


def test_sweep_records_error_when_central_resolver_fails(tmp_path, monkeypatch):
    """A failed central-store resolution must show up as a measurement error
    in ``errors`` — never pass silently as though the (possibly wrong)
    fallback store's emptiness were a valid 'zero dispatches known'."""
    def _raise():
        raise RuntimeError("vnx runtime not installed")

    monkeypatch.setattr(vnx_paths, "resolve_paths", _raise)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    local = _git_repo(tmp_path)

    res = osweep.sweep(repo_root=local, list_sessions=lambda: [])

    matches = [e for e in res.errors if e.get("kind") == "data_dir_resolution"]
    assert matches, (
        "a failed central-path resolution must be recorded as a measurement "
        "error, never silently treated as 'zero dispatches known'"
    )
    assert "vnx runtime not installed" in matches[0]["error"]


def test_explicit_state_dir_wins_over_central_resolution(monkeypatch, tmp_path):
    """An explicit state_dir always wins, even when data_dir is left to
    resolve via the fabric — the bogus resolver answer below must never be
    consulted for state_dir once it is given explicitly."""
    explicit_state_dir = tmp_path / "explicit-state"
    explicit_state_dir.mkdir()
    _write_register_event_in_state_dir(explicit_state_dir, "dispatch_completed", "explicit-1")
    local = _git_repo(tmp_path)

    bogus_central = tmp_path / "wrong-central-store"
    monkeypatch.setattr(
        vnx_paths, "resolve_paths",
        lambda: {
            "VNX_DATA_DIR": str(bogus_central),
            "VNX_STATE_DIR": str(bogus_central / "state"),
        },
    )

    res = osweep.sweep(
        repo_root=local,
        state_dir=explicit_state_dir,
        dry_run=True,
        list_sessions=lambda: ["vnx-explicit-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert len(res.tmux_completed_orphans_killed) == 1
    assert res.tmux_completed_orphans_killed[0]["dispatch_id"] == "explicit-1"
    assert not bogus_central.exists()  # resolver's answer was never touched


def test_explicit_data_dir_still_derives_state_dir_unchanged(env, monkeypatch, tmp_path):
    """An explicit data_dir (no state_dir) must still derive
    ``data_dir / "state"`` exactly as before — the central resolver must not
    even be consulted in this case (operator/test override path)."""
    local = _git_repo(tmp_path)
    _write_register_event(env, "dispatch_completed", "explicit-data-1")

    def _fail_if_called():
        raise AssertionError(
            "central resolver must not be consulted when data_dir is explicit"
        )

    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: _fail_if_called())

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        dry_run=True,
        list_sessions=lambda: ["vnx-explicit-data-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert len(res.tmux_completed_orphans_killed) == 1
    assert res.tmux_completed_orphans_killed[0]["dispatch_id"] == "explicit-data-1"


def test_worktree_kind_stays_repo_relative_when_central_store_differs(monkeypatch, tmp_path):
    """kind 2 (worktrees) must be derived from repo_root regardless of where
    data_dir/state_dir resolve to — only the register/receipts (1b) and
    active-manifest (3) kinds should ever follow the central store."""
    local = _git_repo(tmp_path)
    handle = _alloc(local, "wt-central-1")

    central_dir = tmp_path / "elsewhere-central"
    monkeypatch.setattr(
        vnx_paths, "resolve_paths",
        lambda: {"VNX_DATA_DIR": str(central_dir), "VNX_STATE_DIR": str(central_dir / "state")},
    )

    res = osweep.sweep(repo_root=local, list_sessions=lambda: [])

    assert len(res.worktrees_scanned) == 1
    assert str(handle.path) in res.worktrees_removed
    assert not handle.path.exists()
    assert str(handle.path).startswith(str(local)), (
        "worktree path must stay under repo_root even though data_dir "
        "resolved to a completely different central store"
    )
    assert not central_dir.exists(), (
        "the (bogus) central store must never be created/touched by the "
        "worktree kind"
    )


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


# ---------------------------------------------------------------------------
# OI-1424 — the tmux session listing is account-wide, but kind-1b's (b) check
# used to consult only THIS project's own register. A session belonging to a
# DIFFERENT project on the same account (own tmux server, own dispatch ids)
# read as unknown_project FOREVER, no matter how provably completed it was in
# ITS OWN project's register. This extends (b)/(c) to search every OTHER
# project's register sharing the account
# (<account-root>/<project_id>/state/dispatch_register.ndjson) once a
# session's dispatch id is unknown to the sweeping project's own register.
# ---------------------------------------------------------------------------

def _write_register_event_for_project(
    account_root: Path, project_id: str, event: str, dispatch_id: str,
) -> None:
    """Write a raw register event under <account_root>/<project_id>/state/ —
    simulates ANOTHER project's own dispatch_register.ndjson sharing this
    account (mirrors _write_register_event's raw-append style)."""
    state_dir = account_root / project_id / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "dispatch_register.ndjson"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": "2026-08-21T00:00:00.000000Z",
            "event": event,
            "dispatch_id": dispatch_id,
        }) + "\n")


def test_sweep_cross_project_owner_killed_and_inflight_preserved(env, tmp_path, monkeypatch):
    """The OI-1424 red-test target. Two REAL project stores: project-a's own
    register knows 'cross-done-1' as COMPLETED and 'cross-live-1' as still in
    flight; the sweeping project's own register (`env`) knows NEITHER id at
    all. Both sessions are alive on the (account-wide) shared tmux server.

    After the fix, a sweep run from the OWN project must (1) recognize
    cross-done-1 as a killable completed orphan using project-a's evidence,
    and (2) leave cross-live-1 alone. Both assertions live in one test on
    purpose: testing only the kill half would also pass an overly-aggressive
    sweep that treats every session unknown to its own project as completed
    by default — exactly the safety regression this must never introduce.

    On main pre-OI-1424 this fails on BEHAVIOR, not a missing symbol: sweep()
    runs to completion (VNX_DATA_HOME is simply never read), but BOTH
    sessions land in tmux_completed_orphans_preserved with reason
    "unknown_project" — project-a is never consulted, so cross-done-1 is
    never recognized as an orphan and never killed.
    """
    local = _git_repo(tmp_path)
    account_root = tmp_path / "account-root"
    _write_register_event_for_project(account_root, "project-a", "dispatch_created", "cross-done-1")
    _write_register_event_for_project(account_root, "project-a", "dispatch_completed", "cross-done-1")
    _write_register_event_for_project(account_root, "project-a", "dispatch_created", "cross-live-1")
    # Default resolution path (no explicit account_data_root kwarg) — proves
    # the fix's env-var fallback, not just its explicit-override plumbing.
    monkeypatch.setenv("VNX_DATA_HOME", str(account_root))

    killed = []
    res = osweep.sweep(
        repo_root=local,
        data_dir=env,  # this project's OWN register — knows neither id
        list_sessions=lambda: ["vnx-cross-done-1", "vnx-cross-live-1"],
        probe_liveness=lambda s: True,
        kill_session=lambda s: (killed.append(s), True)[1],
        capture_pane=lambda s: "",
    )

    killed_ids = {e["dispatch_id"] for e in res.tmux_completed_orphans_killed}
    preserved = {e["dispatch_id"]: e["reason"] for e in res.tmux_completed_orphans_preserved}

    # Half 1: the completed orphan owned by project-a IS recognized and killed.
    assert killed_ids == {"cross-done-1"}, (
        f"expected only cross-done-1 to be killed via project-a's evidence, "
        f"got killed={killed_ids} preserved={preserved}"
    )
    assert killed == ["vnx-cross-done-1"]
    assert res.tmux_completed_orphans_killed[0]["reason"] == (
        "register:dispatch_completed@project-a;pane=dead"
    )

    # Half 2: the still-in-flight session owned by project-a is LEFT ALONE —
    # proves the fix does not just kill every unknown-to-this-project session.
    assert preserved.get("cross-live-1") == "not_completed@project-a", (
        f"cross-live-1 must be preserved on project-a's not_completed ground, "
        f"got preserved={preserved}"
    )
    assert "vnx-cross-live-1" not in killed
    assert "vnx-cross-live-1" in res.tmux_skipped_alive


def test_sweep_cross_project_lookup_disabled_by_default_kwargs(tmp_path):
    """Direct unit-level control: _evaluate_completed_orphan with the new
    cross-project kwargs OMITTED must reproduce the exact pre-OI-1424
    behavior — existing callers that never pass them are unaffected."""
    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids=set(),
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
    )
    assert (is_orphan, reason) == (False, "unknown_project")


def test_evaluate_completed_orphan_cross_project_completed(tmp_path):
    """Direct unit test: dispatch id unknown to this project's register, but
    another project's register (injected via other_project_state_dirs) shows
    it COMPLETED — the conjunction succeeds using THAT project's evidence."""
    account_root = tmp_path / "account"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_completed", "y-1")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-y-1", "y-1",
        worktrees_dir=tmp_path,
        register_known_ids=set(),       # unknown to THIS project
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
        other_project_state_dirs=[account_root / "other-proj" / "state"],
        cross_project_cache={},
    )
    assert is_orphan is True
    assert reason == "register:dispatch_completed@other-proj;pane=dead"


def test_evaluate_completed_orphan_cross_project_not_completed(tmp_path):
    """The flip side: another project's register knows the id but has NOT
    proven a terminal outcome — never an orphan, ground is preserved and
    attributed to the correct owner."""
    account_root = tmp_path / "account"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_created", "y-2")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-y-2", "y-2",
        worktrees_dir=tmp_path,
        register_known_ids=set(),
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
        other_project_state_dirs=[account_root / "other-proj" / "state"],
        cross_project_cache={},
    )
    assert (is_orphan, reason) == (False, "not_completed@other-proj")


def test_evaluate_completed_orphan_cross_project_via_receipt(tmp_path):
    """Cross-project completion evidence via a terminal RECEIPT (not a
    register event) — mirrors the existing same-project receipt test."""
    account_root = tmp_path / "account"
    other_state_dir = account_root / "other-proj" / "state"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_created", "y-3")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-y-3", "y-3",
        worktrees_dir=tmp_path,
        register_known_ids=set(),
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
        other_project_state_dirs=[other_state_dir],
        cross_project_cache={
            other_state_dir: (
                {"y-3"}, set(), {"y-3": [{"event_type": "task_complete", "status": "success"}]},
            ),
        },
    )
    assert is_orphan is True
    assert reason == "receipt:success@other-proj;pane=dead"


def test_evaluate_completed_orphan_no_project_claims_it_stays_unknown(tmp_path):
    """No register — not this project's, not any other project's — has ever
    heard of this dispatch id. Must stay unknown_project, exactly as before
    OI-1424: absence everywhere is a real, measured 'nobody claims this', not
    a reason to guess."""
    account_root = tmp_path / "account"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_completed", "unrelated-id")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-z-1", "z-1",
        worktrees_dir=tmp_path,
        register_known_ids=set(),
        register_completed_ids=set(),
        receipts_index={},
        capture_pane=lambda s: "",
        other_project_state_dirs=[account_root / "other-proj" / "state"],
        cross_project_cache={},
    )
    assert (is_orphan, reason) == (False, "unknown_project")


def test_evaluate_completed_orphan_cross_project_own_project_takes_precedence(tmp_path):
    """A dispatch id known to THIS project's own register must be judged
    against ITS OWN evidence, never fall through to a cross-project lookup —
    even when other_project_state_dirs is populated with a store that would
    otherwise satisfy the conjunction differently."""
    account_root = tmp_path / "account"
    # A different project's register also knows this id but says NOT completed
    # — if own-project evidence didn't take precedence, this would wrongly win.
    _write_register_event_for_project(account_root, "other-proj", "dispatch_created", "x-1")

    is_orphan, reason = osweep._evaluate_completed_orphan(
        "vnx-x-1", "x-1",
        worktrees_dir=tmp_path,
        register_known_ids={"x-1"},          # THIS project already knows it
        register_completed_ids={"x-1"},      # and has proven it completed
        receipts_index={},
        capture_pane=lambda s: "",
        other_project_state_dirs=[account_root / "other-proj" / "state"],
        cross_project_cache={},
    )
    assert is_orphan is True
    assert reason == "register:dispatch_completed;pane=dead"  # no @owner suffix


def test_resolve_cross_project_owner_memoizes_per_state_dir(tmp_path):
    """Each candidate store is loaded at most once per sweep run, even when
    multiple sessions probe it — verifies the cache is actually consulted
    rather than re-reading the register from disk every call."""
    account_root = tmp_path / "account"
    other_state_dir = account_root / "other-proj" / "state"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_completed", "m-1")

    cache: "dict" = {}
    owner1 = osweep._resolve_cross_project_owner(
        "m-1", other_state_dirs=[other_state_dir], cache=cache,
    )
    assert owner1 is not None
    assert owner1[0] == "other-proj"
    assert other_state_dir in cache

    # Corrupt the on-disk register — a fresh (uncached) read would now see it
    # as unmeasurable/empty. The cached answer must still be returned as-is.
    (other_state_dir / "dispatch_register.ndjson").write_text("not json at all\n")
    owner2 = osweep._resolve_cross_project_owner(
        "m-1", other_state_dirs=[other_state_dir], cache=cache,
    )
    assert owner2 == owner1


def test_resolve_cross_project_owner_none_when_no_store_claims_id(tmp_path):
    account_root = tmp_path / "account"
    _write_register_event_for_project(account_root, "other-proj", "dispatch_completed", "known-1")

    owner = osweep._resolve_cross_project_owner(
        "totally-unclaimed-id",
        other_state_dirs=[account_root / "other-proj" / "state"],
        cache={},
    )
    assert owner is None


def test_enumerate_other_project_state_dirs_excludes_own_and_finds_others(tmp_path):
    account_root = tmp_path / "account"
    own_state_dir = account_root / "vnx-dev" / "state"
    own_state_dir.mkdir(parents=True)
    (own_state_dir / "dispatch_register.ndjson").write_text("")
    other_a = account_root / "project-a" / "state"
    other_a.mkdir(parents=True)
    (other_a / "dispatch_register.ndjson").write_text("")
    other_b = account_root / "project-b" / "state"
    other_b.mkdir(parents=True)
    (other_b / "dispatch_register.ndjson").write_text("")
    # A non-project account entry (e.g. locks/) with no register must never
    # be mistaken for a project store.
    (account_root / "locks").mkdir(parents=True)

    others = osweep._enumerate_other_project_state_dirs(
        account_root, exclude_state_dir=own_state_dir,
    )

    assert sorted(p.parent.name for p in others) == ["project-a", "project-b"]
    assert own_state_dir.resolve() not in [p.resolve() for p in others]


def test_enumerate_other_project_state_dirs_missing_root_is_empty_not_error(tmp_path):
    """A missing account root is a real 'nothing else here', not a
    measurement failure — Path.glob never raises on a missing directory."""
    others = osweep._enumerate_other_project_state_dirs(
        tmp_path / "does-not-exist", exclude_state_dir=tmp_path / "state",
    )
    assert others == []


def test_account_data_root_prefers_explicit_env_over_home_default(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "custom-home"))
    assert osweep._account_data_root() == tmp_path / "custom-home"


def test_account_data_root_falls_back_to_home_vnx_data(monkeypatch):
    monkeypatch.delenv("VNX_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/fake/home")))
    assert osweep._account_data_root() == Path("/fake/home") / ".vnx-data"


def test_sweep_cross_project_errors_recorded_and_fail_open(env, tmp_path, monkeypatch):
    """An unreadable OTHER project's register must be recorded in errors and
    must never be read as 'this id is completed' — fails open exactly like
    every other unmeasurable signal in this module."""
    local = _git_repo(tmp_path)
    account_root = tmp_path / "account-root"
    other_state_dir = account_root / "broken-proj" / "state"
    other_state_dir.mkdir(parents=True)
    (other_state_dir / "dispatch_register.ndjson").write_text("")

    import dispatch_register

    def _fake_read_events(*, since_iso=None, state_dir=None):
        if state_dir is not None and Path(state_dir).resolve() == other_state_dir.resolve():
            raise RuntimeError("other project's register blew up")
        return []  # this project's own (empty, never-written) register

    monkeypatch.setattr(dispatch_register, "read_events", _fake_read_events)

    res = osweep.sweep(
        repo_root=local,
        data_dir=env,
        account_data_root=account_root,
        list_sessions=lambda: ["vnx-broken-1"],
        probe_liveness=lambda s: True,
        capture_pane=lambda s: "",
    )

    assert res.tmux_completed_orphans_killed == []
    assert res.tmux_completed_orphans_preserved[0]["reason"] == "unknown_project"
    assert any(
        e.get("kind") == "completed_check" and e.get("project") == "broken-proj"
        for e in res.errors
    )


if __name__ == "__main__":
    import unittest

    raise SystemExit(pytest.main([__file__, "-q"]))
