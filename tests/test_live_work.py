"""D5 fabric-state-herstel: live_work in t0_state / t0_index.

Runs the real builder (``build_t0_state``) against a tmp store with a real
``dispatches`` table and real occupancy flocks. Every test carries a second
project whose rows and dispatch ids collide with project A (ADR-007).
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import build_t0_state as bts  # noqa: E402
import live_work  # noqa: E402

PROJECT_A = "proj-a"
PROJECT_B = "proj-b"

_DISPATCHES_DDL = """
CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dispatch_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'proposed',
  terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_after TEXT,
  metadata_json TEXT DEFAULT '{}', operator_approved_at TEXT,
  claimed_by TEXT, claimed_at TEXT, output_ref TEXT, output_kind TEXT,
  UNIQUE (dispatch_id, project_id)
)
"""


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Store:
    def __init__(self, root: Path):
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.dispatch_dir = root / "dispatches"
        (self.dispatch_dir / "pending").mkdir(parents=True)
        self.claims = self.state_dir / "dispatch_worktree_claims"
        self.claims.mkdir()
        self.db = self.state_dir / "runtime_coordination.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute(_DISPATCHES_DDL)
        self.held = []

    def add(self, project, dispatch_id, state, age: timedelta):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state, created_at, updated_at, track) "
                "VALUES (?, ?, ?, ?, ?, 'A')",
                (dispatch_id, project, state, _iso(age), _iso(age)),
            )

    def hold_lock(self, dispatch_id):
        """Hold the occupancy flock exactly like _acquire_occupancy does."""
        fh = open(self.claims / f"{dispatch_id}.occupancy", "a")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.held.append(fh)

    def leave_unlocked_file(self, dispatch_id):
        (self.claims / f"{dispatch_id}.occupancy").write_text("")

    def release_all(self):
        for fh in self.held:
            fh.close()
        self.held.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_A)
    (tmp_path / ".vnx-project-id").write_text(PROJECT_A)
    # No gh call from a test: open PRs are injected per test.
    monkeypatch.setattr(bts, "_build_pr_queue_section", lambda state_dir: {"open_prs": []})
    s = Store(tmp_path)
    yield s
    s.release_all()


def _build(store):
    return bts.build_t0_state(store.state_dir, store.dispatch_dir)


def _by_id(state):
    return {d["dispatch_id"]: d for d in state["live_work"]["dispatches"]}


def test_delivering_five_minutes_with_held_lock_is_live(store):
    store.add(PROJECT_A, "d-deliver", "delivering", timedelta(minutes=5))
    store.hold_lock("d-deliver")
    state = _build(store)
    assert _by_id(state).get("d-deliver", {}).get("status") == "live"
    assert "d-deliver" in bts._build_t0_index(state)["live_work"]["live"]


def test_running_ninety_minutes_with_held_lock_is_live_not_stale(store):
    store.add(PROJECT_A, "d-long", "running", timedelta(minutes=90))
    store.hold_lock("d-long")
    item = _by_id(_build(store)).get("d-long", {})
    assert item.get("status") == "live"
    assert item.get("age_seconds", 0) >= 90 * 60 - 5


def test_delivering_three_days_without_lock_is_stale(store):
    store.add(PROJECT_A, "d-zombie", "delivering", timedelta(days=3))
    state = _build(store)
    assert _by_id(state).get("d-zombie", {}).get("status") == "stale"
    assert bts._build_t0_index(state)["live_work"]["stale"] == ["d-zombie"]


def test_leftover_lock_file_without_holder_is_not_alive(store):
    # 1.7k old .occupancy files exist in production: "file exists" is not "lock held".
    store.add(PROJECT_A, "d-old", "running", timedelta(hours=5))
    store.leave_unlocked_file("d-old")
    assert _by_id(_build(store)).get("d-old", {}).get("status") == "stale"


def test_young_row_without_lock_is_starting_not_stale(store):
    store.add(PROJECT_A, "d-new", "claimed", timedelta(seconds=20))
    state = _build(store)
    assert _by_id(state).get("d-new", {}).get("status") == "starting"
    assert state["live_work"]["counts"] == {"live": 0, "starting": 1, "stale": 0}


def test_lock_dies_with_its_holder(store):
    store.add(PROJECT_A, "d-crash", "running", timedelta(minutes=30))
    script = textwrap.dedent(f"""
        import fcntl, sys, time
        fh = open({str(store.claims / 'd-crash.occupancy')!r}, "a")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert _by_id(_build(store)).get("d-crash", {}).get("status") == "live"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert _by_id(_build(store)).get("d-crash", {}).get("status") == "stale"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_other_project_rows_and_colliding_ids_do_not_leak(store):
    # Same dispatch_id in both projects; B's is in flight, A's is completed.
    store.add(PROJECT_A, "d-shared", "completed", timedelta(hours=1))
    store.add(PROJECT_B, "d-shared", "running", timedelta(minutes=10))
    store.add(PROJECT_B, "d-only-b", "delivering", timedelta(days=2))
    store.add(PROJECT_A, "d-mine", "running", timedelta(minutes=10))
    store.hold_lock("d-mine")
    state = _build(store)
    assert set(_by_id(state)) == {"d-mine"}
    index = bts._build_t0_index(state)
    assert "d-shared" not in json.dumps(index) and "d-only-b" not in json.dumps(index)


