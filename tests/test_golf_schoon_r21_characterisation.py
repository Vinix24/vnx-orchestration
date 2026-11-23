"""Characterisation tests for golf-schoon R21 (dlv-a76ab0c4cf5b).

Pin ``migrate_import._import_table`` and ``objective_reconcile.run_reconcile``
before the extract-method refactor: return values, rows written to the central
DB and the p4 bookkeeping tables, the warning lines, the abort exception, and
for run_reconcile the order of the re-derive / close calls, their evidence and
the summary. Real temp dirs and SQLite files; gh and the per-track library
calls whose order is pinned are spied.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
for _p in (_TESTS, _ROOT, _ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts import migrate_to_central_vnx as M
from scripts.aggregator.build_central_view import ProjectEntry, attach_readonly
from scripts.lib import migrate_import as MI

import objective_reconcile
import track_reconciler
from test_objective_reconcile import PROJECT_ID, _build_db, _make_gh_mock


def _central(tmp_path: Path, src_sql: str, central_sql: str) -> sqlite3.Connection:
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.executescript(src_sql)
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "central.db", isolation_level=None)
    con.executescript(central_sql)
    M._ensure_idempotency_table(con)
    M._ensure_skipped_table(con)
    M._ensure_rowid_map_table(con)
    attach_readonly(con, "src", src)
    return con


def _imp(con, tmp_path, table, run_id=None):
    project = ProjectEntry(name="mc", path=tmp_path / "mc", project_id="mc")
    return MI._import_table(con, "src", project, table, run_id=run_id)


def _rows(con, sql):
    return con.execute(sql).fetchall()


@pytest.fixture(autouse=True)
def _no_abort_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(MI, "ABORT_FLAG", tmp_path / "ABORT")


_DISPATCH_SRC = """
CREATE TABLE dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT, parent_dispatch TEXT,
    source_dispatch_ids TEXT, state TEXT);
INSERT INTO dispatches VALUES (1, 'd-new', 'p-1', '["a","mc:b"]', 'ok');
INSERT INTO dispatches VALUES (2, 'd-done', NULL, NULL, 'ok');
INSERT INTO dispatches VALUES (3, 'd-dup', '', 'not-json', 'ok');
INSERT INTO dispatches VALUES (4, 'd-boom', NULL, '{"k":1}', 'boom');
"""
_DISPATCH_CENTRAL = """
CREATE TABLE dispatches (id INTEGER PRIMARY KEY, dispatch_id TEXT UNIQUE, parent_dispatch TEXT,
    source_dispatch_ids TEXT, state TEXT, project_id TEXT);
