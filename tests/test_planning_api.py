#!/usr/bin/env python3
"""Tests for dashboard/api_planning.py.

Covers:
  - /api/operator/planning returns grouped horizon structure
  - Deliverables joined from dispatches (v26 fallback path)
  - Open items from track_open_items bridge + open_items.json
  - Graceful degradation when DB is absent
  - Smoke: index.html references /api/operator/planning
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Make dashboard importable
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

import api_planning as ap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_V26 = """
CREATE TABLE runtime_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT NOT NULL
);

CREATE TABLE tracks (
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    title       TEXT    NOT NULL,
    goal_state  TEXT,
    phase       TEXT    NOT NULL DEFAULT 'queued',
    next_up     INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    priority    TEXT    DEFAULT 'medium',
    requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
    instruction_template TEXT,
    context_composer_rules TEXT DEFAULT '{}',
    pr_ref      TEXT,
    trigger_condition TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    phase_changed_at TEXT,
    completed_at TEXT,
    metadata_json TEXT DEFAULT '{}',
    PRIMARY KEY (track_id, project_id)
);

CREATE TABLE track_dependencies (
    from_track_id   TEXT    NOT NULL,
    from_project_id TEXT    NOT NULL DEFAULT 'vnx-dev',
    to_track_id     TEXT    NOT NULL,
    to_project_id   TEXT    NOT NULL DEFAULT 'vnx-dev',
    kind            TEXT    NOT NULL,
    derivation_source TEXT  NOT NULL,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    evidence_json   TEXT    DEFAULT '{}',
    derived_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id)
);

CREATE TABLE track_open_items (
    track_id    TEXT    NOT NULL,
    project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
    oi_id       TEXT    NOT NULL,
    link_type   TEXT    NOT NULL,
    link_source TEXT    NOT NULL,
    linked_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (track_id, project_id, oi_id, link_type)
);

CREATE TABLE dispatches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id     TEXT    NOT NULL,
    project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
    state           TEXT    NOT NULL DEFAULT 'queued',
    terminal_id     TEXT,
    track           TEXT,
    priority        TEXT    DEFAULT 'P2',
    pr_ref          TEXT,
    output_ref      TEXT,
    output_kind     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json   TEXT    DEFAULT '{}',
    UNIQUE(dispatch_id, project_id)
);

PRAGMA user_version = 26;
"""

_SCHEMA_V27_EXTRAS = """
ALTER TABLE tracks ADD COLUMN horizon TEXT
    CHECK (horizon IS NULL OR horizon IN ('now', 'next', 'later'));

CREATE VIEW IF NOT EXISTS deliverables AS
SELECT
    project_id                                                  AS project_id,
    output_ref                                                  AS deliverable_ref,
    MIN(output_kind)                                            AS output_kind,
    MIN(track)                                                  AS track,
    COUNT(*)                                                    AS dispatch_count,
    SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END)        AS completed_count,
    SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
             THEN 1 ELSE 0 END)                                 AS in_flight_count,
    CASE
        WHEN SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'done'
        WHEN SUM(CASE WHEN state IN ('active', 'running', 'delivering', 'claimed', 'accepted')
                      THEN 1 ELSE 0 END) > 0
            THEN 'in_progress'
        ELSE 'proposed'
    END                                                         AS derived_status,
    MAX(updated_at)                                             AS last_activity
FROM dispatches
WHERE output_ref IS NOT NULL
GROUP BY project_id, output_ref;

