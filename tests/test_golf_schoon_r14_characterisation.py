#!/usr/bin/env python3
"""Golf schoon R14 (dlv-cb90c5e6b2ac): pin producer_freshness_monitor.main and quality_db_init._migrate_v22 /
_migrate_v29 (SQL order, log lines, schema digests, rows, exit codes, output) around an extract-method refactor."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qdi = _load("_r14_quality_db_init", SCRIPTS_DIR / "quality_db_init.py")
pfm = _load("_r14_producer_freshness_monitor", SCRIPTS_DIR / "producer_freshness_monitor.py")


def _conn(path: Path, ddl: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.isolation_level = None
    conn.executescript(ddl)
    return conn


def _run_traced(conn, fn, monkeypatch):
    statements: list[str] = []
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(qdi, "log", lambda level, message: logs.append((level, message)))
    conn.set_trace_callback(statements.append)
    try:
        fn(conn)
    finally:
        conn.set_trace_callback(None)
    # Quoted literals become ? and only the first 60 whitespace-collapsed chars
    # count, so the trace is the same whether or not bound parameters are expanded.
    trace = [re.sub(r"'[^']*'", "?", re.sub(r"\s+", " ", s)).strip()[:60] for s in statements]
    return [t for t in trace if not t.startswith("--")], logs


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _schema(conn) -> dict:
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    return {f"{t}:{n}": _digest(re.sub(r'^CREATE TABLE "?\w+"?', "CREATE TABLE T", sql or "")) for t, n, sql in rows}


_COST_VIEW_SQL = """
    CREATE VIEW cost_per_dispatch AS SELECT dm.dispatch_id, sa.session_model
    FROM dispatch_metadata dm LEFT JOIN session_analytics sa ON sa.dispatch_id = dm.dispatch_id;
"""
_V22_LEGACY = """
    CREATE TABLE dispatch_metadata (
        id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL UNIQUE,
        terminal TEXT NOT NULL, track TEXT NOT NULL, provider TEXT, role TEXT, gate TEXT,
        outcome_status TEXT, pattern_count INTEGER DEFAULT 0, legacy_extra TEXT
    );
    CREATE INDEX idx_dispatch_meta_provider ON dispatch_metadata (provider);
    CREATE TABLE session_analytics (id INTEGER PRIMARY KEY, session_id TEXT, dispatch_id TEXT, session_model TEXT);
    CREATE VIEW dispatch_success_by_role AS SELECT role FROM dispatch_metadata;
    CREATE VIEW intelligence_effectiveness AS SELECT outcome_status FROM dispatch_metadata;
""" + _COST_VIEW_SQL + """
    INSERT INTO dispatch_metadata (dispatch_id, terminal, track, provider, role, outcome_status, legacy_extra)
    VALUES ('d-1', 'T1', 'A', 'claude', 'backend', 'success', 'x'), ('d-2', 'T2', 'B', 'codex', 'review', NULL, 'y');
