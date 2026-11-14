#!/usr/bin/env python3
"""Golf schoon R24: characterisation tests for two class C functions.

Pins the observable behaviour of ``verify_dispatch_provenance`` and
``_rebuild_table_phase1`` before and after their extract-method refactor:
return values, findings and advisories in order, the order of the lookups and
SQL statements, the rebuilt schema and rows, and the exact exception text.

Only the git subprocess (``find_commits_by_dispatch``) is replaced; the rest
runs on real temp dirs and SQLite files.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import provenance_verification as pv  # noqa: E402
import tenant_stamping as ts  # noqa: E402
from receipt_provenance import register_provenance_link  # noqa: E402
from runtime_coordination import get_connection, init_schema  # noqa: E402

_F = ("finding_type", "severity", "entity_type", "entity_id", "description", "layer")
_A = ("advisory_type", "severity", "entity_type", "entity_id", "recommendation")


@pytest.fixture
def prov_conn(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    init_schema(state)
    with get_connection(state) as conn:
        yield conn


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _spy_lookups(monkeypatch, commits: list[str]) -> list[str]:
    calls: list[str] = []
    real_link, real_receipts = pv.get_provenance_link, pv.find_receipts_by_dispatch

    def link(conn, dispatch_id):
        calls.append("registry")
        return real_link(conn, dispatch_id)

    def receipts(path, dispatch_id):
        calls.append("receipts")
        return real_receipts(path, dispatch_id)

    def git(dispatch_id, repo_root=None):
        calls.append(f"git:{repo_root}")
        return list(commits)

    monkeypatch.setattr(pv, "get_provenance_link", link)
    monkeypatch.setattr(pv, "find_receipts_by_dispatch", receipts)
    monkeypatch.setattr(pv, "find_commits_by_dispatch", git)
    return calls


def _rows(items: list[dict], keys: tuple) -> list[tuple]:
    return [tuple(item[k] for k in keys) for item in items]


def test_provenance_nothing_known(prov_conn, tmp_path, monkeypatch):
    calls = _spy_lookups(monkeypatch, [])
    data = pv.verify_dispatch_provenance(prov_conn, "d-a", tmp_path / "none.ndjson").to_dict()

    assert calls == ["registry", "receipts", "git:None"]
    assert _rows(data["findings"], _F) == [
        ("missing_dispatch_id", "warning", "provenance", "d-a",
         "Dispatch d-a not found in provenance registry", "registry"),
        ("missing_receipt", "warning", "dispatch", "d-a", "No receipts found for dispatch d-a", "receipt"),
    ]
    assert _rows(data["advisories"], _A) == [
        ("register_provenance", "warning", "provenance", "d-a", "Register provenance link for dispatch d-a"),
    ]
    assert data["advisories"][0]["evidence"] == {}
    assert (data["verdict"], data["chain_status"], data["registry_link"]) == ("warning", "incomplete", None)
    assert (data["receipt_count"], data["commit_count"]) == (0, 0)


def test_provenance_broken_chain_on_both_registry_keys(prov_conn, tmp_path, monkeypatch):
    register_provenance_link(prov_conn, dispatch_id="d-b", receipt_id="run-other", commit_sha="sha-reg")
    prov_conn.commit()
    receipts = tmp_path / "r.ndjson"
    _write_ndjson(receipts, [
        {"dispatch_id": "d-b", "event_type": "task_complete", "task_id": "TASK-b",
         "terminal": "T1", "status": "success", "timestamp": "2026-03-29T18:30:00Z"},
        {"dispatch_id": "d-b", "event_type": "task_complete", "run_id": "run-b2",
         "terminal": "T1", "status": "success", "timestamp": "2026-03-29T18:31:00Z",
         "trace_token": "Dispatch-ID: d-b", "provenance": {"git_ref": "abc", "branch": "x"}},
    ])
    calls = _spy_lookups(monkeypatch, ["sha-1"])
    data = pv.verify_dispatch_provenance(prov_conn, "d-b", receipts, tmp_path).to_dict()

    assert calls == ["registry", "receipts", f"git:{tmp_path}"]
    assert (data["verdict"], data["chain_status"]) == ("fail", "broken")
    assert (data["receipt_count"], data["commit_count"]) == (2, 1)
    assert (data["registry_link"]["receipt_id"], data["registry_link"]["commit_sha"]) == ("run-other", "sha-reg")
    assert _rows(data["findings"], _F) == [
        ("missing_git_ref", "warning", "receipt", "TASK-b", "Receipt has no provenance block", "receipt"),
        ("broken_chain", "error", "provenance", "d-b",
         "Registry receipt_id 'run-other' not found in actual receipts for dispatch d-b", "registry"),
        ("broken_chain", "error", "provenance", "d-b",
         "Registry commit_sha 'sha-reg' not found in commits with trace token for dispatch d-b", "registry"),
    ]
    assert _rows(data["advisories"], _A) == [
        ("add_trace_token", "info", "receipt", "TASK-b",
         "Receipt missing trace_token — enrich for full traceability"),
    ]


def test_provenance_receipts_without_commits(prov_conn, tmp_path, monkeypatch):
    register_provenance_link(prov_conn, dispatch_id="d-c", receipt_id="run-c")
    prov_conn.commit()
    receipts = tmp_path / "r.ndjson"
    prov = {"git_ref": "abc", "branch": "x"}
    _write_ndjson(receipts, [
        {"dispatch_id": "d-c", "event_type": "task_complete", "run_id": "run-c", "terminal": "T2",
         "status": "success", "timestamp": "2026-03-29T18:30:00Z", "provenance": prov},
        {"dispatch_id": "d-c", "event_type": "task_started", "terminal": "T2",
         "status": "working", "timestamp": "2026-03-29T18:20:00Z", "provenance": prov},
    ])
    _spy_lookups(monkeypatch, [])
    data = pv.verify_dispatch_provenance(prov_conn, "d-c", receipts).to_dict()

    assert (data["verdict"], data["chain_status"]) == ("warning", "incomplete")
    assert _rows(data["findings"], _F)[-1] == (
        "missing_trace_token", "info", "dispatch", "d-c",
        "No commits found with trace token for dispatch d-c", "git",
    )
    assert not any(f["finding_type"] == "broken_chain" for f in data["findings"])
    assert _rows(data["advisories"], _A) == [
        ("add_trace_token", "info", "receipt", "run-c", "Receipt missing trace_token — enrich for full traceability"),
        ("add_trace_token", "info", "receipt", "d-c", "Receipt missing trace_token — enrich for full traceability"),
        ("link_commit", "info", "dispatch", "d-c", "Add Dispatch-ID trace token to commit messages for this dispatch"),
    ]


def _tenant_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE parent (key TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT 'n',
            project_id TEXT NOT NULL DEFAULT 'p', tag TEXT, UNIQUE(name), UNIQUE(tag, project_id));
        CREATE TABLE child (id INTEGER PRIMARY KEY,
            parent_key TEXT REFERENCES parent(key) ON DELETE CASCADE,
            other TEXT REFERENCES other(oid), project_id TEXT);
        CREATE TABLE other (oid TEXT, project_id TEXT, PRIMARY KEY (oid, project_id));
        CREATE INDEX idx_child_other ON child(other) WHERE other IS NOT NULL;
        CREATE VIEW v_child AS SELECT id, parent_key FROM child;
        CREATE TABLE audit (n TEXT);
        CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN INSERT INTO audit(n) VALUES (new.parent_key); END;
        INSERT INTO parent VALUES ('k1','a','p','t1'),('k2','b','p',NULL);
        INSERT INTO child(parent_key, other, project_id) VALUES ('k1', NULL, 'p'), ('k2', 'o', 'p');
        """
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


