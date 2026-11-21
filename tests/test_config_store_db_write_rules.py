#!/usr/bin/env python3
"""ADR-087 decision 8 (config-een-oppervlak G3): scope/enum refusal, unset_config, audit migration.

Dispatch-ID: dlv-bde7a2e6d4e9
"""

import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import api_config as ac  # noqa: E402
import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402

PID = "vnx-dev"
GLOBAL_KEY = "VNX_TEST_G3_GLOBAL"
ENUM_KEY = "VNX_TEST_G3_ENUM"
FREE_KEY = "VNX_TEST_G3_FREE"
APPROVAL_KEY = "VNX_TEST_G3_APPROVAL"

# project_config_audit as created on origin/main b0c92f1c (before G3).
_OLD_AUDIT = (
    "CREATE TABLE project_config_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,"
    " config_key TEXT NOT NULL, old_value TEXT, new_value TEXT NOT NULL, changed_by TEXT NOT NULL,"
    " changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), approval_id TEXT,"
    " event_id TEXT NOT NULL, UNIQUE (project_id, event_id));")


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    def put(key, **kw):
        monkeypatch.setitem(cr.CONFIG_REGISTRY, key, cr._e(key, kw.pop("type_", "string"), "", "gate", "g3", **kw))
    put(GLOBAL_KEY, scope="global")
    put(ENUM_KEY, type_="enum", allowed_values=("shadow", "enforced"))
    put(FREE_KEY)
    put(APPROVAL_KEY, approval=True)


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / "runtime_coordination.db")) as c:
        yield c


def _rows(conn, table):
    cs.ensure_config_tables(conn)
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def _audit(conn):
    cols = "config_key, old_value, new_value, operation, reason, approval_id"
    return conn.execute(f"SELECT {cols} FROM project_config_audit ORDER BY id").fetchall()


def test_global_key_refused_and_nothing_written(conn):
    with pytest.raises(PermissionError, match=f"{GLOBAL_KEY} is a global key.*--global"):
        cs.set_config(conn, PID, GLOBAL_KEY, "x", actor="op")
    assert _rows(conn, "project_config") == [] and _rows(conn, "project_config_audit") == []


def test_allowed_values_exact_match(conn):
    for bad in ("loose", "Enforced", " enforced"):
        with pytest.raises(ValueError, match=f"{ENUM_KEY}.*{bad!r}.*shadow"):
            cs.set_config(conn, PID, ENUM_KEY, bad, actor="op")
    assert _rows(conn, "project_config") == [] and _rows(conn, "project_config_audit") == []
    cs.set_config(conn, PID, ENUM_KEY, "enforced", actor="op")
    cs.set_config(conn, PID, FREE_KEY, "Whatever 123", actor="op")  # no allowed_values: as today
    assert cs.read_config(conn, PID, ENUM_KEY) == "enforced"
    assert cs.read_config(conn, PID, FREE_KEY) == "Whatever 123"


def test_dashboard_maps_global_to_403_and_enum_to_400(tmp_path):
    db_path = tmp_path / "runtime_coordination.db"
    sqlite3.connect(db_path).close()
    ac._wire_resolver(tmp_path)
    try:
        res, status = ac.operator_post_config_set({"key": GLOBAL_KEY, "value": "x"}, project_id=PID, db_path=db_path)
        assert status == 403 and "global key" in res["message"]
        res, status = ac.operator_post_config_set({"key": ENUM_KEY, "value": "loose"}, project_id=PID, db_path=db_path)
        assert status == 400 and "loose" in res["message"]
    finally:
        cr.set_db_resolver(None)
    with closing(sqlite3.connect(db_path)) as c:
        assert _rows(c, "project_config") == [] and _rows(c, "project_config_audit") == []


def test_set_reason_is_stored_and_empty_value_is_a_set(conn):
    cs.set_config(conn, PID, FREE_KEY, "a", actor="op", reason="because")
    cs.set_config(conn, PID, FREE_KEY, "", actor="op")
    assert _audit(conn) == [(FREE_KEY, None, "a", "set", "because", None), (FREE_KEY, "a", "", "set", None, None)]
    assert cs.read_config(conn, PID, FREE_KEY) == ""


def test_unset_removes_row_with_one_audit_row(conn):
    cs.set_config(conn, PID, FREE_KEY, "a", actor="op")
    res = cs.unset_config(conn, PID, FREE_KEY, actor="op", reason="cleanup")
    assert (res["removed"], res["operation"], res["old_value"], res["new_value"]) == (True, "unset", "a", "")
    assert cs.read_config(conn, PID, FREE_KEY) is None
    assert [r for r in _audit(conn) if r[3] == "unset"] == [(FREE_KEY, "a", "", "unset", "cleanup", None)]


