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


def test_lock_of_project_b_under_colliding_id_never_makes_row_of_a_live(
    tmp_path, monkeypatch
):
    """ADR-007 on the lock side: B holds the occupancy lock for the id that A
    also has in flight. B's lock is taken by the real writer, anchored on B's
    project root, so it lands in B's claim registry; A's probe reads A's."""
    import dispatch_worktree_isolation as dwi

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    state_a, _ = _env(tmp_path / "a", monkeypatch)
    state_b, _ = _env(tmp_path / "b", monkeypatch)
    now = datetime.now(timezone.utc)
    _insert(state_a, "d5-shared", "running", now - timedelta(minutes=10))
    _insert(state_b, "d5-shared", "running", now - timedelta(minutes=10), PROJECT_B)

    root_b = tmp_path / "b" / "repo"
    root_b.mkdir()
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_B)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(state_b.parent))
    assert dwi._claim_dir(root_b) == dwi.claims_dir_for_state_dir(state_b)
    wt_b = root_b / "wt"
    dwi._acquire_occupancy("d5-shared", wt_b, root_b)
    try:
        assert dwi.probe_occupancy(dwi.claims_dir_for_state_dir(state_b), "d5-shared") == "held"
        section_a = bts._build_live_work(state_a, PROJECT_A, {"open_prs": []}, now=now)
        section_b = bts._build_live_work(state_b, PROJECT_B, {"open_prs": []}, now=now)
    finally:
        dwi._release_occupancy(wt_b)

    assert _ids(section_a["live"]) == []
    assert _ids(section_a["stale"]) == ["d5-shared"]
    assert section_a["stale"][0]["lock"] == "absent"
    assert _ids(section_b["live"]) == ["d5-shared"]


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


# ---------------------------------------------------------------------------
# ff6: claimed_at comes with migration 0026 and is optional. A store without
# that migration (website-vincentvandeth, measured 29-09) still has live work.
# ---------------------------------------------------------------------------


