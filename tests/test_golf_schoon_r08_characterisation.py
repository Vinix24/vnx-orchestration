"""Characterisation of migrate_dry_run.render_markdown / _scan_collisions_in_db (golf schoon R08).

Pins observable behaviour so the extract-method refactor cannot change it.
Existing neighbours: tests/test_migrate_dry_run.py.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import migrate_dry_run as md  # noqa: E402


def _plan(**over):
    plan = {
        "generated_at": "2026-01-01T00:00:00Z",
        "projects": [{"project_id": "a", "name": "Alpha", "path": "/p/a"},
                     {"project_id": "b", "name": "Beta", "path": "/p/b"}],
        "row_count_plan": [
            {"project_id": "b", "db": "x.db", "table": "t2", "rows": 1234, "has_project_id_column": False},
            {"project_id": "a", "db": "x.db", "table": "t1", "rows": 5, "has_project_id_column": True},
            {"project_id": "a", "db": "a.db", "table": "t9", "rows": 0, "has_project_id_column": True},
        ],
        "expected_central_totals": [{"db": "x.db", "table": "t1", "rows": 1239}],
        "collisions": {"dispatch_id": {"d2": ["a", "b"], "d1": ["a", "b"]},
                       "pattern_id": {"p1": ["a", "b"]}},
        "schema_drift": {"reference": "a", "projects": {"b": {"x.db": {
            "missing_tables": ["m1", "m2"], "extra_tables": ["e1"],
            "column_diffs": {"t1": {"missing": ["c"], "extra": ["d"]}}}}}},
        "read_errors": [{"project_id": "b", "db": "x.db", "phase": "select", "table": "t1", "error": "a|b"},
                        {"error": "boom"}],
    }
    plan.update(over)
    return plan


def _empty_plan():
    return _plan(projects=[], row_count_plan=[], expected_central_totals=[],
                 collisions={"dispatch_id": {}, "pattern_id": {}},
                 schema_drift={"projects": {"a": {"x.db": {"missing_tables": [], "extra_tables": [], "column_diffs": {}}}}},
                 read_errors=None)


def _many_plan():
    many = {f"k{i:03d}": ["a", "b"] for i in range(53)}
    return _plan(collisions={"dispatch_id": many, "pattern_id": dict(many)}, read_errors=[])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Digests recorded on the pre-refactor code.
DIGESTS = {"full": "1da34cc9e187e7dfbc3c46426a7f469b3f35deedf28c76742fb6d196b39c4353", "empty": "b10d75d575c218842fa5111fed5848efd8b39b9246268c14e575bff63a9b82d8", "many": "016b8bfe23cf66ef8ecd18d83e7bae0f5cf2fa6a902327ff5e87fe4adb3af71f"}


def test_render_full_digest_and_shape():
    out = md.render_markdown(_plan())
    assert _sha(out) == DIGESTS["full"]
    lines = out.split("\n")
    assert lines[0] == "# Phase 6 P4 — One-shot data import DRY-RUN REPORT"
    assert lines[-1] == ""
    heads = [ln for ln in lines if ln.startswith("## ")]
    assert heads == ["## Projects in scope", "## Per-project row-count plan (per source DB)",
                     "## Expected central totals after import", "## Collisions detected",
                     "## Schema drift", "## Read errors (fatal if non-empty)",
                     "## Operator pre-flight checklist"]
    assert "| a.db | t9 | 0 | [ok] |" in lines
    assert "| x.db | t2 | 1,234 | [!] |" in lines
    assert lines.index("| a.db | t9 | 0 | [ok] |") < lines.index("| x.db | t1 | 5 | [ok] |")
    assert lines.index("| `d1` | a, b |") < lines.index("| `d2` | a, b |")
    assert "    - column drift in `t1`: missing=['c'] extra=['d']" in lines
    assert "| `?` | ? | ? | - | `boom` |" in lines
    assert "| `b` | x.db | select | t1 | `a b` |" in lines


def test_render_empty_digest():
    out = md.render_markdown(_empty_plan())
    assert _sha(out) == DIGESTS["empty"]
    assert "_No cross-project key collisions detected._" in out
    assert "_No schema drift detected._" in out
    assert "_No source DBs or tables produced read errors._" in out


def test_render_collisions_truncated_at_fifty():
    out = md.render_markdown(_many_plan())
    assert _sha(out) == DIGESTS["many"]
    assert out.count("| _…3 more_ | |") == 2
    assert "k050" not in out


def test_render_only_pattern_collisions():
    out = md.render_markdown(_plan(collisions={"dispatch_id": {}, "pattern_id": {"p": ["a", "b"]}}))
    assert "### `runtime_coordination.dispatch_id` collisions" not in out
    assert "### `quality_intelligence.pattern_usage.pattern_id` collisions" in out
    assert "Collision-handling rule (plan §5.2)" in out


# --- _scan_collisions_in_db -------------------------------------------------

def _scan(tmp_path, ddl, inserts=(), alias="src"):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    for s in ddl:
        c.execute(s)
    for s in inserts:
        c.execute(s)
    c.commit()
    c.close()
    con = sqlite3.connect(":memory:")
    con.execute(f"ATTACH DATABASE '{db}' AS {alias}")
    d, p, errs = {}, {}, []
    ret = md._scan_collisions_in_db(con, alias, "proj", d, p, errs, "s.db")
    con.close()
    return ret, d, p, errs


def test_scan_scalar_json_and_entity_columns(tmp_path):
    ret, d, p, errs = _scan(
        tmp_path,
        ["CREATE TABLE dispatches (dispatch_id TEXT, parent_dispatch TEXT, note TEXT)",
         "CREATE TABLE pattern_usage (pattern_id TEXT)",
         "CREATE TABLE success_patterns (source_dispatch_ids TEXT)",
         "CREATE TABLE coordination_events (entity_type TEXT, entity_id TEXT)",
         "CREATE TABLE plain (a TEXT)"],
        ["INSERT INTO dispatches VALUES ('d1','d0','x'),('d1',NULL,'y'),('','','z')",
         "INSERT INTO pattern_usage VALUES ('p1'),(NULL)",
         "INSERT INTO success_patterns VALUES ('[\"d5\", null, \"\", \"d6\"]'),('not json'),('{\"a\":1}'),(NULL),('')",
         "INSERT INTO coordination_events VALUES ('Pattern','p2'),('dispatch','d7'),('other','o'),(NULL,'q'),('dispatch','')"],
    )
    assert ret is None and errs == []
    assert d == {"d1": ["proj", "proj"], "d0": ["proj"], "d5": ["proj"], "d6": ["proj"], "d7": ["proj"]}
    assert p == {"p1": ["proj"], "p2": ["proj"]}
    assert list(d) == ["d1", "d0", "d5", "d6", "d7"]


def test_scan_select_error_recorded_and_continues(tmp_path, monkeypatch):
    real_list, real_cols = md._list_user_tables, md._table_columns_dry
    monkeypatch.setattr(md, "_list_user_tables", lambda con, alias: ["ghost"] + real_list(con, alias))
    monkeypatch.setattr(md, "_table_columns_dry",
                        lambda con, alias, t: ["dispatch_id"] if t == "ghost" else real_cols(con, alias, t))
    _, d, p, errs = _scan(
        tmp_path,
        ["CREATE TABLE zdispatches (dispatch_id TEXT)"],
        ["INSERT INTO zdispatches VALUES ('d1')"],
    )
    assert d == {"d1": ["proj"]}
    assert [e["phase"] for e in errs] == ["select"]
    assert errs[0]["table"] == "ghost" and errs[0]["project_id"] == "proj" and errs[0]["db"] == "s.db"
    assert set(errs[0]) == {"project_id", "db", "phase", "table", "error"}


def test_scan_list_tables_error(tmp_path, monkeypatch):
    def boom(con, alias):
        raise sqlite3.OperationalError("cannot list")
    monkeypatch.setattr(md, "_list_user_tables", boom)
    _, d, p, errs = _scan(tmp_path, ["CREATE TABLE dispatches (dispatch_id TEXT)"])
    assert d == {} and p == {}
    assert errs == [{"project_id": "proj", "db": "s.db", "phase": "list_tables", "error": "cannot list"}]


def test_scan_table_info_error(tmp_path, monkeypatch):
    def boom(con, alias, table):
        raise sqlite3.OperationalError("nope")
    monkeypatch.setattr(md, "_table_columns_dry", boom)
    _, d, p, errs = _scan(tmp_path, ["CREATE TABLE dispatches (dispatch_id TEXT)"])
    assert d == {} and [e["phase"] for e in errs] == ["table_info"]
    assert errs[0]["table"] == "dispatches" and errs[0]["error"] == "nope"


def test_scan_entity_table_without_type_column_is_not_entity(tmp_path):
    _, d, p, errs = _scan(tmp_path, ["CREATE TABLE coordination_events (entity_id TEXT)"],
                          ["INSERT INTO coordination_events VALUES ('x')"])
    assert d == {} and p == {} and errs == []
