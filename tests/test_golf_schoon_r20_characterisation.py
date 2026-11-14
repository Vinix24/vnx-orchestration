#!/usr/bin/env python3
"""Characterisation tests for golf-schoon R20 (Dispatch-ID: dlv-27167ff24cf7).

Pins the observable behaviour of ``get_behavioral_summary`` so the
extract-method refactor stays behaviour-preserving. Real SQLite files in
tmp dirs; nothing is patched except env vars.
"""

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import intelligence_dashboard_data as idd  # noqa: E402

# ---------------------------------------------------------------------------
# get_behavioral_summary
# ---------------------------------------------------------------------------

_EMPTY = {
    "rework_files": [],
    "common_errors": [],
    "file_affinities": [],
    "duration_baselines": [],
    "exploration_insight": "",
    "total_dispatches_analyzed": 0,
    "patterns_generated": 0,
}

_DASH_SCHEMA = """
CREATE TABLE success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT, title TEXT, pattern_data TEXT,
    confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT
);
"""
_PREVENTION = """
CREATE TABLE prevention_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT, triggered_count INTEGER, source TEXT
);
"""


def _dash_db(tmp_path, monkeypatch, *, prevention=_PREVENTION, with_sp=True):
    db = tmp_path / "quality_intelligence.db"
    con = sqlite3.connect(db)
    if with_sp:
        con.executescript(_DASH_SCHEMA)
    if prevention:
        con.executescript(prevention)
    con.commit()
    con.close()
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    return db


def _sp(db, title, data, conf=0.5, usage=0, src=None, category="behavior_analysis"):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO success_patterns (category,title,pattern_data,confidence_score,"
        "usage_count,source_dispatch_ids) VALUES (?,?,?,?,?,?)",
        (category, title, data if isinstance(data, str) else json.dumps(data), conf, usage, src),
    )
    con.commit()
    con.close()


def test_summary_no_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(idd, "_db_path", lambda: None)
    assert idd.get_behavioral_summary() == _EMPTY


def test_summary_unopenable_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(idd, "_db_path", lambda: tmp_path / "missing" / "x.db")
    assert idd.get_behavioral_summary() == _EMPTY


def test_summary_missing_tables_returns_empty(tmp_path, monkeypatch):
    _dash_db(tmp_path, monkeypatch, prevention=None, with_sp=False)
    assert idd.get_behavioral_summary() == _EMPTY


def test_summary_full_aggregation(tmp_path, monkeypatch):
    db = _dash_db(tmp_path, monkeypatch)
    _sp(db, "Files A,B co-occur", {"files": ["A", "B"], "co_occurrence": 0.9, "count": 4},
        conf=0.9, usage=5, src=json.dumps(["d1", "d2", "d3"]))
    _sp(db, "Files B,C co-occur", {"files": ["B", "C"], "co_occurrence": 0.5}, conf=0.7, usage=9)
    _sp(db, "Files bad co-occur", "not json{", conf=0.1, usage=1)
    _sp(db, "Files null co-occur", "", conf=0.2, usage=2)
    _sp(db, "Expected duration: T1", {"role": "backend", "avg_seconds": 312, "count": 7}, usage=3)
    _sp(db, "Expected duration: T2", {"role": "test", "avg_seconds": 60, "count": 2}, usage=1)
    _sp(db, "Files other co-occur", {"files": ["Z"]}, category="other", usage=99)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO prevention_rules (description,triggered_count,source) VALUES (?,?,?)",
                ("x" * 300, 3, "behavior_analysis"))
    con.execute("INSERT INTO prevention_rules (description,triggered_count,source) VALUES (?,?,?)",
                (None, None, "behavior_analysis"))
    con.execute("INSERT INTO prevention_rules (description,triggered_count,source) VALUES (?,?,?)",
                ("skipped", 50, "other"))
    con.commit()
    con.close()

    out = idd.get_behavioral_summary()

    assert out["file_affinities"][:2] == [
        {"files": ["A", "B"], "co_occurrence": 0.9, "count": 4},
        {"files": ["B", "C"], "co_occurrence": 0.5, "count": 0},
    ]
    assert [a["files"] for a in out["file_affinities"]] == [["A", "B"], ["B", "C"], []]
    assert out["duration_baselines"] == [
        {"role": "backend", "avg_seconds": 312, "count": 7},
        {"role": "test", "avg_seconds": 60, "count": 2},
    ]
    assert out["common_errors"] == [
        {"error": "x" * 200, "count": 3},
        {"error": "", "count": 0},
    ]
    # B gets 4 + 1 (default count); unparsable rows contribute nothing
    assert out["rework_files"] == [
        {"file": "B", "rework_count": 5},
        {"file": "A", "rework_count": 4},
        {"file": "C", "rework_count": 1},
    ]
    assert out["patterns_generated"] == 6
    assert out["total_dispatches_analyzed"] == 3
    assert out["exploration_insight"] == "backend tasks average 5.2 min based on 7 dispatches"


def test_summary_rework_order_and_cap(tmp_path, monkeypatch):
    db = _dash_db(tmp_path, monkeypatch)
    for i in range(12):
        _sp(db, f"Files f{i} co-occur", {"files": [f"f{i}"], "count": i + 1}, usage=i)
    out = idd.get_behavioral_summary()
    assert len(out["rework_files"]) == 10
    assert out["rework_files"][0] == {"file": "f11", "rework_count": 12}
    assert out["rework_files"][-1] == {"file": "f2", "rework_count": 3}
    assert len(out["file_affinities"]) == 12


def test_summary_without_source_column_has_no_common_errors(tmp_path, monkeypatch):
    db = _dash_db(tmp_path, monkeypatch, prevention=(
        "CREATE TABLE prevention_rules (id INTEGER PRIMARY KEY, description TEXT, triggered_count INTEGER);"))
    _sp(db, "Expected duration: T1", {"role": "r", "avg_seconds": 0, "count": 1}, usage=1)
    out = idd.get_behavioral_summary()
    assert out["common_errors"] == []
    # avg_seconds 0 -> avg_min 0 -> no insight
    assert out["exploration_insight"] == ""
    assert out["duration_baselines"] == [{"role": "r", "avg_seconds": 0, "count": 1}]


def test_summary_dispatches_analyzed_counts_ids(tmp_path, monkeypatch):
    db = _dash_db(tmp_path, monkeypatch)
    _sp(db, "Anything", {}, src=json.dumps(["a", "b", "c", "d"]))
    assert idd.get_behavioral_summary()["total_dispatches_analyzed"] == 4


def test_summary_dispatches_analyzed_malformed_ids_stay_zero(tmp_path, monkeypatch):
    db = _dash_db(tmp_path, monkeypatch)
    _sp(db, "Anything", {}, src="not-json")
    out = idd.get_behavioral_summary()
    assert out["total_dispatches_analyzed"] == 0
    assert out["patterns_generated"] == 1