def _store_without_0026(tmp_path: Path) -> Path:
    """Canonical multi-tenant dispatches table, migration 0026 never applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute(dispatches_ddl())
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatches)")}
        assert "claimed_at" not in cols
        started = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        for project_id in (PROJECT_A, PROJECT_B):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count,"
                " created_at, updated_at) VALUES (?, ?, 'running', 0, ?, ?)",
                ("ff6-collide", project_id, started, started),
            )
    return state_dir


def test_store_without_claim_migration_still_reads_live_work(tmp_path, held_locks):
    state_dir = _store_without_0026(tmp_path)
    held_locks.append(_HeldFlock(state_dir, "ff6-collide"))

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []})

    assert section["available"] is True, section.get("reason")
    assert _ids(section["live"]) == ["ff6-collide"]
    assert section["counts"]["live"] == 1
    # Project B's row under the same id never shows up in A's section.
    assert sum(section["counts"].values()) == 1
    rows = bts._read_in_flight_rows(
        state_dir / "runtime_coordination.db", PROJECT_A, sorted(section["in_flight_states"]),
    )
    assert len(rows) == 1
    assert rows[0][0] == "ff6-collide"
    assert rows[0][-1] is None  # claimed_at: no column, NULL in its place


def test_store_without_project_id_column_names_the_missing_column(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute(
            "CREATE TABLE dispatches (dispatch_id TEXT, state TEXT, track TEXT,"
            " gate TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, created_at, updated_at)"
            " VALUES ('ff6-noproj', 'running', '2026-09-29T10:00:00Z', '2026-09-29T10:00:00Z')"
        )

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []})

    assert section["available"] is False
    assert section["read_error"] is False
    assert "dispatches lacks required column(s): project_id" in section["reason"]


def test_store_without_dispatches_table_says_so(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute("CREATE TABLE unrelated (x TEXT)")

    section = bts._build_live_work(state_dir, PROJECT_A, {"open_prs": []})

    assert section["available"] is False
    assert section["read_error"] is False
    assert section["reason"] == "premigration: no dispatches table"


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


# ---------------------------------------------------------------------------
# Fix-forward 2 (deepseek_gate on PR #1984)
# ---------------------------------------------------------------------------


def test_index_with_live_work_carries_schema_1_1(tmp_path, monkeypatch):
    """The index shape changed (live_work in; terminals, queue.active and
    active_dispatches out), so the schema version moves with it."""
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)

    index = bts._build_t0_index(bts.build_t0_state(state_dir, dispatch_dir))

    assert "live_work" in index
    assert index["schema"] == "t0_index/1.1"


def test_legacy_md_in_pending_counts_as_pending(tmp_path, monkeypatch):
    """queue_auto_accept.sh (still started by vnx_supervisor_simple.sh) moves
    ``queue/<id>.md`` to ``pending/<id>.md``: that is a pending dispatch too."""
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    pending = dispatch_dir / "pending"
    (pending / "20260929-legacy.md").write_text("x", encoding="utf-8")
    bundle = pending / "20260929-staged"
    bundle.mkdir()
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    # The same id in both forms is one dispatch, not two.
    (pending / "20260929-staged.md").write_text("x", encoding="utf-8")
    (pending / "notes.txt").write_text("x", encoding="utf-8")

    queues = bts._build_queues(dispatch_dir, state_dir)

    assert queues["pending_count"] == 2


def test_failed_lock_probe_is_unmeasured_and_shown_by_vnx_status(
    tmp_path, monkeypatch, capsys
):
    """A probe that raises is not "nothing running": the row lands in
    ``unmeasured`` and the human view names it."""
    import dispatch_worktree_isolation as dwi

    cli_dir = str(_REPO_ROOT / "scripts" / "cli")
    if cli_dir not in sys.path:
        sys.path.insert(0, cli_dir)
    import vnx_status as vs

    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    _insert(state_dir, "d5-unprobed", "running",
            datetime.now(timezone.utc) - timedelta(minutes=30))
    _insert(state_dir, "d5-unprobed", "running",
            datetime.now(timezone.utc) - timedelta(minutes=30), PROJECT_B)

    def _raising_probe(_claims_dir, _dispatch_id):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(dwi, "probe_occupancy", _raising_probe)
    state = bts.build_t0_state(state_dir, dispatch_dir)
    assert _ids(state["live_work"]["unmeasured"]) == ["d5-unprobed"]
    assert _ids(state["live_work"]["live"]) == []

    (state_dir / "t0_state.json").write_text(json.dumps(state), encoding="utf-8")
    capsys.readouterr()
    assert vs.main(argv=[], data_dir=tmp_path) == 0
    out = capsys.readouterr().out

    unmeasured_lines = [line for line in out.splitlines() if "d5-unprobed" in line]
    assert len(unmeasured_lines) == 1
    assert "unmeasured" in unmeasured_lines[0]
    assert "nothing in flight" not in out


def _status_sh_summary(tmp_path: Path, state: Dict[str, Any]) -> str:
    """Run the REAL _s_print_summary from scripts/commands/status.sh against
    *state* (only the state reader and the header are stubbed)."""
    import subprocess

    state_file = tmp_path / "t0_state_for_status_sh.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    script = (
        'source "$1"\n'
        '_s_header() { echo "# $1"; }\n'
        '_s_t0_state() { cat "$VNX_TEST_STATE_FILE"; }\n'
        '_s_print_summary\n'
    )
    status_sh = _REPO_ROOT / "scripts" / "commands" / "status.sh"
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(status_sh)],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "VNX_TEST_STATE_FILE": str(state_file)},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_status_sh_active_line_reads_live_work_including_unmeasured(tmp_path):
    """queues.active_count is gone; status.sh's Active line printed "?"."""
    out = _status_sh_summary(tmp_path, {
        "queues": {"pending_count": 2},
        "live_work": {"available": True, "counts": {
            "live": 1, "starting": 1, "stale": 2, "unmeasured": 1}},
    })

    assert "Active:           2 (stale: 2, unmeasured: 1)" in out


