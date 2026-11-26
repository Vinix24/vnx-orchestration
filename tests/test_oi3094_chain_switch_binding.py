"""OI-3094: explicit receipt stores own their project and expose config read failures."""
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/lib")]
import config_registry as registry
import config_runtime as runtime
import config_store_db
import ndjson_hash_chain as chain
from test_receipt_hashchain import _append_one

KEY = "VNX_HASH_CHAIN_REQUIRED"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for key in (KEY, "VNX_OVERRIDE_HASH_CHAIN_REQUIRED", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime, "_wired_for", {})
    monkeypatch.setattr(runtime, "_wire_status", None)
    monkeypatch.setattr(registry, "_db_resolver", None)
    monkeypatch.setattr(registry, "_default_project_id", None)


def _store(root, project, value):
    state = root / project / "state"
    state.mkdir(parents=True)
    (state.parent / ".vnx-project-id").write_text(project + "\n")
    with sqlite3.connect(state / "runtime_coordination.db") as conn:
        config_store_db.set_config(conn, project, KEY, value, actor="test", approval_id="oi3094")
    return state


def _global_config(tmp_path, monkeypatch, value):
    path = tmp_path / "global.json"
    path.write_text(json.dumps({"version": 1, "values": {KEY: {"value": value}}}))
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(path))


@pytest.mark.parametrize("value", ["0", "1"])
def test_explicit_store_uses_its_project_with_ambient_and_foreign_rows(tmp_path, monkeypatch, value):
    opposite = str(1 - int(value))
    a = _store(tmp_path, "project-a", opposite)
    b = _store(tmp_path, "project-b", value)
    with sqlite3.connect(b / "runtime_coordination.db") as conn:
        config_store_db.set_config(conn, "project-a", KEY, opposite, actor="test", approval_id="oi3094")
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    monkeypatch.setenv("VNX_STATE_DIR", str(a))
    assert chain.chain_receipts_variable_on() is (opposite == "1")
    assert chain.chain_receipts_variable_on(state_dir=b) is (value == "1")
    assert chain.chain_receipts_variable_on(state_dir=a) is (opposite == "1")
    assert chain.chain_receipts_variable_on(state_dir=b) is (value == "1")


@pytest.mark.parametrize("global_value", [None, "0", "1"])
def test_corrupt_store_logs_key_path_error_and_preserves_append(
    tmp_path, monkeypatch, caplog, capsys, global_value,
):
    state = _store(tmp_path, "project-b", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "project-b")
    assert chain.chain_receipts_variable_on(state_dir=state)
    (state / "runtime_coordination.db").write_bytes(b"not a sqlite database")
    if global_value is not None:
        _global_config(tmp_path, monkeypatch, global_value)
    expected = global_value == "1"
    with caplog.at_level(logging.WARNING):
        assert chain.chain_receipts_variable_on(state_dir=state) is expected
    assert any(KEY in r.message and str(state) in r.message and "not a database" in r.message
               for r in caplog.records if r.levelno == logging.WARNING)

    ledger = state / "t0_receipts.ndjson"
    _append_one(ledger, {"event_type": "test_event", "dispatch_id": "oi3094"})
    receipt = json.loads(ledger.read_text())
    assert receipt["dispatch_id"] == "oi3094"
    assert ("prev_hash" in receipt) is expected
    logs = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(r["code"] == "receipt_config" and r["level"] == "WARN"
               and KEY in r["message"] and str(state) in r["message"]
               and "not a database" in r["message"] for r in logs)


@pytest.mark.parametrize("value", ["0", "1"])
def test_explicit_store_rebinds_same_project_a_b_a(tmp_path, value):
    opposite = str(1 - int(value))
    a = _store(tmp_path / "a", "same-project", value)
    b = _store(tmp_path / "b", "same-project", opposite)
    assert chain.chain_receipts_variable_on(state_dir=a) is (value == "1")
    assert chain.chain_receipts_variable_on(state_dir=b) is (opposite == "1")
    assert chain.chain_receipts_variable_on(state_dir=a) is (value == "1")