_EXPECTED_TRACE = [
    'PRAGMA table_info(parent)',
    'PRAGMA index_list(parent)',
    'PRAGMA index_info(sqlite_autoindex_parent_3)',
    'PRAGMA index_info(sqlite_autoindex_parent_2)',
    'PRAGMA foreign_key_list(parent)',
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='parent' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='parent' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name",
    'DROP VIEW IF EXISTS "v_child"',
    'DROP TABLE IF EXISTS "parent_w1_p1"',
    'CREATE TABLE "parent_w1_p1" (\n  "key" TEXT,\n  "name" TEXT NOT NULL DEFAULT (\'n\'),\n  "project_id" TEXT,\n  "tag" TEXT,\n  PRIMARY KEY ("key", "project_id"),\n  UNIQUE ("tag", "project_id"),\n  UNIQUE ("name", "project_id")\n)',
    'SELECT COUNT(*) FROM "parent"',
    'INSERT OR IGNORE INTO "parent_w1_p1" ("key", "name", "project_id", "tag") SELECT "key", "name", "project_id", "tag" FROM "parent"',
    'SELECT COUNT(*) FROM "parent_w1_p1"',
    'DROP TABLE "parent"',
    'ALTER TABLE "parent_w1_p1" RENAME TO "parent"',
    'CREATE VIEW v_child AS SELECT id, parent_key FROM child',
    'PRAGMA table_info(child)',
    'PRAGMA index_list(child)',
    'PRAGMA foreign_key_list(child)',
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='child' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='child' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name",
    'DROP VIEW IF EXISTS "v_child"',
    'DROP TABLE IF EXISTS "child_w1_p1"',
    'CREATE TABLE "child_w1_p1" (\n  "id" INTEGER PRIMARY KEY AUTOINCREMENT,\n  "parent_key" TEXT,\n  "other" TEXT,\n  "project_id" TEXT,\n  FOREIGN KEY ("other") REFERENCES "other" ("oid") ON UPDATE NO ACTION ON DELETE NO ACTION,\n  FOREIGN KEY ("parent_key", "project_id") REFERENCES "parent" ("key", "project_id") ON UPDATE NO ACTION ON DELETE CASCADE\n)',
    'SELECT COUNT(*) FROM "child"',
    'INSERT OR IGNORE INTO "child_w1_p1" ("id", "parent_key", "other", "project_id") SELECT "id", "parent_key", "other", "project_id" FROM "child"',
    'SELECT COUNT(*) FROM "child_w1_p1"',
    'DROP TABLE "child"',
    'ALTER TABLE "child_w1_p1" RENAME TO "child"',
    'CREATE VIEW v_child AS SELECT id, parent_key FROM child',
    'CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN INSERT INTO audit(n) VALUES (new.parent_key); END',
    'CREATE INDEX idx_child_other ON child(other) WHERE other IS NOT NULL',
    'PRAGMA table_info(other)',
    'PRAGMA index_list(other)',
    'PRAGMA foreign_key_list(other)',
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='other' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='other' AND sql IS NOT NULL ORDER BY name",
    "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name",
    'DROP TABLE IF EXISTS "other_w1_p1"',
    'CREATE TABLE "other_w1_p1" (\n  "oid" TEXT,\n  "project_id" TEXT,\n  PRIMARY KEY ("oid", "project_id")\n)',
    'SELECT COUNT(*) FROM "other"',
    'INSERT OR IGNORE INTO "other_w1_p1" ("oid", "project_id") SELECT "oid", "project_id" FROM "other"',
    'SELECT COUNT(*) FROM "other_w1_p1"',
    'DROP TABLE "other"',
    'ALTER TABLE "other_w1_p1" RENAME TO "other"',
]