def test_status_sh_active_line_says_why_live_work_is_unavailable(tmp_path):
    out = _status_sh_summary(tmp_path, {
        "queues": {"pending_count": 0},
        "live_work": {"available": False, "reason": "degraded: database is locked"},
    })

    assert "Active:           unavailable: degraded: database is locked" in out


# ---------------------------------------------------------------------------
# ff5: an unreadable queue directory is not zero (the guard reads these counts)
# ---------------------------------------------------------------------------


def _unreadable(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """``target`` exists, but listing it raises PermissionError."""
    real_iterdir = Path.iterdir

    def _iterdir(self: Path):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)


def _second_project_with_staged(tmp_path: Path, dispatch_id: str) -> tuple[Path, Path]:
    """Project B: its own store and a readable pending/ holding ``dispatch_id``."""
    root = tmp_path / "project-b"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    dispatch_dir = root / "dispatches"
    for sub in ("pending", "active", "conflicts"):
        (dispatch_dir / sub).mkdir(parents=True)
    bundle = dispatch_dir / "pending" / dispatch_id
    bundle.mkdir()
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    return state_dir, dispatch_dir


def test_unreadable_pending_dir_is_not_zero_and_degrades_health(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    staged = dispatch_dir / "pending" / "20260929-shared"
    staged.mkdir()
    (staged / "dispatch-spec.json").write_text("{}", encoding="utf-8")
    b_state, b_dispatch = _second_project_with_staged(tmp_path, "20260929-shared")
    monkeypatch.setattr(bts, "_init_and_check_db", lambda _sd: True)
    monkeypatch.setattr(bts, "_build_terminals", lambda _sd: {})
    _unreadable(monkeypatch, dispatch_dir / "pending")

    state = bts.build_t0_state(state_dir, dispatch_dir)

    queues = state["queues"]
    assert queues["pending_count"] != 0, "an unreadable pending/ must not read as empty"
    assert queues["pending_count"] is None
    assert "PermissionError" in queues["pending_unmeasured_reason"]
    assert state["system_health"]["status"] in ("degraded", "failed")
    assert any(
        r.startswith("queues.pending_count unmeasured")
        for r in state["system_health"].get("degraded_reasons", [])
    )
    # Project B (colliding id, readable dir) keeps its own measured count.
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_B)
    queues_b = bts._build_queues(b_dispatch, b_state)
    assert queues_b["pending_count"] == 1
    assert "pending_unmeasured_reason" not in queues_b


def test_missing_pending_dir_stays_zero(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    (dispatch_dir / "pending").rmdir()
    b_state, b_dispatch = _second_project_with_staged(tmp_path, "20260929-shared")

    queues = bts._build_queues(dispatch_dir, state_dir)

    assert queues["pending_count"] == 0
    assert "pending_unmeasured_reason" not in queues
    assert bts._build_queues(b_dispatch, b_state)["pending_count"] == 1


def test_unreadable_conflicts_dir_is_not_zero_and_degrades_health(tmp_path, monkeypatch):
    state_dir, dispatch_dir = _env(tmp_path, monkeypatch)
    (dispatch_dir / "conflicts" / "20260929-shared.md").write_text("x", encoding="utf-8")
    b_state, b_dispatch = _second_project_with_staged(tmp_path, "20260929-shared")
    monkeypatch.setattr(bts, "_init_and_check_db", lambda _sd: True)
    monkeypatch.setattr(bts, "_build_terminals", lambda _sd: {})
    _unreadable(monkeypatch, dispatch_dir / "conflicts")

    state = bts.build_t0_state(state_dir, dispatch_dir)

    queues = state["queues"]
    assert queues["conflict_count"] != 0, "an unreadable conflicts/ must not read as empty"
    assert queues["conflict_count"] is None
    assert "PermissionError" in queues["conflict_unmeasured_reason"]
    assert state["system_health"]["status"] in ("degraded", "failed")
    assert any(
        r.startswith("queues.conflict_count unmeasured")
        for r in state["system_health"].get("degraded_reasons", [])
    )
    queues_b = bts._build_queues(b_dispatch, b_state)
    assert queues_b["conflict_count"] == 0
    assert "conflict_unmeasured_reason" not in queues_b
