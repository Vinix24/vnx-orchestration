#!/usr/bin/env python3
"""Tests for config_store_db — the config control-plane persistence layer (P0, PR 2).

Dispatch-ID: 20260627-config-store-db

Covers: idempotent ensure_config_tables, ADR-007 composite keys on both tables, and read_config
fail-open (missing table / unset key → None, never raises).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_store_db as cs  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_ensure_creates_tables_idempotently(conn):
    cs.ensure_config_tables(conn)
    cs.ensure_config_tables(conn)  # second call must not raise
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"project_config", "project_config_audit"} <= tables


def test_project_config_pk_is_composite_over_project_id(conn):
    cs.ensure_config_tables(conn)
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(project_config)") if r[5] > 0]
    assert pk_cols == ["project_id", "config_key"]  # ADR-007 composite PK over project_id


def test_project_config_audit_has_composite_unique_over_project_id(conn):
    cs.ensure_config_tables(conn)
    # The audit table is append-only (id PK) + a composite UNIQUE(project_id, event_id) for ADR-007.
    cs.read_config  # keep import used
    conn.execute(
        "INSERT INTO project_config_audit(project_id, config_key, new_value, changed_by, event_id) "
        "VALUES ('p', 'k', 'v', 'op', 'evt-1')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO project_config_audit(project_id, config_key, new_value, changed_by, event_id) "
            "VALUES ('p', 'k2', 'v2', 'op', 'evt-1')"  # same (project_id, event_id) → rejected
        )
    # a different project may reuse the event_id
    conn.execute(
        "INSERT INTO project_config_audit(project_id, config_key, new_value, changed_by, event_id) "
        "VALUES ('other', 'k', 'v', 'op', 'evt-1')"
    )


def test_project_config_pk_rejects_duplicate_key_per_project(conn):
    cs.ensure_config_tables(conn)
    conn.execute("INSERT INTO project_config(project_id, config_key, config_value, updated_by) VALUES ('p','k','0','op')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO project_config(project_id, config_key, config_value, updated_by) VALUES ('p','k','1','op')")
    # same key, different project is fine
    conn.execute("INSERT INTO project_config(project_id, config_key, config_value, updated_by) VALUES ('p2','k','1','op')")


def test_read_config_returns_value_and_none(conn):
    cs.ensure_config_tables(conn)
    conn.execute("INSERT INTO project_config(project_id, config_key, config_value, updated_by) VALUES ('vnx-dev','VNX_SCOUT_PREPASS','1','operator')")
    assert cs.read_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS") == "1"
    assert cs.read_config(conn, "vnx-dev", "VNX_TAGGER_ENABLED") is None  # unset
    assert cs.read_config(conn, "other-proj", "VNX_SCOUT_PREPASS") is None  # tenant-scoped


def test_read_config_fail_open_when_table_absent(conn):
    # No ensure_config_tables call → table missing → read must return None, not raise.
    assert cs.has_config_tables(conn) is False
    assert cs.read_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