"""
_V22_TRACE = [
    "PRAGMA table_info(dispatch_metadata)", "ALTER TABLE dispatch_metadata ADD COLUMN project_id TEXT NOT",
    "PRAGMA table_info(dispatch_metadata)", "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
    "DROP VIEW IF EXISTS dispatch_success_by_role", "DROP VIEW IF EXISTS intelligence_effectiveness",
    "DROP VIEW IF EXISTS cost_per_dispatch", "PRAGMA table_info(dispatch_metadata)",
    "CREATE TABLE _dispatch_metadata_v22 ( id INTEGER PRIMARY KEY", "PRAGMA table_info(_dispatch_metadata_v22)",
    "INSERT OR IGNORE INTO _dispatch_metadata_v22 (dispatch_id, t", "DROP TABLE dispatch_metadata",
    "ALTER TABLE _dispatch_metadata_v22 RENAME TO dispatch_metada",
    "CREATE VIEW IF NOT EXISTS dispatch_success_by_role AS SELECT",
    "CREATE VIEW IF NOT EXISTS intelligence_effectiveness AS SELE",
    "CREATE VIEW IF NOT EXISTS cost_per_dispatch AS SELECT dm.dis",
]
_V22_INDEX_TAIL = ["DROP INDEX IF EXISTS idx_dispatch_meta_provider"] + [
    f"CREATE INDEX IF NOT EXISTS idx_dispatch_meta_{n} ON dispatch_metadata"[:60]
    for n in ("provider", "id", "terminal", "role", "gate", "outcome", "dispatched")]
_V22_LOG_TAIL = [("INFO", "Migrated dispatch_metadata: composite (project_id, provider) index ensured (ADR-007)")]


def test_v22_legacy_rebuild_trace_logs_schema_rows(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "q.db", _V22_LEGACY)
    trace, logs = _run_traced(conn, qdi._migrate_v22, monkeypatch)
    assert trace == _V22_TRACE + _V22_INDEX_TAIL
    assert logs == [
        ("INFO", "Migrated dispatch_metadata: composite UNIQUE (project_id, dispatch_id) (ADR-007)"),
        ("INFO", "Recreated dispatch_metadata-dependent views after table rebuild"),
    ] + _V22_LOG_TAIL
    schema = _schema(conn)
    assert [schema[k] for k in ("view:dispatch_success_by_role", "view:intelligence_effectiveness",
                                "view:cost_per_dispatch", "table:dispatch_metadata")] == [
        "052363e900a424e8", "40f30c52012ce6be", "31703cf571043b32", "e05ee83c72d09ea0"]
    assert sorted(k[len("index:"):] for k in schema if k.startswith("index:")) == [
        f"idx_dispatch_meta_{s}" for s in ("dispatched", "gate", "id", "outcome", "provider", "role", "terminal")]
    rows = conn.execute(
        "SELECT id, dispatch_id, project_id, terminal, provider, outcome_status FROM dispatch_metadata ORDER BY id"
    ).fetchall()
    assert rows == [(1, "d-1", "vnx-dev", "T1", "claude", "success"), (2, "d-2", "vnx-dev", "T2", "codex", None)]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dispatch_metadata)")]
    assert "legacy_extra" not in cols and cols[:3] == ["id", "dispatch_id", "project_id"]


def test_v22_rerun_skips_rebuild_and_only_ensures_indexes(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "q.db", _V22_LEGACY)
    _run_traced(conn, qdi._migrate_v22, monkeypatch)
    before = _schema(conn)
    trace, logs = _run_traced(conn, qdi._migrate_v22, monkeypatch)
    assert (trace, logs) == ([_V22_TRACE[0], _V22_TRACE[3]] + _V22_INDEX_TAIL, _V22_LOG_TAIL)
    assert _schema(conn) == before


_V29_LEGACY = """
    CREATE TABLE session_analytics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL UNIQUE,
        project_path TEXT NOT NULL, terminal TEXT, session_date DATE NOT NULL,
        total_input_tokens INTEGER DEFAULT 0, session_model TEXT DEFAULT 'unknown'
    );
    CREATE INDEX idx_session_dispatch_id ON session_analytics (session_model);
    CREATE INDEX idx_custom_terminal ON session_analytics (terminal);
    CREATE TABLE dispatch_metadata (dispatch_id TEXT, outcome_status TEXT);
""" + _COST_VIEW_SQL + """
    INSERT INTO session_analytics (session_id, project_path, terminal, session_date, total_input_tokens)
    VALUES ('s-1', '/p', 'T1', '2026-11-01', 5), ('s-2', '/p', NULL, '2026-11-02', 7);
