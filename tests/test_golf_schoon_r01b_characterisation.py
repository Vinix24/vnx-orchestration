"""Characterisation tests for golf-schoon R01b (extract-method refactor).

Pins the observable behaviour of ``materialize_views`` and the 02_rls_policy
``verify``. Real temp dirs and real sqlite.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.aggregator.build_central_view import (
    SOURCE_DBS,
    ProjectEntry,
    load_registry,
    materialize_views,
)

RLS_DIR = REPO_ROOT / "scripts/benchmark/field-tests/tasks/t1_trivial/02_rls_policy"


# --------------------------------------------------------------------------
# materialize_views
# --------------------------------------------------------------------------


def _mk_qi(path: Path, rows, extra_col: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cols = "id INTEGER PRIMARY KEY, pattern TEXT, project_id TEXT"
    if extra_col:
        cols += ", extra TEXT"
    con.execute(f"CREATE TABLE success_patterns ({cols})")
    for r in rows:
        if extra_col:
            con.execute("INSERT INTO success_patterns VALUES (?,?,?,?)", (*r, "x"))
        else:
            con.execute("INSERT INTO success_patterns VALUES (?,?,?)", r)
    con.commit()
    con.close()


def _mk_rc(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE dispatches (dispatch_id TEXT, state TEXT, project_id TEXT)")
    con.execute("INSERT INTO dispatches VALUES ('d1','done',NULL)")
    con.commit()
    con.close()


@pytest.fixture
def projects(tmp_path: Path) -> list[ProjectEntry]:
    specs = []
    for name, pid, extra in [("alpha", "pa", False), ("beta", "pb", True), ("gamma", "pg", False)]:
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        if name != "gamma":  # gamma has no DBs at all
            _mk_qi(state / "quality_intelligence.db",
                   [(1, f"{name}1", pid), (2, f"{name}2", None), (3, f"{name}3", "")],
                   extra_col=extra)
            _mk_rc(state / "runtime_coordination.db")
        specs.append({"name": name, "path": str(proj), "project_id": pid})
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"schema_version": 1, "projects": specs}))
    return load_registry(reg)


def _dump(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        out = {}
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for n in names:
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{n}")')]
            rows = sorted(con.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr)
            out[n] = (cols, rows)
        return out
    finally:
        con.close()


def test_materialize_full_output_pinned(projects, tmp_path):
    view = tmp_path / "agg" / "v.db"
    plan = materialize_views(view, projects)
    assert plan["dry_run"] is False
    assert plan["view_db"] == str(view)
    tables = [p["table"] for p in plan["projects"]]
    assert "success_patterns_unified" in tables
    sp = next(p for p in plan["projects"] if p["table"] == "success_patterns_unified")
    assert sp["rows"] == 6
    assert sp["per_project"] == {"pa": 3, "pb": 3}
    assert plan["pool_state_rows"] == 0 or isinstance(plan["pool_state_rows"], int)
    assert isinstance(plan["coordination_events_rows"], int)

    dump = _dump(view)
    cols, rows = dump["success_patterns_unified"]
    assert "extra" in cols and "project_id" in cols
    pid_idx = cols.index("project_id")
    id_idx = cols.index("pattern")
    got = sorted((r[id_idx], r[pid_idx]) for r in rows)
    assert got == [("alpha1", "pa"), ("alpha2", "pa"), ("alpha3", "pa"),
                   ("beta1", "pb"), ("beta2", "pb"), ("beta3", "pb")]
    extra_idx = cols.index("extra")
    assert [r[extra_idx] for r in rows if r[id_idx].startswith("alpha")] == [None] * 3
    assert {r[extra_idx] for r in rows if r[id_idx].startswith("beta")} == {"x"}


def test_materialize_deterministic_and_replaces_existing_file(projects, tmp_path):
    view = tmp_path / "v.db"
    view.write_text("not a database")
    p1 = materialize_views(view, projects)
    d1 = _dump(view)
    p2 = materialize_views(view, projects)
    assert p1 == p2
    assert d1 == _dump(view)


def test_materialize_skips_missing_source_dbs(tmp_path):
    proj = ProjectEntry(name="ghost", path=tmp_path / "ghost", project_id="g")
    plan = materialize_views(tmp_path / "v.db", [proj])
    assert plan["dry_run"] is False
    assert [p for p in plan["projects"] if p.get("table") == "success_patterns_unified"] == []


def test_materialize_unreadable_source_is_skipped(tmp_path, projects):
    bad = projects[0].state_dir / "quality_intelligence.db"
    bad.write_bytes(b"garbage" * 100)
    plan = materialize_views(tmp_path / "v.db", projects)
    sp = next(p for p in plan["projects"] if p["table"] == "success_patterns_unified")
    assert "pa" not in sp["per_project"]
    assert sp["per_project"] == {"pb": 3}


def test_dry_run_plan_and_stderr_pinned(projects, tmp_path, capsys):
    view = tmp_path / "x" / "v.db"
    plan = materialize_views(view, projects, dry_run=True)
    assert not view.exists() and not view.parent.exists()
    assert plan["dry_run"] is True
    assert [p["name"] for p in plan["projects"]] == ["alpha", "beta", "gamma"]
    assert plan["projects"][0]["attached"] == [
        {"db": d, "path": str(projects[0].state_dir / d),
         "exists": (projects[0].state_dir / d).is_file()} for d in SOURCE_DBS
    ]
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("DRY-RUN: would attach project=pa path=")
    assert err[0].endswith(f"dbs present)")
    assert "/%d dbs present)" % len(SOURCE_DBS) in err[0]
    assert err[2].startswith("DRY-RUN: would attach project=pg") and "(0/" in err[2]





# --------------------------------------------------------------------------
# 02_rls_policy verify
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rls():
    spec = importlib.util.spec_from_file_location("rls_verify_r01", RLS_DIR / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workdir(tmp_path: Path, rls, migrate: str | None, init: bool = True) -> Path:
    seed = tmp_path / rls.SEED_REL
    seed.mkdir(parents=True)
    if migrate is not None:
        (seed / "migrate.sql").write_text(migrate)
    if init:
        (seed / "init_seed_db.sql").write_text((RLS_DIR / "seed/init_seed_db.sql").read_text())
    return tmp_path


GOOD = """
CREATE TABLE IF NOT EXISTS scan_quota_new (
  scan_id TEXT, used_count INTEGER, quota_limit INTEGER, project_id TEXT NOT NULL DEFAULT 'default',
  UNIQUE (project_id, scan_id));