def test_rebuild_phase1_statement_order_schema_and_rows(tmp_path):
    conn = _tenant_db(tmp_path / "t.db")
    trace: list[str] = []
    conn.set_trace_callback(trace.append)
    widened = ts._rebuild_table_phase1(conn, "parent", [])
    assert ts._rebuild_table_phase1(conn, "child", [], set(widened)) == []
    assert ts._rebuild_table_phase1(conn, "other", []) == []
    conn.set_trace_callback(None)

    assert widened == [("parent", "key"), ("parent", "name")]
    assert [t for t in trace if not t.startswith("BEGIN")] == _EXPECTED_TRACE
    sql_by_name = dict(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'"
    ).fetchall())
    assert sql_by_name["parent"] == _EXPECTED_TRACE[10].replace('"parent_w1_p1"', '"parent"')
    assert sql_by_name["child"] == _EXPECTED_TRACE[25].replace('"child_w1_p1"', '"child"')
    assert {"parent_w1_p1", "child_w1_p1", "other_w1_p1"}.isdisjoint(sql_by_name)
    assert {"child_ai", "idx_child_other", "v_child"} <= set(sql_by_name)
    assert conn.execute("SELECT * FROM parent ORDER BY key").fetchall() == [
        ("k1", "a", "p", "t1"), ("k2", "b", "p", None),
    ]
    assert conn.execute("SELECT * FROM child ORDER BY id").fetchall() == [
        (1, "k1", None, "p"), (2, "k2", "o", "p"),
    ]
    conn.close()


def test_rebuild_phase1_widened_keys_default_none_leaves_fk_single(tmp_path):
    conn = _tenant_db(tmp_path / "t.db")
    assert ts._rebuild_table_phase1(conn, "child", []) == []
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='child'").fetchone()[0]
    assert 'FOREIGN KEY ("parent_key") REFERENCES "parent" ("key") ON UPDATE NO ACTION ON DELETE CASCADE' in sql
    conn.close()


def test_rebuild_phase1_row_copy_collision_raises_and_keeps_source(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "c.db"))
    conn.executescript("CREATE TABLE t (name UNIQUE, project_id TEXT); INSERT INTO t VALUES (1, 'p'), ('1', 'p');")
    with pytest.raises(RuntimeError) as exc:
        ts._rebuild_table_phase1(conn, "t", [])
    assert type(exc.value) is RuntimeError
    assert str(exc.value) == (
        "Phase 1 row-copy mismatch for table 't': 2 source rows but only 1 copied. "
        "The widened composite key produced collisions — this indicates "
        "duplicate rows that would silently be dropped. "
        "Investigate the data before rerunning W1. ROLLBACK."
    )
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"t", "t_w1_p1"}
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    conn.close()
