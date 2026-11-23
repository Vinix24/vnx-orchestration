"""One-time fill of declared rework edges into rework_edges (herwerk H3-b)."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backfill_rework_edges as backfill
import quality_db_init as qdi

PID = "tproj"


def _bootstrap(path: Path) -> Path:
    assert qdi.bootstrap_qi_db(path) is True
    return path


def _fixture(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    specs = {
        "s1": {"dispatch_id": "d-round", "project_id": PID, "work_ref": "origin/dispatch/d-orig"},
        "s2": {"dispatch_id": "d-oi", "project_id": PID, "parent_dispatch": "d-orig", "origin_oi": "OI-12"},
        "s3": {"dispatch_id": "d-pr", "project_id": PID, "parent_dispatch": "d-orig2", "origin_pr": 77},
        "s4": {"dispatch_id": "d-self", "project_id": PID, "work_ref": "dispatch/d-self"},
        "s5": {"dispatch_id": "d-both", "project_id": PID, "parent_dispatch": "d-orig",
               "origin_oi": "OI-1", "origin_pr": 2},
        "s6": {"dispatch_id": "d-other", "project_id": "elsewhere", "parent_dispatch": "d-orig"},
        "s7": {"dispatch_id": "d-plain", "project_id": PID},
    }
    for name, spec in specs.items():
        d = data / "dispatches" / "pending" / name
        d.mkdir(parents=True)
        (d / "dispatch-spec.json").write_text(json.dumps(spec))
    (data / "dispatches" / "pending" / "bad").mkdir()
    (data / "dispatches" / "pending" / "bad" / "dispatch-spec.json").write_text("{not json")
    state = data / "state"
    state.mkdir()
    receipts = [
        {"dispatch_id": "d-round", "project_id": PID, "parent_dispatch": "d-orig"},
        {"dispatch_id": "d-rc", "project_id": PID, "parent_dispatch": "d-orig3"},
        {"dispatch_id": "d-bad", "project_id": PID, "parent_dispatch": "has space"},
        {"dispatch_id": "d-x", "project_id": "elsewhere", "parent_dispatch": "d-orig"},
    ]
    (state / "t0_receipts.ndjson").write_text("\n".join(json.dumps(r) for r in receipts) + "\nnot json\n")
    return data


def _rows(db: Path) -> list:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT project_id, rework_dispatch_id, origin_dispatch_id, kind, class, source "
            "FROM rework_edges ORDER BY 1, 2, 3, 4").fetchall()


def _other_tables(db: Path) -> dict:
    with closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT * FROM "{n}"').fetchall() for n in names if n != "rework_edges"}


def test_dry_run_writes_nothing_and_counts_per_kind(tmp_path, capsys):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    capsys.readouterr()
    assert backfill.main(["--project-id", PID, "--data-dir", str(data)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["apply"] is False
    assert out["declared"] == {"round": 3, "oi_fix": 1, "pr_fix": 1, "blame": 0}
    assert out["skipped"] == {"spec_malformed": 1, "spec_ambiguous_origin": 1, "receipt_bad_origin": 1,
                              "receipt_malformed": 1}
    assert _rows(db) == []


def test_two_apply_runs_give_the_same_table(tmp_path):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    first = backfill.run(data, db, PID, apply=True)
    rows = _rows(db)
    second = backfill.run(data, db, PID, apply=True)
    assert _rows(db) == rows
    assert sum(first["written"].values()) == len(rows) == 4
    assert sum(second["written"].values()) == 0
    assert ("tproj", "d-round", "d-orig", "round", "rework", "spec") in rows
    assert ("tproj", "d-oi", "d-orig", "oi_fix", "rework", "spec") in rows
    assert all(r[3] != "blame" for r in rows)


def test_the_other_project_is_never_written_or_counted(tmp_path):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    result = backfill.run(data, db, PID, apply=True)
    assert sum(result["declared"].values()) == 5
    assert {r[0] for r in _rows(db)} == {PID}
    other = backfill.run(data, db, "elsewhere", apply=True)
    assert sum(other["declared"].values()) == 2
    assert [r for r in _rows(db) if r[0] == "elsewhere"] == [
        ("elsewhere", "d-other", "d-orig", "round", "rework", "spec"),
        ("elsewhere", "d-x", "d-orig", "round", "rework", "receipt"),
    ]
    assert len([r for r in _rows(db) if r[0] == PID]) == 4


def test_apply_touches_no_other_table(tmp_path):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    before = _other_tables(db)
    backfill.run(data, db, PID, apply=True)
    assert _other_tables(db) == before


def test_apply_without_a_database_fails(tmp_path):
    assert backfill.main(["--project-id", PID, "--data-dir", str(tmp_path), "--apply"]) == 1
