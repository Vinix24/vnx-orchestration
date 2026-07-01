#!/usr/bin/env python3
"""Tests for config_runtime — the runtime-process façade that autowires config_registry (P0, PR 6).

Dispatch-ID: 20260627-config-runtime

Covers: autowire binds the DB resolver + default project so a UI-set value is honoured; behaviour
preservation (no DB value / no state → exactly env-or-default); idempotence + fail-soft; and
config_registry's new default-project resolution.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402
import config_runtime as crt  # noqa: E402

PID = "vnx-dev"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _state_dir_with(tmp_path, key, value, project_id=PID):
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    cs.set_config(conn, project_id, key, value, actor="op")
    conn.close()
    return sd


# ---------------------------------------------------------------------------
# autowire honours a UI-set value
# ---------------------------------------------------------------------------

def test_autowire_honours_db_value(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1")
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")  # env says off…
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True  # …DB (UI) wins


def test_get_autowires_from_env(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_TAGGER_PROVIDER", "kimi")
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    # no explicit autowire() — get() must autowire from the env
    assert crt.get("VNX_TAGGER_PROVIDER") == "kimi"


# ---------------------------------------------------------------------------
# behaviour preservation + fail-soft
# ---------------------------------------------------------------------------

def test_no_db_value_falls_through_to_env(tmp_path, monkeypatch):
    sd = _state_dir_with(tmp_path, "VNX_TAGGER_ENABLED", "1")  # a DB exists, but for a different key
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    crt.autowire(state_dir=sd, project_id=PID)
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True   # no DB row → env wins
    assert crt.get_bool("VNX_OUTCOME_GROUNDING_V2") is False  # no DB row, no env → default


def test_autowire_failsoft_without_state_dir():
    assert crt.autowire(state_dir=None, project_id=PID) is False
    # registry stays env-only → default
    assert crt.get_bool("VNX_SCOUT_PREPASS") is False


def test_autowire_failsoft_without_db(tmp_path):
    empty = tmp_path / "no-db"
    empty.mkdir()
    assert crt.autowire(state_dir=empty, project_id=PID) is False


def test_autowire_is_idempotent(tmp_path):
    sd = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1")
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    # A second call with the same (state_dir, project_id) is a fast no-op and still returns True.
    assert crt.autowire(state_dir=sd, project_id=PID) is True
    assert crt.get_bool("VNX_SCOUT_PREPASS") is True


# ---------------------------------------------------------------------------
# config_registry default-project resolution
# ---------------------------------------------------------------------------

def test_default_project_used_when_arg_omitted():
    cr.set_db_resolver(lambda pid, key: "1" if (pid == PID and key == "VNX_SCOUT_PREPASS") else None)
    # without a default project, an omitted project_id resolves to None → resolver returns None
    assert cr.get("VNX_SCOUT_PREPASS") == "0"
    cr.set_default_project_id(PID)
    assert cr.get("VNX_SCOUT_PREPASS") == "1"  # now the omitted arg resolves to the default project


def test_explicit_project_id_beats_default():
    cr.set_default_project_id("default-proj")
    seen = {}
    def _res(pid, key):
        seen["pid"] = pid
        return None
    cr.set_db_resolver(_res)
    cr.get("VNX_SCOUT_PREPASS", project_id="explicit-proj")
    assert seen["pid"] == "explicit-proj"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
