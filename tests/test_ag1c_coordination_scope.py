"""AG-1c: the opened store supplies identity; unresolved hook reads are visible."""
import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "scripts/lib"):
    sys.path.insert(0, str(directory))
import build_t0_state as bts
import coordination_db as db
from project_scope import TenantUnresolved


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    state = tmp_path / "other/state"
    db.init_schema(state)
    with db.get_connection(state) as conn:
        for table in ("terminal_leases", "coordination_events"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
        conn.execute("DELETE FROM terminal_leases")
        for pid in ("other", "vnx-dev", "other-extra"):
            conn.execute("INSERT INTO terminal_leases (terminal_id, project_id) VALUES (?, ?)",
                         (pid, pid))
            db._append_event(conn, event_type="scope", entity_type="lease",
                             entity_id="shared", project_id=pid)
        conn.commit()
    return state


def call(conn, operation, **kwargs):
    if operation == "lease":
        return db.get_lease(conn, "vnx-dev", **kwargs)
    if operation == "events":
        return db.get_events(conn, entity_id="shared", **kwargs)
    if operation == "projection":
        return db.project_terminal_state(conn, **kwargs)
    return db._append_event(conn, event_type="scope", entity_type="lease",
                            entity_id="new", **kwargs)


@pytest.mark.parametrize("operation", ["lease", "events", "projection", "append"])
def test_unresolved_store_refuses_before_query(store, operation):
    statements = []
    with db.get_connection(store) as conn:
        conn.set_trace_callback(statements.append)
        with pytest.raises(TenantUnresolved):
            call(conn, operation)
    assert not any(s.startswith(("SELECT *", "INSERT INTO")) for s in statements)


@pytest.mark.parametrize("source", ["env", "marker", "explicit", "layout", "symlink"])
def test_resolvable_store_keeps_only_own_rows(store, tmp_path, monkeypatch, source):
    kwargs = {}
    if source == "env":
        monkeypatch.setenv("VNX_PROJECT_ID", "other")
    elif source == "marker":
        (store.parent / ".vnx-project-id").write_text("other\n")
    elif source == "explicit":
        kwargs["project_id"] = "other"
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    else:
        owner = tmp_path / ".vnx-data/other/state"
        owner.mkdir(parents=True)
        (store / db.DB_FILENAME).replace(owner / db.DB_FILENAME)
        if source == "symlink":
            (store / db.DB_FILENAME).symlink_to(owner / db.DB_FILENAME)
        else:
            store = owner
    with db.get_connection(store) as conn:
        assert call(conn, "lease", **kwargs) is None
        assert db.get_lease(conn, "other", **kwargs)["project_id"] == "other"
        assert {r["project_id"] for r in call(conn, "events", **kwargs)} == {"other"}
        assert set(call(conn, "projection", **kwargs)["terminals"]) == {"other"}
        event_id = call(conn, "append", **kwargs)
        assert conn.execute("SELECT project_id FROM coordination_events WHERE event_id=?",
                            (event_id,)).fetchone()[0] == "other"


@pytest.mark.parametrize("source", ["", "INVALID", "../other", "/other", "conflict"])
@pytest.mark.parametrize("operation", ["lease", "events", "projection", "append"])
def test_invalid_or_conflicting_sources_refuse(store, monkeypatch, source, operation):
    if source == "conflict":
        (store / ".vnx-project-id").write_text("other\n")
        source = "vnx-dev"
    monkeypatch.setenv("VNX_PROJECT_ID", source)
    with db.get_connection(store) as conn, pytest.raises(TenantUnresolved):
        call(conn, operation)


@pytest.mark.parametrize("project_id", [None, "", "INVALID", "../other", "/other"])
@pytest.mark.parametrize("operation", ["lease", "events", "projection", "append"])
def test_explicit_identity_is_validated(store, operation, project_id):
    with db.get_connection(store) as conn, pytest.raises(TenantUnresolved):
        call(conn, operation, project_id=project_id)


def test_connection_failures_stay_visible(store):
    conn = sqlite3.connect(store / db.DB_FILENAME)
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        db._connection_project_id(conn)
    with sqlite3.connect(":memory:") as memory, pytest.raises(TenantUnresolved):
        db._connection_project_id(memory)


@pytest.mark.parametrize("fallback", [False, True])
def test_init_refusal_is_counted_in_both_paths(store, caplog, fallback):
    with db.get_connection(store) as conn:
        conn.execute("PRAGMA user_version = 14")
        if fallback:
            # A damaged schema makes init fail; the existing-DB fallback retries migrations.
            conn.execute("DROP TABLE IF EXISTS provenance_registry")
            conn.execute("CREATE TABLE provenance_registry (id INTEGER)")
        conn.commit()
    assert bts._init_and_check_db(store, None) is False
    records = [json.loads(line) for line in (store / "skip_metrics.ndjson").read_text().splitlines()]
    assert len(records) == 1 and records[0]["context"] == "coordination_db"
    assert records[0]["reason"] == "project_id_unresolved"
    assert "ERROR" in caplog.text and "build_t0_state_hook" in caplog.text


def test_hook_finishes_and_counts_missing_read(store, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    # Apply the real migrations while identity is known, then remove the source.
    monkeypatch.setenv("VNX_PROJECT_ID", "other")
    assert bts._init_and_check_db(store, None)
    monkeypatch.delenv("VNX_PROJECT_ID")
    result = bts.build_t0_state(store, store.parent / "dispatches")
    assert result["live_work"]["available"] is False
    assert "project_id_unresolved" in result["live_work"]["reason"]
    assert "queues" in result and "recent_receipts" in result
    records = [json.loads(line) for line in (store / "skip_metrics.ndjson").read_text().splitlines()]
    refusals = [r for r in records if r["context"] == "coordination_db"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "project_id_unresolved"
    assert refusals[0]["hook"] == "build_t0_state_hook"
    assert "ERROR" in caplog.text and "build_t0_state_hook" in caplog.text
    (store / ".vnx-project-id").write_text("other\n")
    recovered = bts.build_t0_state(store, store.parent / "dispatches")
    assert recovered["live_work"]["available"] is True
    assert recovered.keys() == result.keys()
    for section in ("queues", "recent_receipts", "open_items"):
        assert recovered[section] == result[section]


@pytest.mark.parametrize("project_id", [None, "", "INVALID", "../other", "/other"])
def test_hook_invalid_identity_stays_unavailable(store, project_id):
    result = bts._build_live_work(store, project_id, None)
    assert result["available"] is False
    assert "project_id_unresolved" in result["reason"]
    records = [json.loads(line) for line in (store / "skip_metrics.ndjson").read_text().splitlines()]
    assert len(records) == 1 and records[0]["hook"] == "build_t0_state_hook"
