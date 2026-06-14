"""tests/test_autopilot_tick_bridge.py — R5 integration (PR-D, D2/D4).

Proves the OI→track bridge + reconcile run AUTOMATICALLY inside
``RoadmapManager.autopilot_tick()`` — driven DIRECTLY, no CLI:

  * a seeded blocking open-item is synced into ``track_open_items`` and the
    reconciler flips ``tracks.derived_status`` to ``blocked`` in the SAME tick
    (R5.1 — bridge BEFORE reconcile so derived_status sees the fresh links);
  * a forced reconcile failure SURFACES in the tick result (``track_sync``)
    without crashing the tick (R5.2 — surface, never swallow, never sys.exit).

Temp-DB only — the package conftest pins ``VNX_DATA_DIR_EXPLICIT=1`` + a tmp
``VNX_DATA_DIR`` and the env fixture re-pins every VNX path under ``tmp_path``,
so nothing touches the live ``~/.vnx-data`` store.

ADR-007: every track / ``track_open_items`` access is (track_id, project_id)-
scoped, and both wired primitives take ``(self.state_dir, self.project_id)`` —
tenant isolation across the operator's central DBs is preserved end to end.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import roadmap_manager as rm
import schema_migration
import tracks as tracks_lib
import track_reconciler

PROJECT_ID = "vnx-dev"

_FEATURE_PLAN = """# Feature: Feature A

**Status**: Draft
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate

## Dependency Flow
```text
PR-0 (no dependencies)
```

## PR-0: Feature A PR
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Skill**: @architect
**Risk-Class**: low
**Merge-Policy**: conditional_auto
**Review-Stack**: gemini_review,codex_gate
**Dependencies**: []
"""

_ROADMAP_YAML = """features:
  - feature_id: feature-a
    title: Feature A
    plan_path: roadmap/features/feature-a/FEATURE_PLAN.md
    branch_name: feature/a
    risk_class: low
    merge_policy: conditional_auto
    review_stack: [gemini_review, codex_gate]
    depends_on: []
    status: planned
"""


# ---------------------------------------------------------------------------
# Temp-DB + env fixtures
# ---------------------------------------------------------------------------

def _build_tracks_db(state_dir: Path) -> None:
    """Apply migrations 0022–0030 to <state_dir>/runtime_coordination.db (v30).

    Mirrors tests/test_oi_track_bridge.py: the base ``dispatches`` +
    ``coordination_events`` tables first, then the track-layer migration chain
    up to 0030 (resolution schema) so the bridge's ``_require_resolution_schema``
    precondition is satisfied.
    """
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            project_id TEXT
        )
        """
    )
    conn.commit()
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp project with autopilot enabled, an active feature, and a v30 DB."""
    project_root = tmp_path / "project"
    (project_root / ".claude" / "vnx-system").mkdir(parents=True, exist_ok=True)
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    state_dir.mkdir(parents=True, exist_ok=True)
    dispatch_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(project_root / ".claude" / "vnx-system"))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_ID)
    monkeypatch.setenv("VNX_ROADMAP_AUTOPILOT", "1")
    monkeypatch.setenv("VNX_QUEUE_POPUP_ENABLED", "0")
    monkeypatch.setattr(rm, "emit_governance_receipt", lambda *a, **k: None)

    plan_dir = project_root / "roadmap" / "features" / "feature-a"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "FEATURE_PLAN.md").write_text(_FEATURE_PLAN, encoding="utf-8")
    roadmap_file = project_root / "ROADMAP.yaml"
    roadmap_file.write_text(_ROADMAP_YAML, encoding="utf-8")

    _build_tracks_db(state_dir)
    return {"project_root": project_root, "state_dir": state_dir, "roadmap_file": roadmap_file}


def _seed_blocking_oi(state_dir: Path) -> None:
    """One active track (pr_ref #100) + an on-disk open-item that blocks it."""
    tracks_lib.create_track(
        state_dir, "feat-a", PROJECT_ID, "feat-a", "goal", phase="active", pr_ref="#100",
    )
    (state_dir / "open_items.json").write_text(
        json.dumps({"items": [
            {"id": "OI-1", "severity": "blocker", "status": "open",
             "title": "blocking item", "pr_id": "#100"},
        ]}),
        encoding="utf-8",
    )


def _rows(state_dir: Path, oi_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT track_id, link_type, resolved_at FROM track_open_items "
        "WHERE project_id = ? AND oi_id = ? ORDER BY track_id",
        (PROJECT_ID, oi_id),
    )]
    conn.close()
    return rows


