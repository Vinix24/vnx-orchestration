"""enforcement_profile and config_runtime.wire_status (config-een-oppervlak V3.1, PR-Q3a)."""
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import config_registry as cr
import config_runtime as crt
import config_store_db as cs
import enforcement_profile as ep
import vnx_paths

PID = "vnx-dev"
KEY = "VNX_ENFORCEMENT_PROFILE"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("VNX_STATE_DIR", "VNX_PROJECT_ID", KEY, "VNX_OVERRIDE_ENFORCEMENT_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    monkeypatch.setattr(crt, "_wire_status", None)
    crt._wired_for.clear()
    ep._LOGGED.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _store(tmp_path, value):
    sd = tmp_path / "state"
    sd.mkdir()
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    cs.set_config(conn, PID, KEY, value, actor="op", approval_id="test-approval")
    conn.close()
    return sd


def _wire(monkeypatch, sd):
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name == "enforcement_profile"]


def test_store_missing_with_stored_enforced_is_loud(tmp_path, monkeypatch, caplog):
    _store(tmp_path, "enforced")  # the row exists, but this process cannot find its store
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    _wire(monkeypatch, empty)
    with caplog.at_level(logging.WARNING):
        reading = ep.read()
        ep.read()
    assert crt.wire_status().cause == "store_missing"
    assert (reading.source, reading.cause, reading.fallback_source) == ("project_config_error", "store_missing", "default")
    errors = _messages(caplog, logging.ERROR)
    assert len(errors) == 1, errors  # one ERROR per process
    for part in (KEY, PID, "project_config_error", "store_missing", "NOT applied", "'shadow' from default"):
        assert part in errors[0]


def test_store_missing_falls_back_to_env(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setenv(KEY, "enforced")
    reading = ep.read()
    assert reading.enforced and reading.fallback_source == "env" and reading.cause == "store_missing"


def test_wired_store_row_wins(tmp_path, monkeypatch):
    _wire(monkeypatch, _store(tmp_path, "enforced"))
    monkeypatch.setenv(KEY, "shadow")
    reading = ep.read()
    assert (reading.enforced, reading.source, reading.cause) == (True, "project_config", None)
    assert crt.wire_status().cause is None


def test_wired_store_unreadable_is_enforced(tmp_path, monkeypatch, caplog):
    sd = _store(tmp_path, "shadow")
    _wire(monkeypatch, sd)
    assert crt.autowire() is True
    (sd / "runtime_coordination.db").write_bytes(b"not a database" * 100)
    with caplog.at_level(logging.ERROR):
        reading = ep.read()
    assert (reading.enforced, reading.source, reading.cause) == (True, "project_config_error", "read_failed")
    assert any(KEY in m and "read_failed" in m for m in _messages(caplog, logging.ERROR))


def test_resolver_failed_is_error(monkeypatch, caplog):
    def _boom():
        raise OSError("resolver down")
    monkeypatch.setattr(vnx_paths, "resolve_paths", _boom)
    with caplog.at_level(logging.WARNING):
        reading = ep.read()
    assert crt.wire_status().cause == "resolver_failed"
    assert (reading.enforced, reading.source, reading.cause) == (False, "project_config_error", "resolver_failed")
    assert any("resolver_failed" in m for m in _messages(caplog, logging.ERROR))


def test_no_project_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        reading = ep.read()
        ep.read()
    assert (reading.enforced, reading.source, reading.cause) == (False, "default", "no_project")
    assert len(_messages(caplog, logging.WARNING)) == 1
    assert _messages(caplog, logging.ERROR) == []


@pytest.mark.parametrize("var", [KEY, "VNX_OVERRIDE_ENFORCEMENT_PROFILE"])
@pytest.mark.parametrize("value", ["strict", "", "ENFORCED", "1"])
def test_out_of_set_value_reads_enforced(monkeypatch, caplog, var, value):
    monkeypatch.setenv(var, value)
    with caplog.at_level(logging.ERROR):
        assert ep.enforced() is True
    assert any(repr(value) in m and "outside" in m for m in _messages(caplog, logging.ERROR))


def test_override_takes_one_process_back_to_shadow(tmp_path, monkeypatch):
    _wire(monkeypatch, _store(tmp_path, "enforced"))
    monkeypatch.setenv("VNX_OVERRIDE_ENFORCEMENT_PROFILE", "shadow")
    reading = ep.read()
    assert (reading.enforced, reading.source) == (False, "override")


def test_storeless_reader_does_not_load_config_runtime():
    code = ("import sys; sys.path.insert(0, %r); import enforcement_profile as e; "
            "print(e.enforced_storeless(), 'config_runtime' in sys.modules)" % str(LIB))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip().endswith("False"), out

