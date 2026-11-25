"""PR-6h-c: the single hash-chain config reader binds the ledger's store without git."""
import ast
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/lib")]
import config_registry as registry
import config_runtime as runtime
import config_store_db
import ledger_health
import ndjson_hash_chain as chain
from test_env_reads_t0_rotation_flag import python_reads
from test_receipt_hashchain import _append_one
from test_append_receipt_stderr import _run_append, _assert_all_structured
from test_t0_context_guard import _logging_git

KEY = "VNX_HASH_CHAIN_REQUIRED"


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key in (KEY, "VNX_OVERRIDE_HASH_CHAIN_REQUIRED", "VNX_CHAIN_RECEIPTS", "VNX_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "pr6h")
    monkeypatch.setattr(runtime, "_wired_for", {})
    monkeypatch.setattr(runtime, "_wire_status", None)
    monkeypatch.setattr(registry, "_db_resolver", None)
    monkeypatch.setattr(registry, "_default_project_id", None)

    def put(state, value):
        state.mkdir(exist_ok=True)
        with sqlite3.connect(state / "runtime_coordination.db") as conn:
            config_store_db.set_config(conn, "pr6h", KEY, value, actor="test", approval_id="pr6h")
    return put


@pytest.mark.parametrize("value", ["0", "1"])
@pytest.mark.parametrize("source", ["row", "override", "env"])
def test_precedence_without_git(store, tmp_path, monkeypatch, value, source):
    opposite = str(1 - int(value))
    monkeypatch.setenv(KEY, opposite if source != "env" else value)
    if source != "env":
        store(tmp_path, value if source == "row" else opposite)
    if source == "override":
        monkeypatch.setenv("VNX_OVERRIDE_HASH_CHAIN_REQUIRED", value)
    bin_dir, log = _logging_git(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert chain.chain_receipts_variable_on(state_dir=tmp_path) is (value == "1")
    ledger = tmp_path / "t0_receipts.ndjson"
    _append_one(ledger, {"event_type": "test_event", "dispatch_id": "pr6h"})
    assert ("prev_hash" in json.loads(ledger.read_text())) is (value == "1")
    assert ledger_health._chain_receipts_configured(ledger)[0] is (value == "1")
    assert not log.exists(), log.read_text()


@pytest.mark.parametrize("legacy", [False, True])
def test_default_and_retired_flag(store, tmp_path, monkeypatch, legacy):
    if legacy:
        monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
    assert chain.append_chains(chain.TAIL_EMPTY, state_dir=tmp_path) is False


@pytest.mark.parametrize("target_exists", [False, True])
def test_explicit_store_never_reuses_ambient_store(store, tmp_path, monkeypatch, caplog, target_exists):
    store(tmp_path, "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    assert chain.chain_receipts_variable_on()
    target = tmp_path / "target"
    if target_exists:
        store(target, "0")
    assert chain.append_chains(chain.TAIL_EMPTY, state_dir=target) is False
    if not target_exists:
        assert "no runtime_coordination.db" in caplog.text


def test_retired_flag_has_no_environment_readers():
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in (ROOT / top).rglob("*.py"):
            assert python_reads(ast.parse(path.read_text()), "VNX_CHAIN_RECEIPTS") == 0, path
        for path in (ROOT / top).rglob("*.sh"):
            assert "$VNX_CHAIN_RECEIPTS" not in path.read_text(), path
            assert "${VNX_CHAIN_RECEIPTS" not in path.read_text(), path


@pytest.mark.parametrize("global_content", [None, "invalid json"])
def test_fallback_diagnostics_remain_json(tmp_path, global_content):
    config = tmp_path / "global.json"
    if global_content is not None:
        config.write_text(global_content)
    result = _run_append(tmp_path, '{"timestamp":"2026-11-25T00:00:00Z","event_type":"task_started","dispatch_id":"pr6h","terminal":"T1"}',
                         ["--skip-enrichment"], {"VNX_GLOBAL_CONFIG_FILE": str(config)})
    assert result.returncode == 0, result.stderr
    lines = _assert_all_structured(result.stderr)
    assert any(e["code"] == "receipt_config" and "config_runtime" in e["message"] for e in lines)
    if global_content is not None:
        assert any(e["code"] == "receipt_config" and e["logger"] == "config_registry" for e in lines)


def test_config_log_routing_restored_on_error():
    import logging
    from append_receipt_internals.idempotency import _structured_config_logs
    logger = logging.getLogger("config_runtime")
    before = list(logger.filters)
    with pytest.raises(RuntimeError, match="visible failure"):
        with _structured_config_logs():
            raise RuntimeError("visible failure")
    assert logger.filters == before