def _derived_status(state_dir: Path, track_id: str):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# R5.1 — tick bridges then reconciles, in one tick, no CLI
# ---------------------------------------------------------------------------

def test_autopilot_tick_syncs_bridge_then_reconciles(env):
    """A single autopilot_tick() populates track_open_items AND flips
    tracks.derived_status to 'blocked' for the blocked track (R5.1, D2/D4)."""
    _seed_blocking_oi(env["state_dir"])
    manager = rm.RoadmapManager()
    manager.init_roadmap(env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    result = manager.autopilot_tick()

    # The tick carries the sync outcome (shared tick result — D2 one wiring site).
    sync = result["track_sync"]
    assert sync["status"] == "ok", sync
    assert sync["bridge"]["ok"] is True
    assert sync["bridge"]["linked"] == 1
    assert sync["reconcile"]["status"] == "ok"

    # track_open_items populated: the blocking link exists and is active.
    rows = _rows(env["state_dir"], "OI-1")
    assert len(rows) == 1
    assert rows[0]["track_id"] == "feat-a"
    assert rows[0]["link_type"] == "blocks"
    assert rows[0]["resolved_at"] is None

    # derived_status reflects the freshly-synced link: the blocker → blocked.
    assert _derived_status(env["state_dir"], "feat-a") == "blocked"


def test_autopilot_tick_bridge_is_idempotent_across_ticks(env):
    """The repeating tick is safe to re-run (D5): a second tick adds no duplicate
    link and leaves derived_status stable."""
    _seed_blocking_oi(env["state_dir"])
    manager = rm.RoadmapManager()
    manager.init_roadmap(env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    first = manager.autopilot_tick()
    second = manager.autopilot_tick()

    assert first["track_sync"]["bridge"]["linked"] == 1
    assert second["track_sync"]["status"] == "ok"
    assert second["track_sync"]["bridge"]["linked"] == 0  # no-op re-run
    assert len(_rows(env["state_dir"], "OI-1")) == 1       # no duplicate
    assert _derived_status(env["state_dir"], "feat-a") == "blocked"


# ---------------------------------------------------------------------------
# R5.2 — a forced reconcile failure surfaces in the tick, no crash
# ---------------------------------------------------------------------------

def test_autopilot_tick_surfaces_reconcile_failure_without_crashing(env, monkeypatch):
    """A reconcile that raises is captured into the tick result (status=error,
    stage=reconcile) — the tick does NOT crash and does NOT sys.exit (R5.2)."""
    _seed_blocking_oi(env["state_dir"])
    manager = rm.RoadmapManager()
    manager.init_roadmap(env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    def _boom(*a, **k):
        raise RuntimeError("forced reconcile failure")

    monkeypatch.setattr(track_reconciler, "reconcile_all_tracks", _boom)

    result = manager.autopilot_tick()  # must not raise

    sync = result["track_sync"]
    assert sync["status"] == "error"
    assert sync["stage"] == "reconcile"
    assert "forced reconcile failure" in sync["error"]
    # The bridge still ran (and committed) BEFORE reconcile failed.
    assert sync["bridge"]["ok"] is True
    assert _rows(env["state_dir"], "OI-1")[0]["resolved_at"] is None


def test_autopilot_tick_surfaces_bridge_failure_without_crashing(env, monkeypatch):
    """A bridge that raises is captured too (status=error, stage=bridge); reconcile
    is not reached and the tick survives (R5.2)."""
    _seed_blocking_oi(env["state_dir"])
    manager = rm.RoadmapManager()
    manager.init_roadmap(env["roadmap_file"])
    manager.load_feature("feature-a", no_worktree=True)

    import import_open_items_to_tracks as bridge_mod

    def _boom(*a, **k):
        raise RuntimeError("forced bridge failure")

    monkeypatch.setattr(bridge_mod, "import_open_items_to_tracks", _boom)

    result = manager.autopilot_tick()  # must not raise

    sync = result["track_sync"]
    assert sync["status"] == "error"
    assert sync["stage"] == "bridge"
    assert "forced bridge failure" in sync["error"]
    assert "reconcile" not in sync  # reconcile never ran
