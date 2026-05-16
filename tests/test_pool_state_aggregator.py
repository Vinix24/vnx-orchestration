"""test_pool_state_aggregator.py — Tests for pool_state_unified aggregation + supervisor.

Covers:
- build_central_view creates pool_state_unified table with correct schema
- Cross-project pool data is aggregated from multiple project DBs
- Starvation detection (active_count < min_workers)
- Capacity-bound detection (active_count >= max_workers)
- emit_supervisor_event appends valid NDJSON
- Concurrent emit with fcntl.flock produces no interleaved records

Wave 6 PR-6.8 — ADR-018 Control Centre pool-integration.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.aggregator.build_central_view import (
    ProjectEntry,
    build_pool_state_unified,
    materialize_views,
)
from scripts.control_centre.pool_supervisor import (
    detect_capacity_bound,
    detect_starvation,
    emit_supervisor_event,
    list_all_pools,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_POOL_SCHEMA = """
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS runtime_schema_version (
    version INTEGER PRIMARY KEY, description TEXT,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO runtime_schema_version(version, description)
VALUES (14, 'test-v14');

CREATE TABLE IF NOT EXISTS terminal_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id TEXT NOT NULL, project_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'idle', lease_token TEXT NOT NULL DEFAULT '',
    last_heartbeat_at TEXT,
    UNIQUE(terminal_id, project_id)
);

CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    state TEXT NOT NULL DEFAULT 'queued',
    UNIQUE(dispatch_id, project_id)
);

CREATE TABLE IF NOT EXISTS pool_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL, pool_id TEXT NOT NULL DEFAULT 'default',
    min_workers INTEGER NOT NULL DEFAULT 1, max_workers INTEGER NOT NULL DEFAULT 6,
    target_workers INTEGER NOT NULL DEFAULT 3,
    role_mix_json TEXT NOT NULL DEFAULT '["backend-developer"]',
    provider_mix_json TEXT NOT NULL DEFAULT '["claude"]',
    scale_policy TEXT NOT NULL DEFAULT 'queue_depth_v1',
    cooldown_seconds INTEGER NOT NULL DEFAULT 120,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(project_id, pool_id)
);

CREATE TABLE IF NOT EXISTS worker_pools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL, pool_id TEXT NOT NULL DEFAULT 'default',
    state TEXT NOT NULL DEFAULT 'idle',
    current_size INTEGER NOT NULL DEFAULT 0, target_size INTEGER NOT NULL DEFAULT 0,
    healthy_count INTEGER NOT NULL DEFAULT 0, stuck_count INTEGER NOT NULL DEFAULT 0,
    last_scaled_at TEXT, last_scale_action TEXT,
    last_decision_json TEXT DEFAULT '{}', metadata_json TEXT DEFAULT '{}',
    UNIQUE(project_id, pool_id),
    FOREIGN KEY (project_id, pool_id) REFERENCES pool_config(project_id, pool_id)
);

CREATE TABLE IF NOT EXISTS worker_pool_membership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_id TEXT NOT NULL, project_id TEXT NOT NULL,
    pool_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL, role TEXT NOT NULL,
    joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at TEXT, release_reason TEXT,
    spawn_generation INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pool_membership_active
    ON worker_pool_membership(terminal_id, project_id)
    WHERE released_at IS NULL;

