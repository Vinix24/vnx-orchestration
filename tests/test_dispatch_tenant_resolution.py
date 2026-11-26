"""AG-1f2: daemon and shell attribution requires an authoritative source."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/lib")]


@pytest.mark.parametrize("site", ["dispatch_broker", "headless_dispatch_writer", "pr_queue_manager", "roadmap_manager", "inbound_inbox"])
@pytest.mark.parametrize("source", ["missing", "env", "marker", "store", "conflict", "invalid"])
def test_python_resolution(site, source, tmp_path, monkeypatch, caplog):
    from conftest import pin_vnx_data_dir
    from project_scope import TenantUnresolved
    from runtime_coordination import init_schema, get_connection
    import importlib

    data = tmp_path / ".vnx-data/other" if source == "store" else tmp_path / "data"
    pin_vnx_data_dir(monkeypatch, data)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", "other")
    state = data / "state"
    init_schema(state)
    from project_id_migration import run_runtime_coordination_migration
    run_runtime_coordination_migration(state / "runtime_coordination.db", default_project_id="other")
    monkeypatch.chdir(state)
    module = importlib.import_module(site)
    if site == "dispatch_broker":
        broker = module.DispatchBroker(state, data / "dispatches", intelligence_enabled=False)
        call = lambda: broker.register("tenant-test", "work").dispatch_row["project_id"]
    elif site == "inbound_inbox":
        inbox = module.InboundInbox(state)
        event = inbox.receive("channel", {"text": "work"}).event
        call = lambda: inbox.process(event.event_id)
    elif site == "headless_dispatch_writer":
        call = lambda: json.loads(module.write_dispatch("tenant-test", "T1", "A", "backend-developer", "work").read_text())["project_id"]
    elif site == "roadmap_manager":
        call = lambda: module.RoadmapManager().project_id
    else:
        call = lambda: module._get_project_id()
    monkeypatch.delenv("VNX_PROJECT_ID")
    if source in {"marker", "conflict"}:
        (data / ".vnx-project-id").write_text("other\n")
    if source in {"env", "conflict", "invalid"}:
        monkeypatch.setenv("VNX_PROJECT_ID", {"env": "other", "conflict": "another", "invalid": "../other"}[source])
    if source in {"missing", "conflict", "invalid"}:
        if site == "inbound_inbox":
            result = call()
            assert result.outcome == "retry" and "project_id_unresolved" in result.failure_reason
            message = result.failure_reason
        else:
            with pytest.raises(TenantUnresolved) as exc:
                call()
            message = str(exc.value)
        if source == "missing":
            assert "vnx init" in message and "VNX_PROJECT_ID" in message
        records = [json.loads(line) for line in (state / "skip_metrics.ndjson").read_text().splitlines()]
        assert any(r["context"] == site and r["reason"] == "project_id_unresolved" for r in records)
        assert any(r.levelname == "ERROR" and "project_id_unresolved" in r.message for r in caplog.records)
        with get_connection(state) as conn:
            assert conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 0
        if site == "dispatch_broker":
            from runtime_core import RuntimeCore
            from lease_manager import LeaseManager
            assert RuntimeCore(broker, LeaseManager(state, auto_init=False)).register("refused", "work").registered is False
        monkeypatch.setenv("VNX_PROJECT_ID", "other")
    result = call()
    assert result.outcome == "dispatched" if site == "inbound_inbox" else result == "other"
    if site == "inbound_inbox":
        with get_connection(state) as conn:
            assert conn.execute("SELECT project_id FROM dispatches WHERE dispatch_id = ?", (result.dispatch_id,)).fetchone()[0] == "other"


@pytest.mark.parametrize("source", ["missing", "empty", "env", "marker", "store", "explicit", "conflict", "invalid"])
def test_shell_resolution(source, tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    if source in {"env", "conflict", "invalid", "empty", "explicit"}:
        env["VNX_PROJECT_ID"] = {"env": "other", "conflict": "another", "invalid": "../other", "empty": "", "explicit": "another"}[source]
    if source == "store":
        env["VNX_DATA_DIR"] = str(tmp_path / ".vnx-data/other")
    if source in {"marker", "conflict"}:
        (tmp_path / ".vnx-project-id").write_text("other\n")
    command = 'source "$1"; vnx_dispatch_resolve_project_id "$2"'
    result = subprocess.run(["bash", "-c", command, "bash", str(ROOT / "scripts/lib/dispatch_project_guard.sh"), "other" if source == "explicit" else ""], cwd=tmp_path, env=env, capture_output=True, text=True)
    if source in {"missing", "empty", "conflict", "invalid"}:
        assert result.returncode != 0 and not result.stdout
        assert "project_id_unresolved" in result.stderr
        if source == "missing":
            assert "vnx init" in result.stderr and "VNX_PROJECT_ID" in result.stderr
    else:
        assert result.returncode == 0 and result.stdout.strip() == "other"