INSERT INTO dispatches (dispatch_id, state, project_id) VALUES ('mc:d-dup', 'pre', 'mc');
CREATE TRIGGER boom BEFORE INSERT ON dispatches WHEN NEW.state = 'boom'
BEGIN SELECT RAISE(ABORT, 'boom row'); END;
"""


def test_import_table_mixed_rows_writes_and_logs(tmp_path, caplog):
    con = _central(tmp_path, _DISPATCH_SRC, _DISPATCH_CENTRAL)
    con.execute("INSERT INTO p4_import_idempotency (project_id, source_table, source_rowid) "
                "VALUES ('mc', 'dispatches', 2)")
    con.execute("INSERT INTO p4_import_skipped (project_id, source_table, source_rowid, reason) "
                "VALUES ('mc', 'dispatches', 1, 'old')")
    caplog.set_level(logging.WARNING, logger="vnx.migrate.apply")
    assert _imp(con, tmp_path, "dispatches", "R1") == MI.ImportSummary(
        "mc", "", "dispatches", 1, 3)
    assert _rows(con, "SELECT id, dispatch_id, parent_dispatch, source_dispatch_ids, state, "
                      "project_id FROM dispatches ORDER BY id") == [
        (1, "mc:d-dup", None, None, "pre", "mc"),
        (2, "mc:d-new", "mc:p-1", '["mc:a", "mc:b"]', "ok", "mc"),
    ]
    assert _rows(con, "SELECT source_rowid FROM p4_import_idempotency "
                      "ORDER BY source_rowid") == [(1,), (2,)]
    assert _rows(con, "SELECT source_rowid, reason, run_id, resolved_at IS NULL "
                      "FROM p4_import_skipped ORDER BY source_rowid") == [
        (1, "old", None, 0),
        (3, "insert_or_ignore_conflict", "R1", 1),
        (4, "integrity_error:boom row", "R1", 1),
    ]
    assert _rows(con, "SELECT COUNT(*) FROM p4_import_rowid_map") == [(0,)]
    assert [r.getMessage() for r in caplog.records] == [
        "INSERT IGNORED project=mc table=dispatches rowid=3 (central key conflict)",
        "INSERT skipped project=mc table=dispatches rowid=4 err=boom row",
    ]
    assert _imp(con, tmp_path, "dispatches", "R2") == MI.ImportSummary(
        "mc", "", "dispatches", 0, 4)
    assert _rows(con, "SELECT source_rowid, run_id FROM p4_import_skipped "
                      "WHERE run_id IS NOT NULL ORDER BY source_rowid") == [(3, "R2"), (4, "R2")]


def test_import_table_entity_prefix_only_for_dispatch_and_pattern(tmp_path):
    ddl = ("CREATE TABLE coordination_events (id INTEGER PRIMARY KEY, entity_type TEXT, "
           "entity_id TEXT, project_id TEXT);")
    con = _central(tmp_path, ddl + """
    INSERT INTO coordination_events VALUES (1, 'Dispatch', 'x', 'other');
    INSERT INTO coordination_events VALUES (2, 'pattern', 'y', 'other');
    INSERT INTO coordination_events VALUES (3, 'track', 'z', 'other');
    INSERT INTO coordination_events VALUES (4, NULL, 'w', 'other');
    """, ddl)
    assert _imp(con, tmp_path, "coordination_events") == MI.ImportSummary(
        "mc", "", "coordination_events", 4, 0)
    assert _rows(con, "SELECT entity_type, entity_id, project_id FROM coordination_events "
                      "ORDER BY id") == [
        ("Dispatch", "mc:x", "mc"), ("pattern", "mc:y", "mc"),
        ("track", "z", "mc"), (None, "w", "mc"),
    ]


def test_import_table_snippet_rowid_map_and_missing_map(tmp_path, caplog):
    ddl = ("CREATE TABLE code_snippets (body TEXT); "
           "CREATE TABLE snippet_metadata (snippet_rowid INTEGER, note TEXT);")
    con = _central(tmp_path, ddl + """
    INSERT INTO code_snippets (rowid, body) VALUES (7, 'a');
    INSERT INTO code_snippets (rowid, body) VALUES (9, 'b');
    INSERT INTO snippet_metadata VALUES (9, 'keep');
    INSERT INTO snippet_metadata VALUES (42, 'orphan');
    """, ddl + "INSERT INTO code_snippets (rowid, body) VALUES (1, 'existing');")
    caplog.set_level(logging.WARNING, logger="vnx.migrate.apply")
    assert _imp(con, tmp_path, "code_snippets") == MI.ImportSummary(
        "mc", "", "code_snippets", 2, 0)
    assert _rows(con, "SELECT source_table, source_rowid, central_rowid FROM "
                      "p4_import_rowid_map ORDER BY source_rowid") == [
        ("code_snippets", 7, 2), ("code_snippets", 9, 3)]
    assert _imp(con, tmp_path, "snippet_metadata", "R") == MI.ImportSummary(
        "mc", "", "snippet_metadata", 1, 1)
    assert _rows(con, "SELECT snippet_rowid, note FROM snippet_metadata") == [(3, "keep")]
    assert _rows(con, "SELECT source_table, source_rowid, reason, run_id FROM "
                      "p4_import_skipped") == [
        ("snippet_metadata", 2, "missing_code_snippet_rowid_map", "R")]
    assert [r.getMessage() for r in caplog.records] == [
        "INSERT skipped project=mc table=snippet_metadata rowid=2 "
        "err=missing_code_snippet_rowid_map"]


@pytest.mark.parametrize("src, central, summary, rows", [
    ("CREATE TABLE t (a TEXT); INSERT INTO t VALUES ('x');", "CREATE TABLE t (b TEXT);",
     (0, 0), []),
    ("CREATE TABLE t (a TEXT, project_id TEXT); INSERT INTO t VALUES ('x', 'src');",
     "CREATE TABLE t (a TEXT, project_id TEXT);", (1, 0), [("x", "mc")]),
])
def test_import_table_column_edges(tmp_path, src, central, summary, rows):
    con = _central(tmp_path, src, central)
    assert _imp(con, tmp_path, "t") == MI.ImportSummary("mc", "", "t", *summary)
    assert _rows(con, "SELECT * FROM t") == rows


def test_import_table_abort_flag_raises_before_any_write(tmp_path):
    con = _central(tmp_path, "CREATE TABLE t (a TEXT); INSERT INTO t VALUES ('x');",
                   "CREATE TABLE t (a TEXT);")
    MI.ABORT_FLAG.write_text("stop")
    with pytest.raises(MI.AbortRequested, match="abort flag present"):
        _imp(con, tmp_path, "t")
    assert _rows(con, "SELECT COUNT(*) FROM t") == [(0,)]
    assert _rows(con, "SELECT COUNT(*) FROM p4_import_idempotency") == [(0,)]


# ---------------------------------------------------------------------------
# run_reconcile — steps 4c / 5 / 6
# ---------------------------------------------------------------------------

_MERGED = {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}


def _wire(monkeypatch, close_actions):
    calls = []
    confirmed = [
        {"track_id": "T-a", "pr_ref": "#1,#2", "pr_results": [
            {"number": 1, **_MERGED}, {"number": 2, "state": "OPEN", "mergedAt": ""},
            {"number": "x", **_MERGED}, "not-a-dict"]},
        {"track_id": "T-b", "pr_ref": "#3", "pr_results": [{"number": "3", **_MERGED}, _MERGED]},
    ]
    per_track = [{"track_id": "T-a"}, {"track_id": "T-b"}]
    counts = {"closed": 0, "stale": 0, "unverified": 0}

    def fake_verify(*a, **k):
        calls.append(("verify",))
        return {"per_track": per_track, "counts": counts, "confirmed_candidates": confirmed}

    def fake_merge(gh_set, state_dir, repo_root):
        calls.append(("merge", sorted(gh_set)))
        return frozenset(gh_set | {99})

    def fake_reconcile(state_dir, track_id, project_id, **kw):
        calls.append(("reconcile", track_id, sorted(kw["_merged_pr_numbers"])))
        return {}

    def fake_close(state_dir, track_id, project_id, **kw):
        ev = kw["evidence"]
        calls.append(("close", track_id, kw["actor"], kw["approval_id"].startswith(
            "auto-reconcile-"), sorted(kw["merged_pr_numbers"]), ev["pr_ref"],
            ev["verified_at"], ev["allow_closed_siblings"], sorted(ev)))
        return close_actions[track_id]

    clock = iter(f"T{i:02d}" for i in range(1, 50))
    monkeypatch.setattr(objective_reconcile, "_now_utc", lambda: next(clock))
    monkeypatch.setattr(objective_reconcile, "verify_candidates", fake_verify)
    monkeypatch.setattr(objective_reconcile, "nominate_candidates", lambda *a, **k: (
        [], [{"track_id": "T-g"}], [{"track_id": "T-h"}]))
    monkeypatch.setattr(objective_reconcile, "merge_evidence_pr_numbers", fake_merge)
    monkeypatch.setattr(track_reconciler, "reconcile_track", fake_reconcile)
    monkeypatch.setattr(track_reconciler, "close_track_if_done", fake_close)
    monkeypatch.setattr(objective_reconcile.subprocess, "run", _make_gh_mock({}))
    return calls, counts


def test_run_reconcile_apply_order_evidence_and_counts(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    calls, _ = _wire(monkeypatch, {"T-a": {"action": "closed"},
                                   "T-b": {"action": "stale_candidate"}})
    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True, allow_closed_siblings=True)
    assert code == 0
    keys = ["allow_closed_siblings", "pr_ref", "pr_results", "verified_at"]
    assert calls == [
        ("verify",), ("merge", [1, 3]),
        ("reconcile", "T-a", [1, 3, 99]), ("reconcile", "T-b", [1, 3, 99]),
        ("close", "T-a", "system", True, [1, 3, 99], "#1,#2", "T02", True, keys),
        ("close", "T-b", "system", True, [1, 3, 99], "#3", "T02", True, keys),
    ]
    assert summary["counts"] == {"closed": 1, "stale": 1, "unverified": 0}
    assert (summary["mode"], summary["started_at"], summary["finished_at"]) == (
        "apply", "T01", "T03")
    assert [(p["track_id"], p.get("close_result")) for p in summary["per_track"]] == [
        ("T-a", "closed"), ("T-b", "stale_candidate"), ("T-g", None), ("T-h", None)]


def test_run_reconcile_apply_other_actions_count_nothing(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _wire(monkeypatch, {"T-a": {}, "T-b": {"action": "noop_not_terminal"}})
    summary, code = objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path,
                                                      apply=True)
    assert code == 0
    assert summary["counts"] == {"closed": 0, "stale": 0, "unverified": 0}
    assert [p.get("close_result") for p in summary["per_track"]] == [
        "", "noop_not_terminal", None, None]


@pytest.mark.parametrize("unverified, exit_code", [(0, 0), (1, 3)])
def test_run_reconcile_check_mode_never_closes(tmp_path, monkeypatch, unverified, exit_code):
    sd = _build_db(tmp_path)
    calls, counts = _wire(monkeypatch, {})
    counts["unverified"] = unverified
    summary, code = objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path)
    assert code == exit_code
    assert [c[0] for c in calls] == ["verify", "merge", "reconcile", "reconcile"]
    assert (summary["mode"], summary["started_at"], summary["finished_at"]) == (
        "check", "T01", "T02")
    persisted = json.loads((sd / objective_reconcile._SUMMARY_FILE).read_text())
    assert (persisted["run_id"], persisted["finished_at"]) == (summary["run_id"], "T02")