PRAGMA user_version = 27;
"""


def _seed_db(db_path: Path, *, with_v27: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_V26)
    if with_v27:
        conn.executescript(_SCHEMA_V27_EXTRAS)

    project_id = "test-proj"

    # Two tracks
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, phase, sort_order) VALUES (?,?,?,?,?)",
        ("T-NOW", project_id, "Ship feature X", "active", 0),
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title, phase, sort_order) VALUES (?,?,?,?,?)",
        ("T-NEXT", project_id, "Plan feature Y", "queued", 1),
    )

    # Set horizons if schema supports it
    if with_v27:
        conn.execute(
            "UPDATE tracks SET horizon = ? WHERE track_id = ? AND project_id = ?",
            ("now", "T-NOW", project_id),
        )
        conn.execute(
            "UPDATE tracks SET horizon = ? WHERE track_id = ? AND project_id = ?",
            ("next", "T-NEXT", project_id),
        )

    # Dependency: T-NEXT depends on T-NOW
    conn.execute(
        """
        INSERT INTO track_dependencies
            (from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source)
        VALUES (?,?,?,?,?,?)
        """,
        ("T-NEXT", project_id, "T-NOW", project_id, "hard", "manual"),
    )

    # Dispatch linked to T-NOW with output_ref
    conn.execute(
        """
        INSERT INTO dispatches (dispatch_id, project_id, track, output_ref, output_kind, state)
        VALUES (?,?,?,?,?,?)
        """,
        ("DISP-001", project_id, "T-NOW", "PR-100", "pr", "completed"),
    )
    conn.execute(
        """
        INSERT INTO dispatches (dispatch_id, project_id, track, output_ref, output_kind, state)
        VALUES (?,?,?,?,?,?)
        """,
        ("DISP-002", project_id, "T-NOW", "PR-100", "pr", "active"),
    )

    # OI link for T-NOW
    conn.execute(
        """
        INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source)
        VALUES (?,?,?,?,?)
        """,
        ("T-NOW", project_id, "OI-999", "blocks", "manual"),
    )

    conn.commit()
    conn.close()


def _write_open_items_json(state_dir: Path) -> None:
    payload = {
        "items": [
            {"id": "OI-999", "title": "Test blocker", "severity": "blocker", "status": "open"}
        ]
    }
    (state_dir / "open_items.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: planning API with v26 schema (no horizon, no deliverables view)
# ---------------------------------------------------------------------------

class TestPlanningApiV26:

    def test_returns_horizon_groups(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db")

        result = ap._operator_get_planning(state_dir=state_dir)

        assert "horizons" in result
        assert set(result["horizons"].keys()) == {"now", "next", "later"}

    def test_total_tracks_correct(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db")

        result = ap._operator_get_planning(state_dir=state_dir)
        assert result["total_tracks"] == 2

    def test_tracks_in_later_when_no_horizon_column(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db", with_v27=False)

        result = ap._operator_get_planning(state_dir=state_dir)
        # With v26 schema there's no horizon column; all tracks default to "later"
        later = result["horizons"]["later"]
        assert len(later) == 2

    def test_dispatch_count_on_track(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db")

        result = ap._operator_get_planning(state_dir=state_dir)
        all_tracks = (
            result["horizons"]["now"]
            + result["horizons"]["next"]
            + result["horizons"]["later"]
        )
        t_now = next((t for t in all_tracks if t["track_id"] == "T-NOW"), None)
        assert t_now is not None
        assert t_now["dispatch_count"] == 2

    def test_deliverables_fallback_from_dispatches(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db", with_v27=False)

        result = ap._operator_get_planning(state_dir=state_dir)
        all_tracks = (
            result["horizons"]["now"]
            + result["horizons"]["next"]
            + result["horizons"]["later"]
        )
        t_now = next((t for t in all_tracks if t["track_id"] == "T-NOW"), None)
        assert t_now is not None
        assert len(t_now["deliverables"]) == 1
        d = t_now["deliverables"][0]
        assert d["deliverable_ref"] == "PR-100"
        assert d["output_kind"] == "pr"

    def test_open_items_joined_from_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db")
        _write_open_items_json(state_dir)

        result = ap._operator_get_planning(state_dir=state_dir)
        all_tracks = (
            result["horizons"]["now"]
            + result["horizons"]["next"]
            + result["horizons"]["later"]
        )
        t_now = next((t for t in all_tracks if t["track_id"] == "T-NOW"), None)
        assert t_now is not None
        assert len(t_now["open_items"]) == 1
        oi = t_now["open_items"][0]
        assert oi["oi_id"] == "OI-999"
        assert oi["link_type"] == "blocks"
        assert oi["title"] == "Test blocker"

    def test_depends_on_edges_present(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db")

        result = ap._operator_get_planning(state_dir=state_dir)
        all_tracks = (
            result["horizons"]["now"]
            + result["horizons"]["next"]
            + result["horizons"]["later"]
        )
        t_next = next((t for t in all_tracks if t["track_id"] == "T-NEXT"), None)
        assert t_next is not None
        assert len(t_next["depends_on"]) == 1
        dep = t_next["depends_on"][0]
        assert dep["to_track_id"] == "T-NOW"
        assert dep["kind"] == "hard"

    def test_missing_db_returns_degraded(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No DB created

        result = ap._operator_get_planning(state_dir=state_dir)
        assert result["degraded"] is True
        assert result["total_tracks"] == 0
        assert result["horizons"] == {"now": [], "next": [], "later": []}


# ---------------------------------------------------------------------------
# Tests: planning API with v27 schema (horizon + deliverables view)
# ---------------------------------------------------------------------------

class TestPlanningApiV27:

    def test_tracks_grouped_by_horizon(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db", with_v27=True)

        result = ap._operator_get_planning(state_dir=state_dir)
        assert len(result["horizons"]["now"]) == 1
        assert len(result["horizons"]["next"]) == 1
        assert len(result["horizons"]["later"]) == 0
        assert result["schema_v27"] is True

    def test_deliverables_from_view(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db", with_v27=True)

        result = ap._operator_get_planning(state_dir=state_dir)
        t_now = result["horizons"]["now"][0]
        assert len(t_now["deliverables"]) == 1
        d = t_now["deliverables"][0]
        assert d["deliverable_ref"] == "PR-100"
        assert d["dispatch_count"] == 2

    def test_track_has_required_fields(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _seed_db(state_dir / "runtime_coordination.db", with_v27=True)

        result = ap._operator_get_planning(state_dir=state_dir)
        t = result["horizons"]["now"][0]
        for field in ("track_id", "title", "phase", "horizon", "dispatch_count",
                      "depends_on", "deliverables", "open_items"):
            assert field in t, f"missing field: {field}"


# ---------------------------------------------------------------------------
# Smoke: index.html references the planning endpoint and renders columns
# ---------------------------------------------------------------------------

class TestPlanningSmoke:

    def test_index_html_references_planning_endpoint(self):
        html_path = Path(__file__).parent.parent / "dashboard" / "index.html"
        assert html_path.exists(), "dashboard/index.html not found"
        content = html_path.read_text(encoding="utf-8")
        assert "/api/operator/planning" in content, "planning endpoint not referenced in index.html"

    def test_index_html_has_planning_columns(self):
        html_path = Path(__file__).parent.parent / "dashboard" / "index.html"
        content = html_path.read_text(encoding="utf-8")
        for col_id in ("planning-cards-now", "planning-cards-next", "planning-cards-later"):
            assert col_id in content, f"column id '{col_id}' not found in index.html"

    def test_index_html_has_planning_section(self):
        html_path = Path(__file__).parent.parent / "dashboard" / "index.html"
        content = html_path.read_text(encoding="utf-8")
        assert "planning-kanban" in content, "planning-kanban section not found"
