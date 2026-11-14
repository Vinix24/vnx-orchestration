"""Characterisation tests for golf-schoon R01 (extract-method refactor).

Pins the observable behaviour of ``materialize_views``, ``lane_adapter.dispatch``
and the 02_rls_policy ``verify`` so the refactor provably changes nothing.
Real temp dirs and real sqlite; only the dispatcher subprocesses are patched.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"))

import lane_adapter  # noqa: E402
from scripts.aggregator.build_central_view import (  # noqa: E402
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
# lane_adapter.dispatch
# --------------------------------------------------------------------------


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports, tmp_path / "other"))
    monkeypatch.delenv("VNX_BENCH_CLAUDE_HEADLESS", raising=False)
    calls: list = []
    state = {"seed": ("", None), "ret": (0, "out", "err"), "wd": (None, "no cell")}

    monkeypatch.setattr(lane_adapter, "_main_seed_status", lambda p: state["seed"])

    def fake_provider(lane, did, instr, paths, deadline, role="backend-developer"):
        calls.append(("provider", did, instr, role))
        if isinstance(state["ret"], Exception):
            raise state["ret"]
        return state["ret"]

    def fake_claude(lane, did, instr, paths, deadline, role="backend-developer"):
        calls.append(("claude", did, instr, role))
        return state["ret"]

    monkeypatch.setattr(lane_adapter, "_provider_dispatch", fake_provider)
    monkeypatch.setattr(lane_adapter, "_claude_subprocess_headless", fake_claude)
    monkeypatch.setattr(lane_adapter, "_resolve_provider_workdir", lambda o, e: state["wd"])
    return reports, calls, state


KIMI = {"id": "kimi-x", "provider": "litellm:moonshot", "model_arg": "k"}
CLAUDE = {"id": "claude-x", "provider": "claude", "model_arg": "sonnet"}


def test_dispatch_seed_dirty_refused(adapter):
    _, calls, state = adapter
    state["seed"] = (" M seed/x", None)
    r = lane_adapter.dispatch(KIMI, "t1", 2, "do", "p", 5)
    assert not r.success and r.wallclock_seconds == 0.0 and calls == []
    assert r.dispatch_id.startswith("bench-kimi-x-t1-r2-")
    assert r.error == ("main-repo seed paths dirty before dispatch; refusing benchmark "
                       "cell: M seed/x".replace("M seed", " M seed", 1)) or \
        r.error.startswith("main-repo seed paths dirty before dispatch; refusing benchmark cell:")
    state["seed"] = ("", "boom")
    r = lane_adapter.dispatch(KIMI, "t1", 2, "do", "p", 5)
    assert r.error == "boom" and calls == []


def test_dispatch_claude_refused_without_headless_flag(adapter):
    _, calls, _ = adapter
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert not r.success and calls == []
    assert r.error.startswith("claude benchmark lane refused:")
    assert "VNX_BENCH_CLAUDE_HEADLESS=1" in r.error


def test_dispatch_claude_headless_flag_routes_provider(adapter, monkeypatch):
    reports, calls, state = adapter
    monkeypatch.setenv("VNX_BENCH_CLAUDE_HEADLESS", "1")
    state["wd"] = (Path("/tmp/wd"), None)
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert [c[0] for c in calls] == ["provider"]
    assert r.error == "no_report (worker rc=0 but never emitted unified_report)"
    assert r.workdir == Path("/tmp/wd")


def test_dispatch_forced_headless_routes_claude_subprocess(adapter, monkeypatch):
    _, calls, _ = adapter
    monkeypatch.setattr(lane_adapter, "HEADLESS_FORCED_MODELS", {"sonnet"})
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert [c[0] for c in calls] == ["claude"]
    assert r.workdir is None  # not via_provider
    assert r.error.startswith("no_report")


def test_dispatch_success_with_report_and_completion_protocol(adapter):
    reports, calls, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    state["ret"] = (0, "o" * 3000, "e" * 3000)

    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t1", 1, "do the thing", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.success and r.error is None
    assert r.report_path == reports / f"{r.dispatch_id}.md"
    assert len(r.stdout) == 2000 and len(r.stderr) == 2000
    instr = calls[0][2]
    assert instr.startswith("do the thing\n\n# COMPLETION PROTOCOL (required)\n")
    assert f"  {reports / (r.dispatch_id + '.md')}\n" in instr
    assert calls[0][3] == "backend-developer"


def test_dispatch_alt_report_suffix_and_rc_nonzero_with_report(adapter):
    reports, calls, state = adapter
    state["ret"] = (3, "o", "e")
    state["wd"] = (None, "x")

    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}_report.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t1", 1, "x", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.report_path.name.endswith("_report.md")
    assert r.error == "rc=3 (report present)"


def test_dispatch_failure_reasons_without_report(adapter, monkeypatch):
    _, _, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    state["ret"] = (1, "o", "benchmark provider isolation required: nope")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert r.error.startswith("provider isolation failed; refusing shared main checkout: ")
    state["ret"] = (1, "o", "benchmark seed materialization failed")
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error.startswith("provider isolation")
    state["ret"] = (1, "o", "plain")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert r.error.startswith("immediate_exit (wall=") and ", rc=1)" in r.error
    monkeypatch.setattr(lane_adapter, "MIN_REAL_WALLCLOCK_SECONDS", -1.0)
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error == "rc=1 report_exists=False"


def test_dispatch_unscorable_when_no_workdir(adapter):
    _, _, state = adapter
    state["wd"] = (None, "cell missing")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    # no_report outranks unscorable; workdir stays None
    assert r.error.startswith("no_report") and r.workdir is None


def test_dispatch_unscorable_reason_when_report_present(adapter):
    reports, _, state = adapter
    state["wd"] = (None, "cell missing")
    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.error == "unscorable: cell missing"


def test_dispatch_timeout_paths(adapter):
    _, _, state = adapter
    state["ret"] = subprocess.TimeoutExpired("cmd", 5)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 7)
    assert not r.success and r.error == "timeout after 7s"
    assert r.stdout == "" and r.stderr == str(state["ret"]) and r.report_path is None
    # seed contaminated after timeout
    calls = iter([("", None), (" M a", None)])
    lane_adapter._main_seed_status = lambda p: next(calls)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 7)
    assert r.error == "main-repo seed contamination after timeout:  M a"


def test_dispatch_seed_contaminated_after_dispatch_overrides(adapter):
    reports, _, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    seq = iter([("", None), (" M s", None)])
    lane_adapter._main_seed_status = lambda p: next(seq)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert not r.success
    assert r.error == "main-repo seed contamination after dispatch:  M s"
    seq2 = iter([("", None), ("", "gone")])
    lane_adapter._main_seed_status = lambda p: next(seq2)
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error == "gone"


def test_dispatch_skill_names_role_and_structured_prompt(adapter, monkeypatch):
    _, calls, _ = adapter
    seen = {}

    def fake_build(names, instr, root):
        seen["a"] = (list(names), instr, root)
        return "WRAPPED"

    monkeypatch.setattr(lane_adapter, "build_structured_prompt", fake_build)
    lane_adapter.dispatch(KIMI, "t", 1, "raw", "p", 5, skill_names=["", "security-engineer"])
    assert seen["a"] == (["", "security-engineer"], "raw", lane_adapter.SKILLS_ROOT)
    assert calls[0][2].startswith("WRAPPED\n\n# COMPLETION PROTOCOL")
    assert calls[0][3] == "security-engineer"


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