@pytest.mark.parametrize("state_name", ["proposed", "ready", "queued", "completed", "expired",
                                        "dead_letter", "timed_out", "failed_delivery", "recovered"])
def test_states_outside_flight_are_not_listed(store, state_name):
    store.add(PROJECT_A, "d-x", state_name, timedelta(minutes=1))
    assert _by_id(_build(store)) == {}


def test_in_flight_states_come_from_the_state_machine():
    assert live_work.IN_FLIGHT_STATES == {"claimed", "delivering", "accepted", "running"}


def test_staged_bundle_counts_in_pending_but_gate_bundle_and_md_do_not(store):
    pending = store.dispatch_dir / "pending"
    staged = pending / "20260929-staged"
    staged.mkdir()
    (staged / "dispatch-spec.json").write_text("{}")
    gate = pending / "glm-gate-abc"
    gate.mkdir()
    (gate / "final_prompt.md").write_text("x")
    (pending / "loose.md").write_text("x")
    (pending / "loose2.md").write_text("x")
    state = _build(store)
    assert state["queues"]["pending_count"] == 1
    assert bts._build_t0_index(state)["queue"]["pending"] == 1
    assert "active_count" not in state["queues"]


def test_open_pr_is_linked_to_its_dispatch_by_branch(store, monkeypatch):
    store.add(PROJECT_A, "20260929-fsh-d5", "running", timedelta(minutes=10))
    store.hold_lock("20260929-fsh-d5")
    monkeypatch.setattr(bts, "_build_pr_queue_section", lambda state_dir: {"open_prs": [
        {"number": 1980, "branch": "dispatch/20260929-fsh-d5", "ci_status": "success"},
        {"number": 1981, "branch": "feature/manual", "ci_status": "pending"},
    ]})
    state = _build(store)
    prs = {p["number"]: p for p in state["live_work"]["open_prs"]}
    assert prs[1980]["dispatch_id"] == "20260929-fsh-d5"
    assert prs[1980]["dispatch_in_flight"] is True
    assert prs[1981]["dispatch_id"] is None
    idx = bts._build_t0_index(state)["live_work"]["open_prs"]
    assert {"number": 1980, "dispatch_id": "20260929-fsh-d5"} in idx


def test_locked_db_gives_unavailable_with_reason_and_degraded_health(store, monkeypatch):
    store.add(PROJECT_A, "d-1", "running", timedelta(minutes=1))
    monkeypatch.setattr(live_work, "DB_BUSY_TIMEOUT_SECONDS", 0.05)
    blocker = sqlite3.connect(store.db, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        section = live_work.build_live_work(store.state_dir, PROJECT_A)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert section["available"] is False
    assert "database is locked" in section["reason"]
    assert section["dispatches"] == []


def test_builder_marks_health_degraded_with_the_reason(store, monkeypatch):
    monkeypatch.setattr(bts, "build_live_work", lambda *a, **kw: {
        **live_work.build_live_work(store.state_dir, ""), "reason": "OperationalError: database is locked",
    })
    state = _build(store)
    assert state["live_work"]["available"] is False
    assert state["system_health"]["status"] in ("degraded", "failed")
    assert "database is locked" in state["system_health"]["degraded_reason"]
    idx = bts._build_t0_index(state)["live_work"]
    assert idx == {"available": False, "reason": "OperationalError: database is locked"}


def test_missing_project_id_is_unavailable_not_empty(store):
    section = live_work.build_live_work(store.state_dir, "")
    assert section["available"] is False and section["reason"] == "project_id_unavailable"


def test_terminal_snapshot_failure_carries_its_exception(store, monkeypatch):
    import canonical_state_views

    def boom(state_dir):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(canonical_state_views, "build_terminal_snapshot", boom)
    terminals = bts._build_terminals(store.state_dir)
    assert terminals["T1"]["status"] == "unknown"
    assert "database is locked" in terminals["T1"].get("reason", "")


def test_index_stays_within_five_kb_with_many_stale_rows(store):
    for n in range(300):
        store.add(PROJECT_A, f"d-stale-{n:04d}-{'x' * 40}", "delivering", timedelta(days=2))
    index = bts._build_t0_index(_build(store))
    assert len(json.dumps(index)) < 5 * 1024
    assert index["live_work"]["counts"]["stale"] == 300
    assert len(index["live_work"]["stale"]) == 10