def test_unset_approval_key_without_approval_refused(conn):
    cs.set_config(conn, PID, APPROVAL_KEY, "a", actor="op", approval_id="appr-1")
    with pytest.raises(PermissionError, match="approval_id"):
        cs.unset_config(conn, PID, APPROVAL_KEY, actor="op")
    assert cs.read_config(conn, PID, APPROVAL_KEY) == "a"
    assert len(_audit(conn)) == 1
    res = cs.unset_config(conn, PID, APPROVAL_KEY, actor="op", approval_id="appr-2")
    assert res["removed"] is True and _audit(conn)[-1][5] == "appr-2"


def test_unset_unknown_key_refused_and_no_row_writes_nothing(conn):
    with pytest.raises(ValueError, match="unknown config key"):
        cs.unset_config(conn, PID, "VNX_TEST_G3_NOPE", actor="op")
    res = cs.unset_config(conn, PID, FREE_KEY, actor="op")
    assert res["removed"] is False and res["old_value"] is None
    assert _rows(conn, "project_config_audit") == []


def test_unset_global_key_removes_stale_project_row(conn):
    cs.ensure_config_tables(conn)
    conn.execute(
        "INSERT INTO project_config (project_id, config_key, config_value, updated_by) VALUES (?, ?, 'old', 'op')",
        (PID, GLOBAL_KEY))
    conn.commit()
    res = cs.unset_config(conn, PID, GLOBAL_KEY, actor="op")
    assert res["removed"] is True and cs.read_config(conn, PID, GLOBAL_KEY) is None
    assert _audit(conn) == [(GLOBAL_KEY, "old", "", "unset", None, None)]


def test_unset_audit_failure_keeps_the_row(conn, monkeypatch):
    first = cs.set_config(conn, PID, FREE_KEY, "a", actor="op")
    monkeypatch.setattr(cs.uuid, "uuid4", lambda: uuid.UUID(first["event_id"]))
    with pytest.raises(sqlite3.IntegrityError):
        cs.unset_config(conn, PID, FREE_KEY, actor="op")
    assert cs.read_config(conn, PID, FREE_KEY) == "a"
    assert len(_audit(conn)) == 1


def _shape(conn):
    cols = [tuple(r) for r in conn.execute("PRAGMA table_info(project_config_audit)")]
    idx = {r[1]: r[2] for r in conn.execute("PRAGMA index_list(project_config_audit)")}
    return cols, idx


def test_old_database_gets_both_columns_and_keeps_its_rows(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(_OLD_AUDIT)
    old.execute(
        "INSERT INTO project_config_audit (project_id, config_key, old_value, new_value, changed_by, event_id) "
        "VALUES ('p', 'K', 'x', 'y', 'op', 'evt-old')")
    old.commit()
    before = old.execute("SELECT * FROM project_config_audit").fetchall()
    cs.ensure_config_tables(old)
    assert old.execute("SELECT * FROM project_config_audit").fetchall() == [before[0] + ("set", None)]
    migrated = _shape(old)
    cs.ensure_config_tables(old)  # second call is a no-op
    assert _shape(old) == migrated

    fresh = sqlite3.connect(tmp_path / "fresh.db")
    cs.ensure_config_tables(fresh)
    assert _shape(fresh) == migrated
    pk = [r[1] for r in fresh.execute("PRAGMA table_info(project_config)") if r[5] > 0]
    assert pk == ["project_id", "config_key"]
    unique = [name for name, is_unique in migrated[1].items() if is_unique]
    assert [[r[2] for r in old.execute(f"PRAGMA index_info({u})")] for u in unique] == [["project_id", "event_id"]]
    old.close()
    fresh.close()


def test_duplicate_column_from_a_concurrent_opener_passes_any_other_error_propagates(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "race.db")
    c.executescript(_OLD_AUDIT)
    c.execute("ALTER TABLE project_config_audit ADD COLUMN operation TEXT NOT NULL DEFAULT 'set'")
    real, calls = cs._audit_columns, []

    def stale_first(conn):  # the first look misses the column the other opener just added
        calls.append(1)
        return real(conn) - {"operation"} if len(calls) == 1 else real(conn)

    monkeypatch.setattr(cs, "_audit_columns", stale_first)
    cs.ensure_config_tables(c)
    assert {"operation", "reason"} <= real(c)
    monkeypatch.setattr(cs, "_AUDIT_ADDED_COLUMNS", (("x", "ALTER TABLE no_such_table ADD COLUMN x TEXT"),))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        cs.ensure_config_tables(c)
    c.close()