PRAGMA foreign_keys = ON;
"""


def _make_project_db(
    state_dir: Path,
    project_id: str,
    pool_id: str = "default",
    min_workers: int = 1,
    max_workers: int = 4,
    scale_policy: str = "queue_depth_v1",
    active_members: int = 0,
    reaped_members: int = 0,
) -> None:
    """Create a runtime_coordination.db with pool tables seeded for testing."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(_POOL_SCHEMA)
        con.execute(
            """INSERT OR IGNORE INTO pool_config
               (project_id, pool_id, min_workers, max_workers, target_workers, scale_policy)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, pool_id, min_workers, max_workers, min_workers + 1, scale_policy),
        )
        con.execute(
            """INSERT OR IGNORE INTO worker_pools
               (project_id, pool_id, state, current_size, target_size)
               VALUES (?, ?, 'idle', 0, ?)""",
            (project_id, pool_id, min_workers + 1),
        )
        # Insert active members (released_at IS NULL)
        for i in range(active_members):
            con.execute(
                """INSERT OR IGNORE INTO terminal_leases
                   (terminal_id, project_id, state) VALUES (?, ?, 'active')""",
                (f"{project_id}-T{i}", project_id),
            )
            con.execute(
                """INSERT INTO worker_pool_membership
                   (terminal_id, project_id, pool_id, provider, role, joined_at)
                   VALUES (?, ?, ?, 'claude', 'backend-developer', '2026-05-16T08:00:00Z')""",
                (f"{project_id}-T{i}", project_id, pool_id),
            )
        # Insert reaped members (released_at IS NOT NULL)
        for i in range(reaped_members):
            tid = f"{project_id}-reaped-T{i}"
            con.execute(
                """INSERT OR IGNORE INTO terminal_leases
                   (terminal_id, project_id, state) VALUES (?, ?, 'idle')""",
                (tid, project_id),
            )
            con.execute(
                """INSERT INTO worker_pool_membership
                   (terminal_id, project_id, pool_id, provider, role,
                    joined_at, released_at, release_reason)
                   VALUES (?, ?, ?, 'claude', 'backend-developer',
                           '2026-05-16T07:00:00Z', '2026-05-16T08:00:00Z', 'stale_heartbeat')""",
                (tid, project_id, pool_id),
            )
        con.commit()
    finally:
        con.close()


def _make_registry(tmp_path: Path, specs: list[dict]) -> Path:
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"schema_version": 1, "projects": specs}))
    return reg


# ---------------------------------------------------------------------------
# 1. pool_state_unified table is created by build_central_view
# ---------------------------------------------------------------------------

def test_aggregator_creates_pool_state_unified_view(tmp_path):
    proj_dir = tmp_path / "proj-a"
    _make_project_db(proj_dir / ".vnx-data" / "state", "proj-a", active_members=2)

    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="proj-a", path=proj_dir, project_id="proj-a")]
    materialize_views(view_db, projects)

    con = sqlite3.connect(str(view_db))
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pool_state_unified'"
        )
        assert cur.fetchone() is not None, "pool_state_unified table should exist"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. Cross-project pool data is aggregated
# ---------------------------------------------------------------------------

def test_pool_state_unified_shows_all_projects(tmp_path):
    for pid, active in [("proj-alpha", 2), ("proj-beta", 1), ("proj-gamma", 3)]:
        proj_dir = tmp_path / pid
        _make_project_db(
            proj_dir / ".vnx-data" / "state", pid,
            min_workers=1, max_workers=4, active_members=active,
        )

    view_db = tmp_path / "data.db"
    projects = [
        ProjectEntry(name=pid, path=tmp_path / pid, project_id=pid)
        for pid in ["proj-alpha", "proj-beta", "proj-gamma"]
    ]
    materialize_views(view_db, projects)

    pools = list_all_pools(view_db)
    project_ids = {p["project_id"] for p in pools}
    assert project_ids == {"proj-alpha", "proj-beta", "proj-gamma"}

    counts = {p["project_id"]: p["active_count"] for p in pools}
    assert counts["proj-alpha"] == 2
    assert counts["proj-beta"] == 1
    assert counts["proj-gamma"] == 3


# ---------------------------------------------------------------------------
# 3. Active vs reaped count accuracy
# ---------------------------------------------------------------------------

def test_pool_state_unified_member_counts(tmp_path):
    proj_dir = tmp_path / "proj-counts"
    _make_project_db(
        proj_dir / ".vnx-data" / "state", "proj-counts",
        active_members=3, reaped_members=2,
    )
    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="proj-counts", path=proj_dir, project_id="proj-counts")]
    materialize_views(view_db, projects)

    pools = list_all_pools(view_db)
    assert len(pools) == 1
    assert pools[0]["active_count"] == 3
    assert pools[0]["reaped_count"] == 2


# ---------------------------------------------------------------------------
# 4. Starvation detection
# ---------------------------------------------------------------------------

def test_starvation_detection_below_min():
    pools = [
        {"project_id": "p1", "pool_id": "default", "active_count": 0, "min_workers": 2, "max_workers": 4},
        {"project_id": "p2", "pool_id": "default", "active_count": 2, "min_workers": 2, "max_workers": 4},
        {"project_id": "p3", "pool_id": "default", "active_count": 1, "min_workers": 3, "max_workers": 6},
    ]
    starved = detect_starvation(pools)
    assert len(starved) == 2
    ids = {p["project_id"] for p in starved}
    assert ids == {"p1", "p3"}


def test_starvation_detection_none_when_at_min():
    pools = [
        {"project_id": "p1", "pool_id": "default", "active_count": 1, "min_workers": 1, "max_workers": 4},
        {"project_id": "p2", "pool_id": "default", "active_count": 3, "min_workers": 2, "max_workers": 6},
    ]
    assert detect_starvation(pools) == []


# ---------------------------------------------------------------------------
# 5. Capacity-bound detection
# ---------------------------------------------------------------------------

def test_capacity_bound_detection_at_max():
    pools = [
        {"project_id": "p1", "pool_id": "default", "active_count": 4, "min_workers": 1, "max_workers": 4},
        {"project_id": "p2", "pool_id": "default", "active_count": 2, "min_workers": 1, "max_workers": 4},
        {"project_id": "p3", "pool_id": "default", "active_count": 6, "min_workers": 1, "max_workers": 6},
    ]
    bound = detect_capacity_bound(pools)
    assert len(bound) == 2
    ids = {p["project_id"] for p in bound}
    assert ids == {"p1", "p3"}


def test_capacity_bound_detection_none_below_max():
    pools = [
        {"project_id": "p1", "pool_id": "default", "active_count": 2, "min_workers": 1, "max_workers": 4},
    ]
    assert detect_capacity_bound(pools) == []


# ---------------------------------------------------------------------------
# 6. emit_supervisor_event appends valid NDJSON
# ---------------------------------------------------------------------------

def test_emit_supervisor_event_appends_ndjson(tmp_path):
    events_path = tmp_path / "events" / "pool_decisions.ndjson"
    emit_supervisor_event(events_path, "pool.supervisor.starvation", {"project_id": "p1"})
    emit_supervisor_event(events_path, "pool.supervisor.capacity_bound", {"project_id": "p2"})

    lines = [l for l in events_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2

    ev1 = json.loads(lines[0])
    assert ev1["event_type"] == "pool.supervisor.starvation"
    assert ev1["payload"]["project_id"] == "p1"
    assert "timestamp" in ev1
    assert ev1["timestamp"].endswith("Z")

    ev2 = json.loads(lines[1])
    assert ev2["event_type"] == "pool.supervisor.capacity_bound"


def test_emit_supervisor_event_creates_parent_dirs(tmp_path):
    deep_path = tmp_path / "a" / "b" / "c" / "pool_decisions.ndjson"
    emit_supervisor_event(deep_path, "pool.supervisor.test", {"x": 1})
    assert deep_path.exists()
    ev = json.loads(deep_path.read_text().splitlines()[0])
    assert ev["event_type"] == "pool.supervisor.test"


# ---------------------------------------------------------------------------
# 7. Concurrent emit with fcntl.flock — no interleaved records
# ---------------------------------------------------------------------------

def test_concurrent_emit_atomic(tmp_path):
    events_path = tmp_path / "events" / "pool_decisions.ndjson"
    errors: list[Exception] = []

    def _emit(i: int) -> None:
        try:
            emit_supervisor_event(
                events_path,
                "pool.supervisor.concurrent_test",
                {"thread": i, "data": "x" * 256},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_emit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent emit raised: {errors}"

    lines = [l for l in events_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 20, f"Expected 20 events, got {len(lines)}"

    # Every line must be valid JSON with correct structure
    for line in lines:
        ev = json.loads(line)
        assert ev["event_type"] == "pool.supervisor.concurrent_test"
        assert "thread" in ev["payload"]


# ---------------------------------------------------------------------------
# 8. Projects with no pool tables are skipped cleanly
# ---------------------------------------------------------------------------

def test_projects_without_pool_tables_skipped(tmp_path):
    """Projects with only quality_intelligence.db (no pool tables) must not crash."""
    proj_dir = tmp_path / "no-pool-proj"
    state_dir = proj_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)

    # Create runtime_coordination.db WITHOUT pool tables
    db_path = state_dir / "runtime_coordination.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()

    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="no-pool-proj", path=proj_dir, project_id="no-pool")]
    result = materialize_views(view_db, projects)

    # Should complete without error; pool_state_unified table exists but is empty
    pools = list_all_pools(view_db)
    assert pools == []


# ---------------------------------------------------------------------------
# 9. Regression: LEFT JOIN NULL row must not count as active (BLOCKING fix)
# ---------------------------------------------------------------------------

def test_empty_pool_active_count_is_zero(tmp_path):
    """Pool with 0 members must report active_count=0, not 1 (LEFT JOIN null row bug)."""
    proj_dir = tmp_path / "proj-empty"
    _make_project_db(
        proj_dir / ".vnx-data" / "state", "proj-empty",
        min_workers=2, max_workers=4,
        active_members=0, reaped_members=0,
    )
    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="proj-empty", path=proj_dir, project_id="proj-empty")]
    materialize_views(view_db, projects)

    pools = list_all_pools(view_db)
    assert len(pools) == 1
    pool = pools[0]
    assert pool["active_count"] == 0, (
        f"Empty pool reported active_count={pool['active_count']}; "
        "LEFT JOIN null row is being counted as active"
    )
    assert pool["reaped_count"] == 0


def test_empty_pool_triggers_starvation(tmp_path):
    """Empty pool (active_count=0) with min_workers=2 must appear in starvation list."""
    proj_dir = tmp_path / "proj-starved"
    _make_project_db(
        proj_dir / ".vnx-data" / "state", "proj-starved",
        min_workers=2, max_workers=4,
        active_members=0, reaped_members=0,
    )
    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="proj-starved", path=proj_dir, project_id="proj-starved")]
    materialize_views(view_db, projects)

    pools = list_all_pools(view_db)
    starved = detect_starvation(pools)
    assert len(starved) == 1
    assert starved[0]["project_id"] == "proj-starved"


# ---------------------------------------------------------------------------
# 10. Regression: supervisor ledger must use VNX_DATA_DIR, not ~/.vnx-aggregator
# ---------------------------------------------------------------------------

def test_supervisor_ledger_respects_vnx_data_dir(tmp_path, monkeypatch):
    """run_supervision_tick must write pool_decisions.ndjson under VNX_DATA_DIR."""
    data_dir = tmp_path / "custom-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

    # Import after monkeypatching so env is visible at call time
    from scripts.control_centre.pool_supervisor import run_supervision_tick

    # Create a project with an active pool so a starvation event fires
    proj_dir = tmp_path / "proj-ledger"
    _make_project_db(
        proj_dir / ".vnx-data" / "state", "proj-ledger",
        min_workers=2, max_workers=4,
        active_members=0,
    )
    view_db = tmp_path / "data.db"
    projects = [ProjectEntry(name="proj-ledger", path=proj_dir, project_id="proj-ledger")]
    materialize_views(view_db, projects)

    run_supervision_tick(view_db)

    expected_ledger = data_dir / "events" / "pool_decisions.ndjson"
    assert expected_ledger.exists(), (
        f"Ledger not written to VNX_DATA_DIR={data_dir}; ADR-005 violation"
    )
    lines = [l for l in expected_ledger.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1
    ev = json.loads(lines[0])
    assert ev["event_type"] == "pool.supervisor.starvation"


def test_supervisor_ledger_default_path_no_home_dir(tmp_path, monkeypatch):
    """Without VNX_DATA_DIR, ledger must default to .vnx-data/events/ (not ~/.vnx-aggregator/)."""
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    from scripts.control_centre.pool_supervisor import _default_events_path

    path = _default_events_path()
    # Must NOT reference ~/.vnx-aggregator
    assert ".vnx-aggregator" not in str(path), (
        f"Default ledger path uses deprecated ~/.vnx-aggregator: {path}"
    )
    assert ".vnx-data" in str(path)