INSERT OR IGNORE INTO scan_quota_new (scan_id, used_count, quota_limit)
  SELECT scan_id, used_count, quota_limit FROM scan_quota WHERE EXISTS
  (SELECT 1 FROM pragma_table_info('scan_quota') WHERE name='scan_id')
  AND NOT EXISTS (SELECT 1 FROM pragma_table_info('scan_quota') WHERE name='project_id');
"""


def test_verify_missing_migrate(tmp_path, rls):
    wd = _workdir(tmp_path, rls, None)
    assert rls.verify(wd, {}) == {
        "pass": False, "evidence": "migrate.sql not written by worker",
        "details": {"pass_count": 0, "expected": 5, "files_written": []}}


def test_verify_missing_init(tmp_path, rls):
    wd = _workdir(tmp_path, rls, "SELECT 1;", init=False)
    assert rls.verify(wd, {}) == {
        "pass": False, "evidence": "seed init script missing (test bug)",
        "details": {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]}}


def test_verify_migration_failed(tmp_path, rls):
    wd = _workdir(tmp_path, rls, "THIS IS NOT SQL;")
    out = rls.verify(wd, {})
    assert out["pass"] is False
    assert out["evidence"].startswith("migration failed: sqlite3 rc=")
    assert out["details"] == {"pass_count": 0, "expected": 5, "files_written": ["migrate.sql"]}


def test_verify_seed_init_failed(tmp_path, rls):
    wd = _workdir(tmp_path, rls, "SELECT 1;", init=False)
    (wd / rls.SEED_REL / "init_seed_db.sql").write_text("BROKEN SQL;")
    out = rls.verify(wd, {})
    assert out["evidence"].startswith("seed init failed: sqlite3 rc=")
    assert out["details"]["files_written"] == ["migrate.sql"]


def test_verify_noop_migration_raises_operational_error(tmp_path, rls):
    # pinned existing behaviour: without a project_id column the later INSERT
    # is not guarded and the OperationalError escapes verify().
    wd = _workdir(tmp_path, rls, "SELECT 1;")
    with pytest.raises(sqlite3.OperationalError):
        rls.verify(wd, {})


def test_verify_full_migration_all_checks(tmp_path, rls):
    mig = """
ALTER TABLE scan_quota RENAME TO scan_quota_old;
CREATE TABLE scan_quota (scan_id TEXT NOT NULL, used_count INTEGER, quota_limit INTEGER,
  project_id TEXT NOT NULL DEFAULT 'default', UNIQUE (project_id, scan_id));
INSERT INTO scan_quota (scan_id, used_count, quota_limit) SELECT scan_id, used_count, quota_limit FROM scan_quota_old;
DROP TABLE scan_quota_old;
CREATE INDEX idx_scan_quota_project ON scan_quota(project_id);
"""
    wd = _workdir(tmp_path, rls, mig)
    out = rls.verify(wd, {})
    d = out["details"]
    names = [c["name"] for c in d["checks"]]
    assert names == [
        "3 original rows scoped to 'default' tenant",
        "cross-tenant insert allowed",
        "same-tenant duplicate rejected",
        "composite UNIQUE on (project_id, scan_id) exists",
        "index on (project_id) exists",
        "migration is idempotent (re-runs cleanly)",
    ]
    # pinned: this inline UNIQUE is not an index row with sql text, and the
    # second apply of the non-idempotent script fails.
    assert [c["ok"] for c in d["checks"]] == [True, True, True, False, True, False]
    assert d["idempotent"] is False
    assert d["checks"][-1]["note"].startswith("sqlite3 rc=1: Error near line 5")
    assert d["pass_count"] == 4 and out["pass"] is False
    assert out["evidence"].count("FAIL") == 2
    assert d["files_written"] == ["migrate.sql"]

