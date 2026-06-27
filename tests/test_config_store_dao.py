#!/usr/bin/env python3
"""Tests for the config control-plane DAO — set_config + the DB-resolver (P0, PR 3).

Dispatch-ID: 20260627-config-store-dao

Covers the write path: registry validation (unknown / not-writable / approval-required all raise),
type-coercion, the atomic value+audit write (no write without an audit row), tenant scoping, and
make_db_resolver feeding config_registry's precedence chain.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_resolver():
    cr.set_db_resolver(None)
    yield
    cr.set_db_resolver(None)


# ---------------------------------------------------------------------------
# Validation — the registry gates the write
# ---------------------------------------------------------------------------

def test_unknown_key_rejected(conn):
    with pytest.raises(ValueError):
        cs.set_config(conn, "vnx-dev", "VNX_NOT_A_FLAG", "1", actor="operator")


def test_non_writable_key_rejected(conn):
    # VNX_USE_FEDERATION is planned + writable_from_ui=False.
    with pytest.raises(PermissionError):
        cs.set_config(conn, "vnx-dev", "VNX_USE_FEDERATION", "1", actor="operator")


def test_requires_approval_without_approval_id_rejected(conn):
    # VNX_CI_GATE_REQUIRED requires_approval=True.
    with pytest.raises(PermissionError):
        cs.set_config(conn, "vnx-dev", "VNX_CI_GATE_REQUIRED", "1", actor="operator")


def test_requires_approval_with_approval_id_succeeds(conn):
    res = cs.set_config(conn, "vnx-dev", "VNX_CI_GATE_REQUIRED", "1", actor="operator", approval_id="appr-1")
    assert res["new_value"] == "1"
    assert cs.read_config(conn, "vnx-dev", "VNX_CI_GATE_REQUIRED") == "1"


def test_bad_bool_value_rejected(conn):
    with pytest.raises(ValueError):
        cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "maybe", actor="operator")


# ---------------------------------------------------------------------------
# Coercion + the write
# ---------------------------------------------------------------------------

def test_bool_coercion_variants(conn):
    cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", True, actor="op")
    assert cs.read_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS") == "1"
    cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "false", actor="op")
    assert cs.read_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS") == "0"


def test_set_then_upsert_overwrites(conn):
    cs.set_config(conn, "vnx-dev", "VNX_TAGGER_PROVIDER", "deepseek", actor="op")
    res = cs.set_config(conn, "vnx-dev", "VNX_TAGGER_PROVIDER", "kimi", actor="op")
    assert res["old_value"] == "deepseek"
    assert res["new_value"] == "kimi"
    # one live row per (project, key)
    n = conn.execute(
        "SELECT COUNT(*) FROM project_config WHERE project_id='vnx-dev' AND config_key='VNX_TAGGER_PROVIDER'"
    ).fetchone()[0]
    assert n == 1


def test_every_write_leaves_an_audit_row(conn):
    cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "1", actor="alice")
    cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "0", actor="bob")
    rows = conn.execute(
        "SELECT old_value, new_value, changed_by FROM project_config_audit "
        "WHERE project_id='vnx-dev' AND config_key='VNX_SCOUT_PREPASS' ORDER BY id"
    ).fetchall()
    assert rows == [(None, "1", "alice"), ("1", "0", "bob")]


def test_audit_row_carries_event_id(conn):
    res = cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "1", actor="op")
    stored = conn.execute(
        "SELECT event_id FROM project_config_audit WHERE project_id='vnx-dev'"
    ).fetchone()[0]
    assert stored == res["event_id"]
    assert stored  # non-empty


def test_event_emit_failure_leaves_the_write_intact(conn):
    # The :memory: db has no coordination_events table, so the best-effort event emit fails
    # internally. set_config must still succeed and persist the value + its audit row.
    res = cs.set_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS", "1", actor="op")
    assert res["new_value"] == "1"
    assert cs.read_config(conn, "vnx-dev", "VNX_SCOUT_PREPASS") == "1"
    n = conn.execute(
        "SELECT COUNT(*) FROM project_config_audit WHERE project_id='vnx-dev'"
    ).fetchone()[0]
    assert n == 1


def test_writes_are_tenant_scoped(conn):
    cs.set_config(conn, "proj-a", "VNX_SCOUT_PREPASS", "1", actor="op")
    cs.set_config(conn, "proj-b", "VNX_SCOUT_PREPASS", "0", actor="op")
    assert cs.read_config(conn, "proj-a", "VNX_SCOUT_PREPASS") == "1"
    assert cs.read_config(conn, "proj-b", "VNX_SCOUT_PREPASS") == "0"


# ---------------------------------------------------------------------------
# make_db_resolver feeds config_registry
# ---------------------------------------------------------------------------

def test_db_resolver_reads_back_through_registry(tmp_path, monkeypatch):
    # a per-project state dir with a real runtime_coordination.db carrying a config value
    sdir = tmp_path / "proj-x"
    sdir.mkdir()
    db = sdir / "runtime_coordination.db"
    c = sqlite3.connect(db)
    cs.set_config(c, "proj-x", "VNX_SCOUT_PREPASS", "1", actor="op")
    c.close()

    # the env says off; the DB (via the resolver) says on → DB wins (precedence step 2)
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")
    cr.set_db_resolver(cs.make_db_resolver(lambda pid: tmp_path / pid if pid else None))
    assert cr.get("VNX_SCOUT_PREPASS", project_id="proj-x") == "1"
    # a project without a DB falls through to the env
    assert cr.get("VNX_SCOUT_PREPASS", project_id="proj-missing") == "0"


def test_db_resolver_none_project_returns_none(tmp_path):
    resolver = cs.make_db_resolver(lambda pid: tmp_path / pid if pid else None)
    assert resolver(None, "VNX_SCOUT_PREPASS") is None


def test_db_resolver_missing_db_returns_none(tmp_path):
    resolver = cs.make_db_resolver(lambda pid: tmp_path / "no-such-dir")
    assert resolver("p", "VNX_SCOUT_PREPASS") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
