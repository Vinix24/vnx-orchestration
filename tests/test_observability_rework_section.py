#!/usr/bin/env python3
"""Tests for the observability API's _rework section (slice 1b).

Covers the inline SQL (per-role first-pass success view, rework-by-origin-role self-join, recent edges)
and the fail-open contract. Isolates from the canonical store by monkeypatching _qi_db at a temp DB.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

api_observability = pytest.importorskip("api_observability")

_QI_SCHEMA = """
CREATE TABLE dispatch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    terminal TEXT,
    track TEXT,
    role TEXT,
    parent_dispatch TEXT,
    pattern_count INTEGER DEFAULT 0,
    prevention_rule_count INTEGER DEFAULT 0,
    instruction_char_count INTEGER DEFAULT 0,
    dispatched_at DATETIME,
    outcome_status TEXT,
    UNIQUE (project_id, dispatch_id)
);
CREATE VIEW dispatch_success_by_role AS
SELECT role,
       COUNT(*) AS total_dispatches,
       SUM(CASE WHEN outcome_status='success' THEN 1 ELSE 0 END) AS successes,
       ROUND(AVG(CASE WHEN outcome_status='success' THEN 1.0 ELSE 0.0 END), 3) AS success_rate
FROM dispatch_metadata WHERE outcome_status IS NOT NULL
GROUP BY role ORDER BY total_dispatches DESC;
"""


def _qi(tmp_path) -> Path:
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(db)
    conn.executescript(_QI_SCHEMA)
    conn.executemany(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, project_id, terminal, track, role, parent_dispatch, dispatched_at, outcome_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # governed feature dispatches (track A / terminal T1) — counted
            ("D-origin", "vnx-dev", "T1", "A", "backend-developer", None, "2026-06-28T07:00:00Z", "success"),
            ("D-rework", "vnx-dev", "T2", "B", "debugger", "D-origin", "2026-06-28T08:00:00Z", "success"),
            # benchmark run (headless track) — must be EXCLUDED from by_role, counted in benchmark_excluded
            ("D-bench", "vnx-dev", "headless", "headless", "security-engineer", None, "2026-06-28T06:00:00Z", "failure"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_rework_section_surfaces_roles_origin_and_edges(tmp_path, monkeypatch):
    db = _qi(tmp_path)
    monkeypatch.setattr(api_observability, "_qi_db", lambda: db)
    out = api_observability._rework(10, "vnx-dev")

    assert not out.get("degraded")
    roles = {r["role"]: r for r in out["by_role"]}
    assert roles["backend-developer"]["success_rate"] == 1.0
    # benchmark (headless) role is excluded from governed by_role but counted separately
    assert "security-engineer" not in roles
    assert out["benchmark_excluded"] == 1
    assert {"origin_role": "backend-developer", "reworked": 1} in out["by_origin_role"]
    assert len(out["recent"]) == 1
    edge = out["recent"][0]
    assert edge["rework_dispatch"] == "D-rework"
    assert edge["origin_dispatch"] == "D-origin"
    assert edge["rework_role"] == "debugger"
    assert edge["origin_role"] == "backend-developer"


def test_rework_section_fail_open_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(api_observability, "_qi_db", lambda: tmp_path / "nope.db")
    out = api_observability._rework(10, "vnx-dev")
    assert out["degraded"] is True
    assert out["by_role"] == [] and out["by_origin_role"] == [] and out["recent"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
