"""D5 fabric-state-herstel: live work comes from what is running.

The index used to show live work through ``active_work`` (a scan of
``dispatches/active/``, a directory the headless door never writes), through
``queues.active`` (a ``.md`` count in that same directory) and through the
terminals (the headless lane uses none). The source that does know is the
``dispatches`` table in ``runtime_coordination.db`` plus the occupancy flock
the envelope holds for the whole run
(``dispatch_worktree_isolation._acquire_occupancy``).

ADR-007: every DB fixture carries a second project with a colliding
dispatch_id that must never leak into the first project's index.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "scripts"), str(_REPO_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402
from fixtures.dispatches_schema_fixture import dispatches_ddl  # noqa: E402

PROJECT_A = "fshd5-alpha"
PROJECT_B = "fshd5-bravo"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    for sub in ("pending", "active", "conflicts"):
        (dispatch_dir / sub).mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_A)
    # gh is never called from a test: pr_queue is injected.
    monkeypatch.setattr(bts, "_build_pr_queue_section", lambda _sd: {"open_prs": []})
    # The canonical multi-tenant table (UNIQUE(dispatch_id, project_id)), so a
    # colliding id in a second project can exist at all.
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute(dispatches_ddl())
        conn.execute("ALTER TABLE dispatches ADD COLUMN claimed_at TEXT")
    return state_dir, dispatch_dir


def _insert(state_dir: Path, dispatch_id: str, state: str, started: datetime,
            project_id: str = PROJECT_A) -> None:
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count,"
            " created_at, updated_at, claimed_at) VALUES (?, ?, ?, 0, ?, ?, ?)",
            (dispatch_id, project_id, state, _iso(started), _iso(started), _iso(started)),
        )


class _HeldFlock:
    """Hold the occupancy flock the way the envelope does: an LOCK_EX on an
    open file description, in a description the probe does not share."""

    def __init__(self, state_dir: Path, dispatch_id: str) -> None:
        claims = state_dir / "dispatch_worktree_claims"
        claims.mkdir(parents=True, exist_ok=True)
        self._fh = open(claims / f"{dispatch_id}.occupancy", "a")
        fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _released_lock_file(state_dir: Path, dispatch_id: str) -> None:
    claims = state_dir / "dispatch_worktree_claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{dispatch_id}.occupancy").touch()


def _index_live(index: Dict[str, Any]) -> Dict[str, Any]:
    return index.get("live_work") or {}


def _ids(entries: Optional[List[Dict[str, Any]]]) -> List[str]:
    return [e.get("dispatch_id") for e in (entries or [])]


@pytest.fixture
def held_locks():
    locks: List[_HeldFlock] = []
    yield locks
    for lock in locks:
        lock.release()


# ---------------------------------------------------------------------------
# Klaar-punten (red on the old code, green on the new)
# ---------------------------------------------------------------------------


def test_delivering_five_minutes_with_held_flock_is_live_in_index(
    tmp_path, monkeypatch, held_locks
):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _insert(state_dir, "d5-deliver", "delivering", now - timedelta(minutes=5))
    _insert(state_dir, "d5-deliver", "delivering", now - timedelta(minutes=5), PROJECT_B)
    held_locks.append(_HeldFlock(state_dir, "d5-deliver"))

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    live = _index_live(index)
    assert live.get("available") is True
    assert _ids(live.get("live")) == ["d5-deliver"]
    assert live.get("counts", {}).get("live") == 1


def test_running_ninety_minutes_with_held_flock_is_live_not_stale(
    tmp_path, monkeypatch, held_locks
):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    _insert(state_dir, "d5-longrun", "running",
            datetime.now(timezone.utc) - timedelta(minutes=90))
    held_locks.append(_HeldFlock(state_dir, "d5-longrun"))

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    live = _index_live(index)
    assert _ids(live.get("live")) == ["d5-longrun"]
    assert _ids(live.get("stale")) == []
    assert live["live"][0]["age_seconds"] >= 90 * 60


def test_delivering_three_days_without_lock_is_stale(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    _insert(state_dir, "d5-zombie", "delivering",
            datetime.now(timezone.utc) - timedelta(days=3))
    # 1,746 old lock files lie around in production: "file exists" is not
    # "lock held". This one exists and nobody holds it.
    _released_lock_file(state_dir, "d5-zombie")

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    live = _index_live(index)
    assert _ids(live.get("live")) == []
    assert _ids(live.get("stale")) == ["d5-zombie"]
    assert live["stale"][0]["lock"] == "released"


def test_in_flight_row_of_project_b_never_reaches_index_of_project_a(
    tmp_path, monkeypatch, held_locks
):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    # Colliding id: A's row is finished, B's row with the same id is live.
    _insert(state_dir, "d5-collide", "completed", now - timedelta(minutes=10))
    _insert(state_dir, "d5-collide", "running", now - timedelta(minutes=10), PROJECT_B)
    _insert(state_dir, "d5-only-b", "delivering", now - timedelta(minutes=10), PROJECT_B)
    held_locks.append(_HeldFlock(state_dir, "d5-collide"))
    held_locks.append(_HeldFlock(state_dir, "d5-only-b"))
    _insert(state_dir, "d5-mine", "claimed", now - timedelta(seconds=20))

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    live = _index_live(index)
    assert live.get("available") is True
    every_id = _ids(live.get("live")) + _ids(live.get("starting")) + _ids(live.get("stale"))
    assert every_id == ["d5-mine"]


def test_staging_bundle_counts_as_pending(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    bundle = dispatch_dir / "pending" / "20260929-staged"
    bundle.mkdir()
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    (bundle / "instruction.md").write_text("x", encoding="utf-8")
    # A gate bundle (final_prompt.md only) is not a staged dispatch.
    gate = dispatch_dir / "pending" / "glm-gate-pr1-123"
    gate.mkdir()
    (gate / "final_prompt.md").write_text("x", encoding="utf-8")

    queues = bts._build_queues(dispatch_dir, state_dir)

    assert queues["pending_count"] == 1


def test_locked_db_gives_available_false_with_reason_and_degraded_health(
    tmp_path, monkeypatch
):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    db = state_dir / "runtime_coordination.db"
    _insert(state_dir, "d5-deliver", "delivering",
            datetime.now(timezone.utc) - timedelta(minutes=5))
    # Rollback-journal mode: an EXCLUSIVE transaction blocks every reader.
    with sqlite3.connect(str(db)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
    # Only the live-work read is under test; schema init and the terminal
    # lease read would otherwise wait out their own 10 s timeouts.
    monkeypatch.setattr(bts, "_init_and_check_db", lambda _sd: True)
    monkeypatch.setattr(bts, "_build_terminals", lambda _sd: {})
    holder = sqlite3.connect(str(db), timeout=0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        state = bts.build_t0_state(state_dir, dispatch_dir)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    index = bts._build_t0_index(state)
    live = _index_live(index)
    assert live.get("available") is False
    assert "locked" in (live.get("reason") or "")
    assert state["system_health"]["status"] in ("degraded", "failed")
    assert any(
        "live_work" in r for r in state["system_health"].get("degraded_reasons", [])
    )


# ---------------------------------------------------------------------------
# Supporting behaviour (new code only)
# ---------------------------------------------------------------------------


def test_in_flight_states_are_derived_from_the_state_machine():
    import coordination_db as cdb

    assert cdb.IN_FLIGHT_DISPATCH_STATES == frozenset(
        {"claimed", "delivering", "accepted", "running"}
    )
    assert cdb.IN_FLIGHT_DISPATCH_STATES <= cdb.DISPATCH_STATES
    assert not cdb.IN_FLIGHT_DISPATCH_STATES & cdb.TERMINAL_DISPATCH_STATES


def test_young_row_without_lock_is_starting_not_stale(tmp_path, monkeypatch):
    state_dir, _ = _env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _insert(state_dir, "d5-young", "claimed", now - timedelta(seconds=30))

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []}, now=now)

    assert _ids(section["starting"]) == ["d5-young"]
    assert section["starting"][0]["lock"] == "absent"
    assert section["stale"] == []


def test_old_row_without_lock_file_is_stale_absent(tmp_path, monkeypatch):
    state_dir, _ = _env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _insert(state_dir, "d5-nofile", "accepted", now - timedelta(minutes=3))

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []}, now=now)

    assert _ids(section["stale"]) == ["d5-nofile"]
    assert section["stale"][0]["lock"] == "absent"


def test_open_pr_is_linked_to_its_dispatch_by_branch(tmp_path, monkeypatch, held_locks):
    state_dir, _ = _env(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _insert(state_dir, "d5-withpr", "running", now - timedelta(minutes=12))
    held_locks.append(_HeldFlock(state_dir, "d5-withpr"))
    pr_queue = {"open_prs": [
        {"number": 4242, "branch": "dispatch/d5-withpr"},
        {"number": 4243, "branch": "feature/manual"},
    ]}

    section = bts._build_live_work(state_dir, PROJECT_A, pr_queue, now=now)

    assert section["live"][0]["pr"] == 4242
    assert section["open_prs"] == [
        {"number": 4242, "dispatch_id": "d5-withpr"},
        {"number": 4243, "dispatch_id": None},
    ]


def test_missing_project_id_refuses_an_unscoped_read(tmp_path, monkeypatch):
    state_dir, _ = _env(tmp_path, monkeypatch)

    section = bts._build_live_work(state_dir, "", {"open_prs": []})

    assert section["available"] is False
    assert "project_id" in section["reason"]
    assert section["read_error"] is False


def test_missing_db_gives_available_false(tmp_path):
    section = bts._build_live_work(tmp_path / "nowhere", PROJECT_A, {"open_prs": []})

    assert section["available"] is False
    assert "runtime_coordination.db" in section["reason"]
    assert section["read_error"] is False


def test_pre_migration_schema_is_reported_but_does_not_degrade(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, state TEXT)")

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []})

    assert section["available"] is False
    assert section["reason"].startswith("premigration")
    assert section["read_error"] is False


def test_malformed_db_is_a_read_error(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "runtime_coordination.db").write_bytes(b"not a sqlite file" * 100)

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []})

    assert section["available"] is False
    assert section["read_error"] is True


def test_index_drops_terminals_active_and_active_dispatches(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    assert "terminals" not in index
    assert "active_dispatches" not in index
    assert "active" not in index["queue"]
    assert "live_work" in index
    assert len(json.dumps(index)) <= 5 * 1024


def test_index_caps_lists_but_keeps_true_counts(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(12):
        _insert(state_dir, f"d5-stale-{i:02d}", "delivering", old)

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    live = _index_live(index)
    assert live["counts"]["stale"] == 12
    assert len(live["stale"]) == bts._INDEX_LIVE_WORK_STALE_CAP
    assert len(json.dumps(index)) <= 5 * 1024


def test_brief_active_work_comes_from_live_work(tmp_path, monkeypatch, held_locks):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    _insert(state_dir, "d5-brief", "running",
            datetime.now(timezone.utc) - timedelta(minutes=7))
    held_locks.append(_HeldFlock(state_dir, "d5-brief"))

    brief = bts._state_to_brief(bts.build_t0_state(state_dir, dispatch_dir))

    assert [w["dispatch_id"] for w in brief["active_work"]] == ["d5-brief"]
    assert brief["queues"]["active"] == 1
