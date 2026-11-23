#!/usr/bin/env python3
"""Characterisation tests for golf-schoon R20b (Dispatch-ID: dlv-ae7aaaff65a5).

Pins the observable behaviour of ``update_confidence_from_outcome`` (and, from R20c,
``shadow_grounding_compare``, Dispatch-ID: dlv-ae7aaaff65a5-c) so the extract-method refactor stays
behaviour-preserving. Real SQLite files in tmp dirs; nothing is patched except
env vars. Carried over from R20's tests (commit 56bc98966) and extended.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from confidence_reconcile import beta_score
from project_scope import current_project_id


def _load_persist():
    spec = importlib.util.spec_from_file_location(
        "intelligence_persist_r20b", REPO_ROOT / "scripts" / "lib" / "intelligence_persist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_persist = _load_persist()
update_confidence_from_outcome = _persist.update_confidence_from_outcome
shadow_grounding_compare = _persist.shadow_grounding_compare


# ---------------------------------------------------------------------------
# update_confidence_from_outcome
# ---------------------------------------------------------------------------

_PERSIST_SCHEMA = """
CREATE TABLE success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
    description TEXT NOT NULL, pattern_data TEXT NOT NULL,
    confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME{sp}
);
CREATE TABLE pattern_usage (
    pattern_id TEXT PRIMARY KEY, pattern_title TEXT NOT NULL, pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0, ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP, last_offered TIMESTAMP, confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP{pu}
);
{ce}
CREATE TABLE dispatch_pattern_offered (
    dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL, pattern_title TEXT NOT NULL,
    offered_at TEXT NOT NULL, PRIMARY KEY (dispatch_id, pattern_id)
);
"""
_CE = """CREATE TABLE confidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL, terminal TEXT,
    outcome TEXT NOT NULL, patterns_boosted INTEGER DEFAULT 0, patterns_decayed INTEGER DEFAULT 0,
    confidence_change REAL NOT NULL, occurred_at TEXT NOT NULL{extra}
);"""


def _pdb(tmp_path, *, project=False, grounding=True, ce=_CE):
    db = tmp_path / "persist.db"
    proj = ",\n project_id TEXT NOT NULL DEFAULT 'vnx-dev'" if project else ""
    extra = (",\n grounding_source TEXT" if grounding else "") + proj
    con = sqlite3.connect(db)
    con.executescript(_PERSIST_SCHEMA.format(
        sp=proj, pu=proj, ce=ce.format(extra=extra) if ce else ""))
    con.commit()
    con.close()
    return db


def _add_sp(db, src, conf=0.5, usage=1, title="P"):
    con = sqlite3.connect(db)
    cur = con.execute(
        "INSERT INTO success_patterns (pattern_type,category,title,description,pattern_data,"
        "confidence_score,usage_count,source_dispatch_ids) VALUES ('a','g',?,'d','{}',?,?,?)",
        (title, conf, usage, json.dumps(src)))
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def _q(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def test_update_missing_db_returns_zero(tmp_path):
    assert update_confidence_from_outcome(tmp_path / "nope.db", "d", "T1", "success") == {
        "boosted": 0, "decayed": 0}


def test_update_success_creates_usage_and_event(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    sp = _add_sp(db, ["disp-1"], conf=0.5, usage=3, title="Alpha")
    other = _add_sp(db, ["disp-2"], conf=0.4)
    res = update_confidence_from_outcome(db, "disp-1", "T2", "success")
    assert res == {"boosted": 1, "decayed": 0}
    pu = _q(db, "SELECT * FROM pattern_usage")
    assert len(pu) == 1
    assert pu[0]["pattern_id"] == f"intel_sp_{sp}"
    assert (pu[0]["pattern_title"], pu[0]["used_count"], pu[0]["success_count"],
            pu[0]["failure_count"], pu[0]["ignored_count"]) == ("Alpha", 1, 1, 0, 0)
    assert len(pu[0]["pattern_hash"]) == 40
    assert pu[0]["confidence"] == pytest.approx(beta_score(1, 0))
    row = _q(db, "SELECT * FROM success_patterns WHERE id=?", sp)[0]
    assert row["confidence_score"] == pytest.approx(round(beta_score(1, 0), 6))
    assert row["usage_count"] == 4 and row["last_used"]
    assert _q(db, "SELECT confidence_score FROM success_patterns WHERE id=?", other)[0][
        "confidence_score"] == 0.4
    ev = _q(db, "SELECT * FROM confidence_events")
    assert len(ev) == 1
    assert (ev[0]["dispatch_id"], ev[0]["terminal"], ev[0]["outcome"],
            ev[0]["patterns_boosted"], ev[0]["patterns_decayed"],
            ev[0]["grounding_source"]) == ("disp-1", "T2", "success", 1, 0, "source_dispatch_ids")
    assert ev[0]["confidence_change"] == pytest.approx(
        round(round(beta_score(1, 0), 6) - 0.5, 4))


def test_update_failure_decays_new_pattern(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    sp = _add_sp(db, ["disp-1"], conf=0.5, usage=3, title="T")
    assert update_confidence_from_outcome(db, "disp-1", "T1", "failure") == {
        "boosted": 0, "decayed": 1}
    pu = _q(db, "SELECT * FROM pattern_usage")[0]
    assert (pu["used_count"], pu["success_count"], pu["failure_count"]) == (0, 0, 1)
    row = _q(db, "SELECT * FROM success_patterns WHERE id=?", sp)[0]
    assert row["confidence_score"] == pytest.approx(round(beta_score(0, 1), 6))
    assert row["usage_count"] == 3 and row["last_used"] is None


def test_update_existing_usage_increments(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    sp = _add_sp(db, ["disp-1"])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO pattern_usage (pattern_id,pattern_title,pattern_hash,used_count,"
                "success_count,failure_count) VALUES (?, 't','h',4,3,2)", (f"intel_sp_{sp}",))
    con.commit()
    con.close()
    update_confidence_from_outcome(db, "disp-1", "T1", "success")
    update_confidence_from_outcome(db, "disp-1", "T1", "failure")
    pu = _q(db, "SELECT * FROM pattern_usage")[0]
    assert (pu["success_count"], pu["failure_count"], pu["used_count"]) == (4, 3, 5)
    assert pu["confidence"] == pytest.approx(beta_score(4, 3))
    assert len(_q(db, "SELECT * FROM confidence_events")) == 2


def test_update_junction_grounding_source(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_OUTCOME_GROUNDING_V2", "1")
    db = _pdb(tmp_path)
    sp = _add_sp(db, [])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO dispatch_pattern_offered VALUES ('disp-9', ?, 't', 'now')",
                (f"intel_sp_{sp}",))
    con.commit()
    con.close()
    assert update_confidence_from_outcome(db, "disp-9", "T3", "success")["boosted"] == 1
    assert _q(db, "SELECT grounding_source FROM confidence_events")[0][
        "grounding_source"] == "junction"


def test_update_no_matches_still_writes_event(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    assert update_confidence_from_outcome(db, "ghost", "T1", "success") == {
        "boosted": 0, "decayed": 0}
    ev = _q(db, "SELECT * FROM confidence_events")
    assert len(ev) == 1 and ev[0]["confidence_change"] == 0.0


def test_update_project_columns_are_stamped(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    from project_scope import current_project_id
    pid = current_project_id()
    db = _pdb(tmp_path, project=True)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO success_patterns (pattern_type,category,title,description,pattern_data,"
        "source_dispatch_ids,project_id) VALUES ('a','g','mine','d','{}','[\"d1\"]',?)", (pid,))
    con.execute(
        "INSERT INTO success_patterns (pattern_type,category,title,description,pattern_data,"
        "source_dispatch_ids,project_id) VALUES ('a','g','theirs','d','{}','[\"d1\"]','other-tenant')")
    con.commit()
    con.close()
    assert update_confidence_from_outcome(db, "d1", "T1", "success")["boosted"] == 1
    assert [r["project_id"] for r in _q(db, "SELECT project_id FROM pattern_usage")] == [pid]
    assert _q(db, "SELECT project_id FROM confidence_events")[0]["project_id"] == pid


def test_update_missing_events_table_is_silent_and_commits(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path, ce="")
    sp = _add_sp(db, ["d1"])
    assert update_confidence_from_outcome(db, "d1", "T1", "success") == {
        "boosted": 1, "decayed": 0}
    assert _q(db, "SELECT confidence_score FROM success_patterns WHERE id=?", sp)[0][
        "confidence_score"] == pytest.approx(round(beta_score(1, 0), 6))
    assert len(_q(db, "SELECT * FROM pattern_usage")) == 1


def test_update_resource_error_rolls_back_everything(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    # a VIEW makes the audit INSERT fail with an OperationalError that is not schema-absent
    db = _pdb(tmp_path, ce="CREATE TABLE ce_base (x); CREATE VIEW confidence_events AS SELECT 1 AS dispatch_id;")
    sp = _add_sp(db, ["d1"], conf=0.5, usage=2)
    with caplog.at_level("WARNING"):
        res = update_confidence_from_outcome(db, "d1", "T1", "success")
    assert res == {"boosted": 0, "decayed": 0}
    assert any("rolling back" in r.getMessage() and "d1" in r.getMessage() for r in caplog.records)
    row = _q(db, "SELECT confidence_score, usage_count FROM success_patterns WHERE id=?", sp)[0]
    assert (row["confidence_score"], row["usage_count"]) == (0.5, 2)
    assert _q(db, "SELECT * FROM pattern_usage") == []


def test_update_unexpected_error_rolls_back_and_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    _add_sp(db, ["d1"])
    con = sqlite3.connect(db)
    con.execute("DROP TABLE pattern_usage")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.OperationalError):
        update_confidence_from_outcome(db, "d1", "T1", "success")
    assert len(_q(db, "SELECT * FROM success_patterns")) == 1


def test_update_unknown_status_counts_as_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path)
    _add_sp(db, ["d1"])
    assert update_confidence_from_outcome(db, "d1", "T1", "weird") == {"boosted": 0, "decayed": 1}
    assert _q(db, "SELECT outcome FROM confidence_events")[0]["outcome"] == "weird"


def test_update_project_scoped_pu_without_ce_project(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_OUTCOME_GROUNDING_V2", raising=False)
    db = _pdb(tmp_path, grounding=False)
    _add_sp(db, ["d1"])
    update_confidence_from_outcome(db, "d1", "T1", "success")
    ev = _q(db, "SELECT * FROM confidence_events")[0]
    assert "grounding_source" not in ev and "project_id" not in ev


# ---------------------------------------------------------------------------
# shadow_grounding_compare (R20c; the first four carried over from 56bc98966)
# ---------------------------------------------------------------------------

def _offer(db, dispatch_id, sp_id):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO dispatch_pattern_offered VALUES (?, ?, 't', 'now')",
                (dispatch_id, f"intel_sp_{sp_id}"))
    con.commit()
    con.close()


def test_shadow_missing_db(tmp_path):
    assert shadow_grounding_compare(tmp_path / "no.db", [{"dispatch_id": "x"}]) == {
        "dispatches": [],
        "summary": {"total_dispatches": 0, "diverged_dispatches": 0,
                    "v2_only_grounded": 0, "v1_only_grounded": 0,
                    "junction_available": False},
    }


def test_shadow_full_report_and_read_only(tmp_path):
    db = _pdb(tmp_path)
    both = _add_sp(db, ["d1"], conf=0.6, title="Both")
    v1 = _add_sp(db, ["d1"], conf=0.4, title="OnlyV1")
    v2 = _add_sp(db, [], conf=0.7, title="OnlyV2")
    _offer(db, "d1", both)
    _offer(db, "d1", v2)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO pattern_usage (pattern_id,pattern_title,pattern_hash,success_count,"
                "failure_count) VALUES (?, 't','h',2,1)", (f"intel_sp_{both}",))
    con.commit()
    con.close()
    before = db.read_bytes()
    out = shadow_grounding_compare(db, [{"dispatch_id": " d1 ", "status": "SUCCESS"},
                                        {"dispatch_id": "none", "status": None}])
    assert db.read_bytes() == before
    assert out["summary"] == {"total_dispatches": 2, "diverged_dispatches": 1,
                              "v2_only_grounded": 1, "v1_only_grounded": 1,
                              "junction_available": True}
    d1, none = out["dispatches"]
    assert (d1["dispatch_id"], d1["status"]) == ("d1", "success")
    assert d1["v1_pattern_ids"] == sorted([both, v1])
    assert d1["v2_pattern_ids"] == sorted([both, v2])
    assert (d1["v1_only"], d1["v2_only"], d1["agreement"]) == ([v1], [v2], [both])
    assert d1["has_divergence"] is True
    det = d1["pattern_details"]
    assert set(det) == {both, v1, v2}
    assert det[both] == {"title": "Both", "current_conf": 0.6,
                         "v1_new_conf": round(beta_score(3, 1), 6),
                         "v2_new_conf": round(beta_score(3, 1), 6),
                         "in_v1": True, "in_v2": True}
    assert det[v1]["in_v1"] and not det[v1]["in_v2"]
    assert det[v1]["v1_new_conf"] == round(beta_score(1, 0), 6)
    assert det[v1]["v2_new_conf"] == 0.4
    assert det[v2]["v1_new_conf"] == 0.7
    assert det[v2]["v2_new_conf"] == round(beta_score(1, 0), 6)
    assert (none["status"], none["has_divergence"], none["pattern_details"]) == ("", False, {})


def test_shadow_failure_status_and_no_junction(tmp_path):
    db = _pdb(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE dispatch_pattern_offered")
    con.execute("DROP TABLE pattern_usage")
    con.commit()
    con.close()
    sp = _add_sp(db, ["d1"], conf=0.5)
    out = shadow_grounding_compare(db, [{"dispatch_id": "d1", "status": "failure"}])
    assert out["summary"]["junction_available"] is False
    assert out["summary"]["v1_only_grounded"] == 1
    entry = out["dispatches"][0]
    assert entry["v2_pattern_ids"] == []
    assert entry["pattern_details"][sp]["v1_new_conf"] == round(beta_score(0, 1), 6)
    assert entry["pattern_details"][sp]["v2_new_conf"] == 0.5


def test_shadow_empty_dispatch_list(tmp_path):
    db = _pdb(tmp_path)
    out = shadow_grounding_compare(db, [])
    assert out["dispatches"] == []
    assert out["summary"]["total_dispatches"] == 0
    assert out["summary"]["junction_available"] is True


def test_shadow_unreadable_pattern_usage_counts_as_zero(tmp_path):
    # pattern_usage exists but lacks the count columns: the read raises
    # OperationalError, which the report swallows and treats as 0/0 counts.
    db = _pdb(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE pattern_usage")
    con.execute("CREATE TABLE pattern_usage (pattern_id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO pattern_usage VALUES ('intel_sp_1')")
    con.commit()
    con.close()
    sp = _add_sp(db, ["d1"], conf=0.5, title="T")
    assert sp == 1
    out = shadow_grounding_compare(db, [{"dispatch_id": "d1", "status": "success"}])
    det = out["dispatches"][0]["pattern_details"][sp]
    assert det == {"title": "T", "current_conf": 0.5,
                   "v1_new_conf": round(beta_score(1, 0), 6), "v2_new_conf": 0.5,
                   "in_v1": True, "in_v2": False}
