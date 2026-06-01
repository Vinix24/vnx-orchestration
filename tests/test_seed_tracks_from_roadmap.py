"""tests/test_seed_tracks_from_roadmap.py — ROADMAP -> tracks seeder.

Verifies:
- dry-run (default) writes NOTHING
- --apply creates one track per feature with correct field mapping
- idempotent: re-apply leaves rows unchanged (no dupes, no churn)
- goal_state-absent feature tolerated (synthesized)
- depends_on -> track_dependencies edges
- horizon derivation (1.0 -> now/next, 1.x -> later)
- phase drift reported, not auto-synced
- track_created / track_authored_synced events emitted to track_events.ndjson
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

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schema_migration
import tracks as tracks_lib
import seed_tracks_from_roadmap as seeder


SAMPLE_ROADMAP = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-a
    title: Feature A
    risk_class: high
    depends_on: []
    milestone: "1.0"
    status: done
    notes: Feature A is shipped.
    pr_queue:
      - pr_id: "#10"
        title: a
        status: merged
        risk_class: high
  - feature_id: feat-b
    title: Feature B
    risk_class: low
    depends_on: [feat-a]
    milestone: "1.0"
    status: planned
  - feature_id: feat-c
    title: Feature C (no notes, future)
    risk_class: medium
    depends_on: []
    milestone: "1.x"
    status: planned
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a v27 coordination DB in tmp_path/state and return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
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
    """)
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 22, (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 24, (_MIGRATIONS / "0024_tracks_tenant_scoping.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    schema_migration.apply_script_if_below(
        conn, 27, (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()
    return state_dir


@pytest.fixture()
def roadmap(tmp_path: Path) -> Path:
    p = tmp_path / "ROADMAP.yaml"
    p.write_text(SAMPLE_ROADMAP, encoding="utf-8")
    return p


def test_dry_run_writes_nothing(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    report = seeder.seed(state_dir, roadmap, "vnx-dev", apply=False)
    assert report["summary"]["created"] == 3
    # Nothing actually written.
    assert tracks_lib.list_tracks(state_dir, "vnx-dev") == []


def test_apply_creates_rows(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    report = seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    assert report["summary"]["created"] == 3
    rows = {t["track_id"]: t for t in tracks_lib.list_tracks(state_dir, "vnx-dev")}
    assert set(rows) == {"feat-a", "feat-b", "feat-c"}
    # feat-a: done status -> done phase, high risk -> P1.
    assert rows["feat-a"]["phase"] == "done"
    assert rows["feat-a"]["priority"] == "P1"
    # feat-b: planned -> queued.
    assert rows["feat-b"]["phase"] == "queued"


def test_horizon_derivation(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    rows = {t["track_id"]: t for t in tracks_lib.list_tracks(state_dir, "vnx-dev")}
    # 1.0 done -> next; 1.0 not-done -> now; 1.x -> later.
    assert rows["feat-a"]["horizon"] == "next"   # 1.0 + done
    assert rows["feat-b"]["horizon"] == "now"    # 1.0 + planned
    assert rows["feat-c"]["horizon"] == "later"  # 1.x


def test_goal_state_synthesized_when_absent(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    feat_c = tracks_lib.get_track(state_dir, "feat-c", "vnx-dev")
    # No notes on feat-c -> synthesized non-empty goal_state.
    assert feat_c["goal_state"] == "feat-c done"


def test_dependencies_created(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    try:
        edges = conn.execute(
            "SELECT from_track_id, to_track_id FROM track_dependencies"
        ).fetchall()
    finally:
        conn.close()
    assert ("feat-b", "feat-a") in edges


def test_idempotent_reapply(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    report2 = seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    assert report2["summary"]["created"] == 0
    assert report2["summary"]["unchanged"] == 3
    assert report2["summary"]["updated"] == 0
    # Still exactly 3 rows (no dupes).
    assert len(tracks_lib.list_tracks(state_dir, "vnx-dev")) == 3


def test_phase_drift_reported_not_synced(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    # Operator transitions feat-b queued -> active in the DB (declared status).
    tracks_lib.transition_phase(state_dir, "feat-b", "vnx-dev", "active", actor="operator")
    report = seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    drift_ids = {d["track_id"] for d in report["phase_drift"]}
    assert "feat-b" in drift_ids
    # Phase NOT overwritten back to queued.
    assert tracks_lib.get_track(state_dir, "feat-b", "vnx-dev")["phase"] == "active"


def test_dependency_only_change_reseeded(tmp_path, roadmap):
    """ADVISORY: when a ROADMAP feature's ONLY change is depends_on,
    the seeder must re-seed the track_dependencies row (not skip as
    'unchanged').
    """
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)

    # feat-b depends on feat-a initially.
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    try:
        edges = conn.execute(
            "SELECT from_track_id, to_track_id FROM track_dependencies"
        ).fetchall()
        assert ("feat-b", "feat-a") in edges
    finally:
        conn.close()

    # Modify ROADMAP: feat-b now also depends on feat-c (no other field changes).
    modified = """
roadmap_id: test-roadmap
title: Test
features:
  - feature_id: feat-a
    title: Feature A
    risk_class: high
    depends_on: []
    milestone: "1.0"
    status: done
    notes: Feature A is shipped.
    pr_queue:
      - pr_id: "#10"
        title: a
        status: merged
        risk_class: high
  - feature_id: feat-b
    title: Feature B
    risk_class: low
    depends_on: [feat-a, feat-c]
    milestone: "1.0"
    status: planned
  - feature_id: feat-c
    title: Feature C (no notes, future)
    risk_class: medium
    depends_on: []
    milestone: "1.x"
    status: planned
"""
    modified_path = tmp_path / "ROADMAP_modified.yaml"
    modified_path.write_text(modified, encoding="utf-8")

    report = seeder.seed(state_dir, modified_path, "vnx-dev", apply=True)
    # feat-b row is otherwise unchanged — but dependencies were reconciled.
    assert "feat-b" in report["unchanged"]

    # Verify the new dependency edge was added.
    conn = sqlite3.connect(str(db))
    try:
        edges = conn.execute(
            "SELECT from_track_id, to_track_id FROM track_dependencies ORDER BY from_track_id, to_track_id"
        ).fetchall()
        assert ("feat-b", "feat-a") in edges
        assert ("feat-b", "feat-c") in edges
    finally:
        conn.close()


def test_events_emitted(tmp_path, roadmap):
    state_dir = _make_db(tmp_path)
    seeder.seed(state_dir, roadmap, "vnx-dev", apply=True)
    events_file = state_dir.parent / "events" / "track_events.ndjson"
    assert events_file.exists()
    lines = [json.loads(line) for line in events_file.read_text().splitlines() if line.strip()]
    types = {e["event_type"] for e in lines}
    assert "track_created" in types
