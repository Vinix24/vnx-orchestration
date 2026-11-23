#!/usr/bin/env python3
"""Characterisation of ``upsert_dispatch_provider_row`` (golf schoon R32b).

Pinned on the old code before the extract-method cut: required-arg
ValueErrors, missing DB, insert then fill-once, role normalisation, the
tenant-scoped UPDATE, the legacy store, the deadline bail per stage, the
sqlite-error warning and the exact SQL statement sequence with bound values.
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO / "scripts" / "lib", REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_r32b_dispatch_metadata_db", REPO / "scripts" / "lib" / "dispatch_metadata_db.py")
dmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dmd)
LOGGER = dmd.logger.name
COLS = ("dispatch_id, project_id, terminal, track, provider, model, role, gate, pr_id, "
        "parent_dispatch, session_id, outcome_status, outcome_report_path, difficulty_json")
BASE = dict(dispatch_id="dlv-c1", terminal="T1", provider="claude")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("VNX_PROJECT_ID", "VNX_CANONICAL_ROOT", "VNX_DATA_DIR", "VNX_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)


def _qi_db(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    db = state_dir / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db, REPO / "schemas" / "quality_intelligence.sql")
    return db


def _rows(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT {COLS} FROM dispatch_metadata ORDER BY id")]
    finally:
        conn.close()


def _traced(monkeypatch) -> list:
    """Record every SQL statement the writer sends, with bound values expanded."""
    statements: list = []
    real_connect = sqlite3.connect

    def _connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(dmd.sqlite3, "connect", _connect)
    return statements


@pytest.mark.parametrize("field", ["dispatch_id", "terminal", "provider"])
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_required_args_raise_value_error(tmp_path, field, bad):
    with pytest.raises(ValueError) as exc:
        dmd.upsert_dispatch_provider_row(tmp_path / "x.db", **dict(BASE, **{field: bad}))
    assert str(exc.value) == f"upsert_dispatch_provider_row: {field} is required"


def test_missing_db_returns_false_and_creates_nothing(tmp_path, caplog):
    db = tmp_path / "absent" / "quality_intelligence.db"
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert dmd.upsert_dispatch_provider_row(db, **BASE) is False
    assert not db.exists()
    assert f"upsert_dispatch_provider_row: DB not found at {db} — skipping" in caplog.messages


def test_insert_then_fill_once(tmp_path, caplog):
    db = _qi_db(tmp_path)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    one = dict(dispatch_id="dlv-c1", terminal="T2", provider="codex", model="m1", track="B",
               role="architect", gate="g1", pr_id="#1", session_id="s1",
               parent_dispatch="dlv-p1", difficulty_json='{"d":1}')
    assert dmd.upsert_dispatch_provider_row(db, **one) is True
    assert "upsert_dispatch_provider_row: stamped dispatch=dlv-c1 provider=codex model=m1 " \
           "outcome=None" in caplog.messages
    first = dict(one, project_id="vnx-dev", outcome_status=None, outcome_report_path=None)
    assert _rows(db) == [first]
    two = dict(one, terminal="T3", provider="kimi", model="m2", track="C", role="reviewer",
               gate="g2", pr_id="#2", session_id="s2", parent_dispatch="dlv-p2",
               difficulty_json='{"d":2}', outcome_status="success", report_path="/r/1.md")
    assert dmd.upsert_dispatch_provider_row(db, **two) is True
    assert _rows(db) == [dict(first, provider="kimi", outcome_status="success",
                              outcome_report_path="/r/1.md")]
    assert dmd.upsert_dispatch_provider_row(db, **BASE, model="", gate="", role="",
                                            outcome_status="failure", report_path="/r/2.md")
    assert _rows(db) == [dict(first, provider="claude", outcome_status="success",
                              outcome_report_path="/r/1.md")]


@pytest.mark.parametrize("raw, stored", [
    ("identity_unresolved", None), ("", None), (None, None), ("  architect  ", "architect"),
    ("backend-developer", "backend-developer")])
def test_role_normalisation(tmp_path, raw, stored):
    db = _qi_db(tmp_path)
    assert dmd.upsert_dispatch_provider_row(db, **BASE, role=raw) is True
    assert _rows(db)[0]["role"] == stored


def test_role_fallback_without_normaliser(tmp_path, monkeypatch):
    db = _qi_db(tmp_path)
    monkeypatch.setattr(dmd, "_normalize_role", None)
    assert dmd.upsert_dispatch_provider_row(db, **BASE, role="identity_unresolved")
    assert _rows(db)[0]["role"] == "identity_unresolved"


def test_update_is_tenant_scoped(tmp_path):
    db = _qi_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track, "
                 "provider) VALUES ('dlv-c1', 'other-tenant', 'T9', 'Z', 'old')")
    conn.commit()
    conn.close()
    assert dmd.upsert_dispatch_provider_row(db, **BASE, model="m1") is True
    rows = {r["project_id"]: (r["provider"], r["model"]) for r in _rows(db)}
    assert rows == {"other-tenant": ("old", None), "vnx-dev": ("claude", "m1")}


def test_legacy_store_statements(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE dispatch_metadata (id INTEGER PRIMARY KEY, dispatch_id TEXT "
                 "UNIQUE, terminal TEXT, track TEXT, role TEXT, gate TEXT, pr_id TEXT, "
                 "dispatched_at TEXT)")
    conn.close()
    statements = _traced(monkeypatch)
    assert dmd.upsert_dispatch_provider_row(db, **BASE, model="m1", role="ar", gate="g",
                                            pr_id="#3", outcome_status="ok", session_id="s")
    sql = [s for s in statements if s.startswith(("INSERT", "UPDATE"))]
    assert sql[0].startswith(
        "INSERT OR IGNORE INTO dispatch_metadata (dispatch_id, terminal, track, role, gate, "
        "pr_id, dispatched_at) VALUES ('dlv-c1', 'T1', 'headless', 'ar', 'g', '#3', '")
    assert sql[1:] == ["UPDATE dispatch_metadata SET role = COALESCE(role, 'ar'), gate = "
                       "COALESCE(gate, 'g'), pr_id = COALESCE(pr_id, '#3') WHERE dispatch_id "
                       "= 'dlv-c1'"]


def test_full_store_statement_sequence(tmp_path, monkeypatch):
    db = _qi_db(tmp_path)
    statements = _traced(monkeypatch)
    assert dmd.upsert_dispatch_provider_row(
        db, **BASE, model="m1", role="ar", gate="g", pr_id="#4", session_id="s1",
        parent_dispatch="dlv-p", difficulty_json="{}", outcome_status="ok", report_path="/r")
    shape = ["REARM" if s.startswith("PRAGMA busy_timeout") else s.split(" VALUES ")[0]
             for s in statements]
    shape[-3] = shape[-3].split("completed_at = COALESCE(completed_at, '")[0] + "<ts>"
    assert shape == [
        "REARM", "PRAGMA table_info(dispatch_metadata)", "REARM", "BEGIN IMMEDIATE", "REARM",
        "INSERT OR IGNORE INTO dispatch_metadata (dispatch_id, terminal, track, role, gate, "
        "pr_id, dispatched_at, provider, model, session_id, parent_dispatch, difficulty_json, "
        "project_id)",
        "REARM",
        "UPDATE dispatch_metadata SET provider = 'claude', model = COALESCE(model, 'm1'), "
        "session_id = COALESCE(session_id, 's1'), parent_dispatch = COALESCE(parent_dispatch, "
        "'dlv-p'), difficulty_json = COALESCE(difficulty_json, '{}'), role = COALESCE(role, "
        "'ar'), gate = COALESCE(gate, 'g'), pr_id = COALESCE(pr_id, '#4'), outcome_status = "
        "COALESCE(outcome_status, 'ok'), outcome_report_path = COALESCE(outcome_report_path, "
        "'/r'), <ts>",
        "REARM", "COMMIT"]
    assert statements[-3].endswith("') WHERE project_id = 'vnx-dev' AND dispatch_id = 'dlv-c1'")


@pytest.mark.parametrize("stage, survive", [
    ("schema probe", 0), ("BEGIN", 1), ("INSERT", 2), ("UPDATE", 3), ("commit", 4)])
def test_deadline_bail_at_each_stage(tmp_path, caplog, monkeypatch, stage, survive):
    db = _qi_db(tmp_path)
    ticks = {"n": 0}
    base = dmd.time.monotonic()

    def _mono():  # call 0 sets the deadline; re-arms 1..survive still see budget
        ticks["n"] += 1
        return base if ticks["n"] - 1 <= survive else base + 1000.0

    monkeypatch.setattr(dmd.time, "monotonic", _mono)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert dmd.upsert_dispatch_provider_row(db, **BASE, timeout=30) is False
    assert (f"upsert_dispatch_provider_row: deadline exhausted before {stage} "
            f"for dispatch=dlv-c1 — skipping") in caplog.messages
    assert _rows(db) == []


def test_sqlite_error_warns_and_returns_false(tmp_path, caplog):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert dmd.upsert_dispatch_provider_row(db, **BASE) is False
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "upsert_dispatch_provider_row: sqlite error for dispatch=dlv-c1 (non-fatal): "
        "no such table: dispatch_metadata"]