@pytest.mark.parametrize("identity", [None, "../invalid", ""])
@pytest.mark.parametrize("global_value", [None, "0", "1"])
def test_unknown_project_warns_and_never_reuses_ambient_row(
    tmp_path, monkeypatch, caplog, identity, global_value,
):
    ambient = _store(tmp_path, "project-a", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    monkeypatch.setenv("VNX_STATE_DIR", str(ambient))
    assert chain.chain_receipts_variable_on()
    state = tmp_path / "unidentified" / "state"
    state.mkdir(parents=True)
    if identity is not None:
        (state.parent / ".vnx-project-id").write_text(identity)
    with sqlite3.connect(state / "runtime_coordination.db") as conn:
        config_store_db.set_config(conn, "project-a", KEY, "1", actor="test", approval_id="oi3094")
    if global_value is not None:
        _global_config(tmp_path, monkeypatch, global_value)
    with caplog.at_level(logging.WARNING):
        assert chain.chain_receipts_variable_on(state_dir=state) is (global_value == "1")
    assert any(KEY in r.message and str(state) in r.message and "project" in r.message
               for r in caplog.records if r.levelno == logging.WARNING)
    assert "global and default layers only" in caplog.text
    assert "existing override and env precedence preserved" in caplog.text
    assert "override > global config > env > default" in caplog.text


@pytest.mark.parametrize("global_value,env_value,override,expected", [
    (None, None, None, False),
    (None, "1", None, True),
    ("0", "1", None, False),
    ("1", "0", "0", False),
    ("0", "0", "1", True),
])
def test_unknown_project_fallback_preserves_non_project_precedence(
    tmp_path, monkeypatch, caplog, global_value, env_value, override, expected,
):
    state = tmp_path / "unidentified" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    with sqlite3.connect(state / "runtime_coordination.db") as conn:
        config_store_db.set_config(conn, "project-a", KEY, "1", actor="test", approval_id="oi3094")
    if global_value is not None:
        _global_config(tmp_path, monkeypatch, global_value)
    if env_value is not None:
        monkeypatch.setenv(KEY, env_value)
    if override is not None:
        monkeypatch.setenv("VNX_OVERRIDE_HASH_CHAIN_REQUIRED", override)
    with caplog.at_level(logging.WARNING):
        assert chain.chain_receipts_variable_on(state_dir=state) is expected
    assert "ignoring project rows" in caplog.text
    assert "override > global config > env > default" in caplog.text


def test_missing_explicit_store_does_not_reuse_wired_project(tmp_path, monkeypatch, caplog):
    ambient = _store(tmp_path, "project-a", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(ambient))
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    assert chain.chain_receipts_variable_on()
    state = tmp_path / "project-b" / "state"
    state.mkdir(parents=True)
    (state.parent / ".vnx-project-id").write_text("project-b\n")
    assert chain.chain_receipts_variable_on(state_dir=state) is False
    assert str(state) in caplog.text and "no runtime_coordination.db" in caplog.text


def test_audit_unchained_names_the_current_config_command(tmp_path):
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text('{"dispatch_id":"oi3094"}\n')
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/audit_chain.py"), "verify", str(ledger)],
                          text=True, capture_output=True)
    assert proc.returncode == 2
    assert "vnx config set VNX_HASH_CHAIN_REQUIRED 1" in json.loads(proc.stdout)["warning"]
    assert "VNX_CHAIN_RECEIPTS" not in proc.stdout + proc.stderr


def test_retired_key_only_remains_in_retirement_assertions_and_history():
    proc = subprocess.run(["git", "grep", "-l", "VNX_CHAIN_RECEIPTS"], cwd=ROOT,
                          text=True, capture_output=True)
    assert proc.returncode in (0, 1), proc.stderr
    retirement_tests = {"tests/test_env_reads_pr6h_hash_chain.py", Path(__file__).relative_to(ROOT).as_posix()}
    # Frozen release history and archived investigations retain the keys measured then.
    unexpected = [p for p in proc.stdout.splitlines()
                  if p not in retirement_tests and p != "CHANGELOG.md" and not p.startswith("changelog.d/")
                  and not p.startswith(("docs/governance/decisions/ADR-", "docs/_archive/"))]
    assert not unexpected, unexpected