"""
_V29_TRACE = [
    "PRAGMA table_info(session_analytics)", "ALTER TABLE session_analytics ADD COLUMN project_id TEXT NOT",
    "PRAGMA table_info(session_analytics)", "ALTER TABLE session_analytics ADD COLUMN context_reset_count",
    "ALTER TABLE session_analytics ADD COLUMN dispatch_id TEXT", "PRAGMA index_list(session_analytics)",
    "PRAGMA index_info(sqlite_autoindex_session_analytics_1)",
    "SELECT name, sql FROM sqlite_master WHERE type=? AND tbl_nam", "DROP VIEW IF EXISTS cost_per_dispatch",
    "PRAGMA table_info(session_analytics)", "CREATE TABLE _session_analytics_v29 ( id INTEGER PRIMARY KEY",
    "PRAGMA table_info(_session_analytics_v29)", "SELECT COUNT(*) FROM session_analytics",
    "INSERT OR IGNORE INTO _session_analytics_v29 (session_id, pr", "SELECT COUNT(*) FROM _session_analytics_v29",
    "DROP TABLE session_analytics", "ALTER TABLE _session_analytics_v29 RENAME TO session_analyti",
    "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
    "CREATE INDEX IF NOT EXISTS idx_custom_terminal ON session_an",
    "CREATE INDEX IF NOT EXISTS idx_session_dispatch ON session_a",
    "CREATE VIEW IF NOT EXISTS cost_per_dispatch AS SELECT dm.dis",
    "SELECT COUNT(*) FROM session_analytics WHERE project_id IS N",
]


def test_v29_legacy_rebuild_trace_logs_schema_rows(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "q.db", _V29_LEGACY)
    trace, logs = _run_traced(conn, qdi._migrate_v29, monkeypatch)
    assert trace == _V29_TRACE
    assert logs == [
        ("INFO", "Migrated session_analytics: composite UNIQUE (project_id, session_id) (ADR-007)"),
        ("INFO", "Replayed 1 captured session_analytics index(es) + ensured idx_session_dispatch"),
        ("INFO", "Recreated cost_per_dispatch view"),
    ]
    schema = _schema(conn)
    assert [schema["view:cost_per_dispatch"], schema["table:session_analytics"]] == [
        "31703cf571043b32", "f08a0e25d9e67a6b"]
    assert sorted(k for k in schema if k.startswith("index:")) == [
        "index:idx_custom_terminal", "index:idx_session_dispatch"]
    rows = conn.execute("SELECT id, session_id, project_id, terminal, total_input_tokens, context_reset_count "
                        "FROM session_analytics ORDER BY id").fetchall()
    assert rows == [(1, "s-1", "vnx-dev", "T1", 5, 0), (2, "s-2", "vnx-dev", None, 7, 0)]


def test_v29_rerun_skips_rebuild(tmp_path, monkeypatch):
    conn = _conn(tmp_path / "q.db", _V29_LEGACY)
    _run_traced(conn, qdi._migrate_v29, monkeypatch)
    before = _schema(conn)
    trace, logs = _run_traced(conn, qdi._migrate_v29, monkeypatch)
    assert (trace, logs) == ([_V29_TRACE[i] for i in (0, 5, 6, -1)], [])
    assert _schema(conn) == before


@pytest.mark.parametrize("edits, message", [
    ([("DEFAULT 'unknown'", "DEFAULT 'unknown', mystery_col TEXT")],
     "V29 migration: session_analytics has column(s) ['mystery_col'] not present in the v29 staging schema — "
     "refusing to silently drop data. Add them to the CREATE TABLE _session_analytics_v29 DDL in _migrate_v29."),
    ([("NOT NULL UNIQUE", "NOT NULL"), ("('s-2', '/p', NULL", "('s-1', '/p', NULL")],
     "V29 migration: row-count mismatch after copy — source had 2 row(s), staging table has 1. "
     "INSERT OR IGNORE silently dropped row(s); refusing to swap the table in."),
])
def test_v29_fail_loud_paths_raise_before_swap(tmp_path, monkeypatch, edits, message):
    ddl = _V29_LEGACY
    for old, new in edits:
        ddl = ddl.replace(old, new)
    conn = _conn(tmp_path / "q.db", ddl)
    with pytest.raises(RuntimeError) as exc:
        _run_traced(conn, qdi._migrate_v29, monkeypatch)
    assert str(exc.value) == message
    assert conn.execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0] == 2


# --- producer_freshness_monitor.main ----------------------------------------------------------------

pf, brr, br, jec = (importlib.import_module(m) for m in (
    "producer_freshness", "beacon_reader_register", "beacon_register", "job_exit_capture"))


def _raise(exc):
    raise exc


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = tmp_path / "registry.yaml"
    config.write_text("producers:\n  - name: trivial\n    type: directory\n"
                      f"    path: '{tmp_path / 'empty'}'\n    cadence_seconds: 86400\n", encoding="utf-8")
    calls: list = []
    finding = {"producer": "beacon_reader_coverage", "key": "orphan", "kind": "no_reader", "cadence_seconds": None}
    monkeypatch.setattr(brr, "check_coverage", lambda: calls.append("coverage") or {"findings": [finding]})
    monkeypatch.setattr(br, "find_duplicate_beacon_writers",
                        lambda data_dir: calls.append(("dupes", data_dir)) or {"b": [Path("/x/1"), Path("/x/2")]})
    monkeypatch.setattr(jec, "harvest_launchd", lambda sd: calls.append(("harvest", sd)) or {"jobs": 0})
    for name in ("run_sweep", "append_report", "write_heartbeat"):
        monkeypatch.setattr(pf, name, lambda *a, _r=getattr(pf, name), _n=name: calls.append(_n) or _r(*a))
    return state_dir, ["--config", str(config), "--state-dir", str(state_dir)], calls


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_full_write_run_order_keys_and_files(cli_env, capsys):
    state_dir, argv, calls = cli_env
    assert pfm.main(argv) == 0
    out = _out(capsys)
    assert calls == ["run_sweep", "coverage", ("dupes", state_dir.parent), ("harvest", state_dir),
                     "append_report", "write_heartbeat"]
    assert list(out)[-4:] == ["beacon_reader_coverage", "job_exits", "report_path", "heartbeat_path"]
    assert out["beacon_reader_coverage"] == {"skipped": False, "no_reader_count": 1, "duplicate_writer_count": 1}
    assert (out["job_exits"], out["status"], out["findings_count"]) == ({"jobs": 0}, "stale", len(out["findings"]))
    assert out["findings"][-1] == {"producer": "beacon_reader_coverage", "key": "b", "kind": "duplicate_writer",
                                   "cadence_seconds": None, "paths": ["/x/1", "/x/2"]}
    assert Path(out["report_path"]).is_file() and Path(out["heartbeat_path"]).is_file()


def test_cli_no_write_skips_harvest_and_files(cli_env, capsys):
    state_dir, argv, calls = cli_env
    assert pfm.main(argv + ["--no-write", "--skip-beacon-checks"]) == 0
    out = _out(capsys)
    assert calls == ["run_sweep"]
    assert (out["beacon_reader_coverage"], out["job_exits"]) == ({"skipped": True}, {"skipped": True})
    assert "report_path" not in out and list(state_dir.iterdir()) == []


def test_cli_bonus_signal_errors_are_recorded_not_raised(cli_env, capsys, monkeypatch):
    _state_dir, argv, _calls = cli_env
    monkeypatch.setattr(brr, "check_coverage", lambda: _raise(ValueError("ast boom")))
    monkeypatch.setattr(jec, "harvest_launchd", lambda sd: _raise(KeyError("cache")))
    assert pfm.main(argv) == 0
    out = _out(capsys)
    assert out["beacon_reader_coverage"] == {"skipped": True, "error": "ast boom"}
    assert out["job_exits"] == {"skipped": True, "error": "'cache'"}


def test_cli_append_oserror_is_exit_io_and_no_heartbeat(cli_env, capsys, monkeypatch):
    _state_dir, argv, calls = cli_env
    monkeypatch.setattr(pf, "append_report", lambda *a: _raise(OSError("disk full")))
    assert pfm.main(argv + ["--skip-job-exits"]) == pfm.EXIT_IO == 20
    assert _out(capsys) == {"ok": False, "error": {"code": "io", "message": "disk full"}}
    assert "write_heartbeat" not in calls


def test_cli_dependency_errors_are_exit_30(tmp_path, capsys, monkeypatch):
    assert pfm.main(["--config", str(tmp_path / "missing.yaml"), "--state-dir", str(tmp_path)]) == 30
    out = _out(capsys)
    assert out["ok"] is False and out["error"]["code"] == "dependency"
    assert out["error"]["message"].startswith("registry: ")
    monkeypatch.setitem(sys.modules, "producer_freshness", None)
    assert pfm.main(["--latest-findings"]) == pfm.EXIT_DEPENDENCY == 30
    assert _out(capsys)["error"]["code"] == "dependency"


def test_cli_latest_findings_runs_no_sweep(cli_env, capsys):
    state_dir, _argv, calls = cli_env
    assert pfm.main(["--state-dir", str(state_dir), "--latest-findings", "--config", "/nonexistent.yaml"]) == 0
    assert calls == [] and _out(capsys)["swept"] is False


def test_cli_state_dir_failure_is_exit_io(cli_env, capsys, monkeypatch):
    _state_dir, argv, _calls = cli_env
    monkeypatch.setattr(pfm, "_default_state_dir", lambda: _raise(RuntimeError("no env")))
    for args in (argv[:2], ["--latest-findings"]):
        assert pfm.main(args) == 20
        assert _out(capsys)["error"] == {"code": "io", "message": "state dir: no env"}


def test_cli_human_output(cli_env, capsys):
    _state_dir, argv, _calls = cli_env
    assert pfm.main(argv + ["--no-write", "--human", "--skip-beacon-checks"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert re.fullmatch(r"producer-freshness sweep \w+ — status=ok findings=0 \(producers=1, keys=0\)", lines[0])
    # Pinned as found: a beacon finding carries no last_seen, so --human raises (Open Items).
    with pytest.raises(KeyError, match="last_seen"):
        pfm.main(argv + ["--no-write", "--human"])


def test_cli_help_text_is_unchanged(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setattr(sys, "argv", ["producer_freshness_monitor.py"])
    with pytest.raises(SystemExit) as exc:
        pfm.main(["--help"])
    assert exc.value.code == 0 and _digest(capsys.readouterr().out) == "cb00d7ee1595d3e1"
