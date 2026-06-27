#!/usr/bin/env python3
"""Tests for the kanban future-ready (queued) lane.

Dispatch-ID: 20260627-kanban-future-ready

The kanban now surfaces a "queued" stage: planned coordination-DB rows (state proposed/ready/
queued) that are not yet a file in the dispatch dirs — the work "staged to run in the future"
(promoted-but-undispatched deliverables). 'ready' = promoted (human-gated). Fail-open: a missing
DB / column / any error yields an empty queued lane and never breaks the kanban.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import api_operator as op  # noqa: E402

_SCHEMA = """
CREATE TABLE dispatches (
    dispatch_id TEXT, state TEXT, track TEXT, terminal_id TEXT, priority TEXT,
    gate TEXT, output_kind TEXT, operator_approved_at TEXT, created_at TEXT
);
"""


def _make_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO dispatches(dispatch_id,state,track,terminal_id,priority,gate,"
        "output_kind,operator_approved_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "CANONICAL_STATE_DIR", tmp_path)
    return tmp_path


def test_ready_dispatch_appears_in_queued(state_dir):
    _make_db(state_dir / "runtime_coordination.db", [
        ("20260627-ready-pr", "ready", "A", "T1", "P1", "gate_x", "pr", "2026-06-27T10:00:00Z", "2026-06-27T09:00:00Z"),
        ("20260627-proposed-doc", "proposed", "B", "T2", "P2", "—", "doc", None, "2026-06-27T08:00:00Z"),
    ])
    entries = op._scan_queued_dispatches(set())
    ids = {e["id"] for e in entries}
    assert ids == {"20260627-ready-pr", "20260627-proposed-doc"}
    ready = next(e for e in entries if e["id"] == "20260627-ready-pr")
    assert ready["stage"] == "queued"
    assert ready["state"] == "ready"
    assert ready["promoted"] is True          # operator_approved_at set
    assert ready["output_kind"] == "pr"
    proposed = next(e for e in entries if e["id"] == "20260627-proposed-doc")
    assert proposed["promoted"] is False       # not yet promoted


def test_non_planned_states_excluded(state_dir):
    _make_db(state_dir / "runtime_coordination.db", [
        ("active-1", "active", "A", "T1", "P1", "—", "pr", None, "2026-06-27T09:00:00Z"),
        ("done-1", "completed", "A", "T1", "P1", "—", "pr", None, "2026-06-27T09:00:00Z"),
    ])
    assert op._scan_queued_dispatches(set()) == []


def test_exclude_ids_dedup(state_dir):
    # A planned row whose id is already a file-stage entry must NOT appear in queued.
    _make_db(state_dir / "runtime_coordination.db", [
        ("dup-1", "ready", "A", "T1", "P1", "—", "pr", None, "2026-06-27T09:00:00Z"),
    ])
    assert op._scan_queued_dispatches({"dup-1"}) == []


def test_missing_db_fails_open(state_dir):
    # No runtime_coordination.db at all → empty, never raises.
    assert op._scan_queued_dispatches(set()) == []


def test_missing_state_column_fails_open(state_dir):
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE dispatches (dispatch_id TEXT)")  # no state column
    conn.commit()
    conn.close()
    assert op._scan_queued_dispatches(set()) == []


def test_scan_dispatches_includes_queued_stage(state_dir, monkeypatch, tmp_path):
    _make_db(state_dir / "runtime_coordination.db", [
        ("20260627-future", "ready", "A", "T1", "P1", "—", "pr", "2026-06-27T10:00:00Z", "2026-06-27T09:00:00Z"),
    ])
    monkeypatch.setattr(op, "DISPATCHES_DIR", tmp_path / "no-dispatches")  # empty file-stages
    result = op._scan_dispatches()
    assert "queued" in result["stages"]
    queued_ids = {c["id"] for c in result["stages"]["queued"]}
    assert "20260627-future" in queued_ids
    assert result["total"] >= 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
