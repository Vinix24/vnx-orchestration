"""rework_edges (v36): the edge table, fill-once record_edge and the one-time fill (herwerk H3)."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backfill_rework_edges as backfill  # noqa: E402
import quality_db_init as qdi  # noqa: E402
import rework_attribution as ra  # noqa: E402

PID = "tproj"


def _bootstrap(path: Path) -> Path:
    assert qdi.bootstrap_qi_db(path) is True
    return path


def _edge(conn, pid="tproj", rework="r1", origin="o1", kind="round", cls="rework"):
    return ra.record_edge(conn, pid, rework, origin, kind, cls, source="spec", evidence={"k": 1})


def _tables(db: Path) -> dict:
    with closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE '%\\_fts%' ESCAPE '\\'")]
        return {n: conn.execute(f"PRAGMA table_info({n})").fetchall() for n in names}


def test_table_missing_before_v36_and_present_with_the_adr007_key(tmp_path, monkeypatch):
    before = {k: v for k, v in qdi.MIGRATIONS.items() if k < 36}
    monkeypatch.setattr(qdi, "MIGRATIONS", before)
    old = _bootstrap(tmp_path / "old.db")
    assert "rework_edges" not in _tables(old)
    monkeypatch.undo()
    new = _bootstrap(tmp_path / "new.db")
    assert qdi.HIGHEST_QI_VERSION == 36
    with closing(sqlite3.connect(new)) as conn:
        pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(rework_edges)"), key=lambda r: r[5]) if r[5]]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
    assert pk == ["project_id", "rework_dispatch_id", "origin_dispatch_id", "kind"]


def test_a_foreign_table_of_that_name_is_not_adopted(tmp_path):
    db = _bootstrap(tmp_path / "qi.db")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TABLE rework_edges")
        conn.execute("CREATE TABLE rework_edges (x TEXT)")
        conn.execute("PRAGMA user_version = 35")
        conn.commit()
        with pytest.raises(RuntimeError):
            qdi._migrate_v36(conn)


def test_second_record_edge_changes_nothing(tmp_path):
    db = _bootstrap(tmp_path / "qi.db")
    with closing(sqlite3.connect(db)) as conn:
        assert _edge(conn) is True
        first = conn.execute("SELECT * FROM rework_edges").fetchall()
        assert ra.record_edge(conn, PID, "r1", "o1", "round", "churn", source="receipt", evidence={"z": 2}) is False
        assert conn.execute("SELECT * FROM rework_edges").fetchall() == first


@pytest.mark.parametrize("field,bad", [("kind", "other"), ("cls", "other"), ("kind", None), ("cls", ""), ("pid", " ")])
def test_unknown_values_raise_before_any_write(tmp_path, field, bad):
    db = _bootstrap(tmp_path / "qi.db")
    kwargs = {field: bad}
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(ValueError):
            _edge(conn, **{("pid" if k == "pid" else k): v for k, v in kwargs.items()})
        assert conn.execute("SELECT COUNT(*) FROM rework_edges").fetchone()[0] == 0


def test_unknown_source_raises(tmp_path):
    db = _bootstrap(tmp_path / "qi.db")
    with closing(sqlite3.connect(db)) as conn:
        with pytest.raises(ValueError):
            ra.record_edge(conn, PID, "r1", "o1", "round", "rework", source="guess")
        assert conn.execute("SELECT COUNT(*) FROM rework_edges").fetchone()[0] == 0


def test_two_projects_do_not_see_each_others_edges(tmp_path):
    db = _bootstrap(tmp_path / "qi.db")
    with closing(sqlite3.connect(db)) as conn:
        assert _edge(conn, pid="a") is True
        assert _edge(conn, pid="b") is True
        assert _edge(conn, pid="b", rework="r2") is True
        assert [e["rework"] for e in ra.load_edges(conn, "a")] == ["r1"]
        assert [e["rework"] for e in ra.load_edges(conn, "b")] == ["r1", "r2"]
        assert ra.load_edges(conn, "c") == []


def test_load_edges_on_a_store_without_the_table_is_empty(tmp_path):
    with closing(sqlite3.connect(tmp_path / "bare.db")) as conn:
        assert ra.load_edges(conn, PID) == []


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


def _count(db: Path) -> list:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT project_id, rework_dispatch_id, origin_dispatch_id, kind, class, source "
            "FROM rework_edges ORDER BY 1, 2, 3, 4").fetchall()


def test_fill_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    dry = backfill.run(data, db, PID, apply=False)
    assert _count(db) == []
    assert dry["declared"] == {"round": 3, "oi_fix": 1, "pr_fix": 1, "blame": 0}
    assert dry["skipped"] == {"spec_malformed": 1, "spec_ambiguous_origin": 1, "receipt_bad_origin": 1,
                              "receipt_malformed": 1}
    first = backfill.run(data, db, PID, apply=True)
    rows = _count(db)
    second = backfill.run(data, db, PID, apply=True)
    assert _count(db) == rows
    assert sum(first["written"].values()) == len(rows) == 4
    assert sum(second["written"].values()) == 0
    assert ("tproj", "d-round", "d-orig", "round", "rework", "spec") in rows
    assert ("tproj", "d-oi", "d-orig", "oi_fix", "rework", "spec") in rows
    assert all(r[0] == PID and r[3] != "blame" for r in rows)


def test_cli_defaults_to_dry_run(tmp_path, capsys):
    data = _fixture(tmp_path)
    db = _bootstrap(data / "state" / "quality_intelligence.db")
    capsys.readouterr()
    assert backfill.main(["--project-id", PID, "--data-dir", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["apply"] is False
    assert _count(db) == []
    assert backfill.main(["--project-id", PID, "--data-dir", str(data), "--apply"]) == 0
    assert len(_count(db)) == 4


def test_apply_without_a_database_fails(tmp_path):
    assert backfill.main(["--project-id", PID, "--data-dir", str(tmp_path), "--apply"]) == 1


def test_init_leaves_every_existing_table_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(qdi, "MIGRATIONS", {k: v for k, v in qdi.MIGRATIONS.items() if k < 36})
    old = _tables(_bootstrap(tmp_path / "old.db"))
    monkeypatch.undo()
    new = _tables(_bootstrap(tmp_path / "new.db"))
    assert set(new) - set(old) == {"rework_edges"}
    assert {n: new[n] for n in old} == old
