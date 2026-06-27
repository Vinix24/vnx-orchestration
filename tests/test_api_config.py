#!/usr/bin/env python3
"""Tests for api_config — the config control-plane HTTP handlers (P0, PR 4).

Dispatch-ID: 20260627-api-config

Covers GET inventory (shape + DB-set value surfacing), POST set (success + the 400/403 error mapping
for unknown / not-writable / approval-required keys), and GET audit (newest-first, key filter,
fail-open on a missing DB).
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import config_registry as cr  # noqa: E402
import api_config as ac  # noqa: E402

PID = "vnx-dev"


@pytest.fixture
def db(tmp_path):
    """A per-project runtime_coordination.db path + its state dir wired into the registry resolver."""
    state_dir = tmp_path
    db_path = state_dir / "runtime_coordination.db"
    sqlite3.connect(db_path).close()  # create the file
    ac._wire_resolver(state_dir)
    yield db_path
    cr.set_db_resolver(None)


# ---------------------------------------------------------------------------
# POST /api/operator/config/set
# ---------------------------------------------------------------------------

def test_set_success_returns_200_and_persists(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_SCOUT_PREPASS", "value": "1", "actor": "alice"},
        project_id=PID, db_path=db,
    )
    assert status == 200
    assert res["status"] == "success"
    assert res["new_value"] == "1"
    assert res["event_id"]
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT config_value FROM project_config WHERE project_id=? AND config_key='VNX_SCOUT_PREPASS'",
        (PID,),
    ).fetchone()[0] == "1"
    conn.close()


def test_set_unknown_key_returns_400(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_NOPE", "value": "1"}, project_id=PID, db_path=db)
    assert status == 400
    assert res["status"] == "failed"


def test_set_non_writable_key_returns_403(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_USE_FEDERATION", "value": "1"}, project_id=PID, db_path=db)
    assert status == 403


def test_set_requires_approval_without_id_returns_403(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_CI_GATE_REQUIRED", "value": "1"}, project_id=PID, db_path=db)
    assert status == 403


def test_set_requires_approval_with_id_succeeds(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_CI_GATE_REQUIRED", "value": "1", "approval_id": "appr-9"},
        project_id=PID, db_path=db)
    assert status == 200
    assert res["approval_id"] == "appr-9"


def test_set_bad_bool_value_returns_400(db):
    res, status = ac.operator_post_config_set(
        {"key": "VNX_SCOUT_PREPASS", "value": "perhaps"}, project_id=PID, db_path=db)
    assert status == 400


def test_set_missing_key_returns_400(db):
    res, status = ac.operator_post_config_set({"value": "1"}, project_id=PID, db_path=db)
    assert status == 400


def test_set_missing_value_returns_400(db):
    res, status = ac.operator_post_config_set({"key": "VNX_SCOUT_PREPASS"}, project_id=PID, db_path=db)
    assert status == 400


# ---------------------------------------------------------------------------
# GET /api/operator/config
# ---------------------------------------------------------------------------

def test_get_config_inventory_shape(db):
    out, status = ac.operator_get_config({}, project_id=PID)
    assert status == 200
    assert out["project_id"] == PID
    keys = {row["key"] for row in out["config"]}
    assert "VNX_SCOUT_PREPASS" in keys
    row = next(r for r in out["config"] if r["key"] == "VNX_CI_GATE_REQUIRED")
    assert row["requires_approval"] is True


def test_get_config_reflects_db_value(db):
    # write via the DAO, then the inventory (through the wired resolver) must show it as non-default
    ac.operator_post_config_set(
        {"key": "VNX_SCOUT_PREPASS", "value": "1"}, project_id=PID, db_path=db)
    out, status = ac.operator_get_config({}, project_id=PID)
    assert status == 200
    row = next(r for r in out["config"] if r["key"] == "VNX_SCOUT_PREPASS")
    assert row["value"] == "1"
    assert row["is_default"] is False


# ---------------------------------------------------------------------------
# GET /api/operator/config/audit
# ---------------------------------------------------------------------------

def test_audit_returns_changes_newest_first(db):
    ac.operator_post_config_set({"key": "VNX_SCOUT_PREPASS", "value": "1", "actor": "a"}, project_id=PID, db_path=db)
    ac.operator_post_config_set({"key": "VNX_SCOUT_PREPASS", "value": "0", "actor": "b"}, project_id=PID, db_path=db)
    out, status = ac.operator_get_config_audit({}, project_id=PID, db_path=db)
    assert status == 200
    assert out["project_id"] == PID
    assert len(out["audit"]) == 2
    assert out["audit"][0]["new_value"] == "0"  # newest first
    assert out["audit"][0]["changed_by"] == "b"


def test_audit_key_filter(db):
    ac.operator_post_config_set({"key": "VNX_SCOUT_PREPASS", "value": "1"}, project_id=PID, db_path=db)
    ac.operator_post_config_set({"key": "VNX_TAGGER_ENABLED", "value": "1"}, project_id=PID, db_path=db)
    out, status = ac.operator_get_config_audit({"key": ["VNX_TAGGER_ENABLED"]}, project_id=PID, db_path=db)
    assert len(out["audit"]) == 1
    assert out["audit"][0]["config_key"] == "VNX_TAGGER_ENABLED"


def test_audit_fail_open_when_db_missing(tmp_path):
    missing = tmp_path / "no-such.db"
    out, status = ac.operator_get_config_audit({}, project_id=PID, db_path=missing)
    assert status == 200
    assert out["audit"] == []


def test_audit_is_tenant_scoped(db):
    ac.operator_post_config_set({"key": "VNX_SCOUT_PREPASS", "value": "1"}, project_id="proj-a", db_path=db)
    out, status = ac.operator_get_config_audit({}, project_id="proj-b", db_path=db)
    assert out["audit"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
