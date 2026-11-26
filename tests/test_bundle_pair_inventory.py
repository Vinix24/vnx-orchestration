"""Inventory real broker state without changing bundles or lifecycle rows."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
from dispatch_broker import BrokerError, DispatchBroker
from project_id_migration import run_runtime_coordination_migration
from runtime_coordination import get_connection, init_schema


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "inventory-test")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    state = tmp_path / "state"
    init_schema(state)
    (state / ".vnx-project-id").write_text("inventory-test\n")
    run_runtime_coordination_migration(state / "runtime_coordination.db", default_project_id="inventory-test")
    return DispatchBroker(state, tmp_path / "dispatches", intelligence_enabled=False)


def command(broker, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/bundle_pair_inventory.py"),
                           "--project-id", "inventory-test", *args],
                          env=os.environ.copy(), capture_output=True, text=True)


def test_real_bundles_and_states_are_read_only(broker):
    profiles = [{"provider": "claude", "model": "sonnet"}, {"provider": "codex"}, {}]
    for name, profile in zip(("whole", "partial", "absent"), profiles):
        broker.register(name, "Read the pair", target_profile=profile)
    broker.claim("partial", "T1")
    claim = broker.claim("absent", "T2")
    broker.deliver_start("absent", claim.attempt_id)
    broker.deliver_failure("absent", claim.attempt_id, reason="retry")
    with get_connection(broker._state_dir) as conn:
        conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state) VALUES ('whole-foreign', 'inventory-test-shadow', 'queued')")
        conn.execute("UPDATE dispatches SET bundle_path = '/obsolete/path' WHERE dispatch_id = 'whole'")
        conn.commit()
        before = list(conn.iterdump())
        files = {p: p.read_bytes() for p in broker._dispatch_dir.rglob("*") if p.is_file()}
        result = command(broker, "--json")
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["totals"] == {"whole": 1, "partial": 1, "absent": 1, "error": 0}
        assert [(r["dispatch_id"], r["state"], r["pair_state"]) for r in report["bundles"]] == [
            ("absent", "failed_delivery", "absent"), ("partial", "claimed", "partial"), ("whole", "queued", "whole")]
        assert list(conn.iterdump()) == before
        assert {p: p.read_bytes() for p in broker._dispatch_dir.rglob("*") if p.is_file()} == files
    assert "whole queued whole" in command(broker).stdout


@pytest.mark.parametrize("profile,expected", [(None, "absent"), ({"provider": ""}, "absent"),
    ({"provider": None, "model": "sonnet"}, "partial"), ([], "error"), ({"model": 4}, "error")])
def test_pair_shapes(broker, profile, expected):
    result = broker.register("shape", "Read")
    path = result.bundle_path / "bundle.json"
    bundle = json.loads(path.read_text())
    bundle["target_profile"] = profile
    path.write_text(json.dumps(bundle))
    report = command(broker, "--json")
    assert report.returncode == (1 if expected == "error" else 0)
    assert json.loads(report.stdout)["bundles"][0]["pair_state"] == expected


@pytest.mark.parametrize("damage", ["missing", "corrupt", "directory", "symlink", "version", "identity"])
def test_unreadable_or_invalid_bundle_is_visible(broker, damage):
    path = broker.register("broken", "Read").bundle_path / "bundle.json"
    original = path.with_suffix(".original")
    path.rename(original)
    if damage == "corrupt":
        path.write_text("{")
    elif damage == "directory":
        path.mkdir()
    elif damage == "symlink":
        path.symlink_to(original)
    elif damage in ("version", "identity"):
        bundle = json.loads(original.read_text())
        bundle["bundle_version" if damage == "version" else "dispatch_id"] = "unknown"
        path.write_text(json.dumps(bundle))
    result = command(broker, "--json")
    assert result.returncode == 1
    row = json.loads(result.stdout)["bundles"][0]
    assert row["pair_state"] == "error" and row["error"]


@pytest.mark.parametrize("state", ["ready", "queued", "claimed", "delivering", "recovered", "timed_out", "failed_delivery", "proposed", "accepted", "running", "completed", "expired", "dead_letter", "withdrawn"])
def test_state_selection(broker, state):
    broker.register("state-check", "Read")
    with get_connection(broker._state_dir) as conn:
        conn.execute("UPDATE dispatches SET state = ?", (state,))
        conn.commit()
    rows = broker.claimable_bundle_pairs("inventory-test")
    assert bool(rows) == (state in {"ready", "queued", "claimed", "delivering", "recovered", "timed_out", "failed_delivery"})


@pytest.mark.parametrize("bad_id", ["../escape", "/absolute"])
def test_unsafe_dispatch_id_never_becomes_a_path(broker, bad_id):
    with get_connection(broker._state_dir) as conn:
        conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, 'inventory-test', 'queued')", (bad_id,))
        conn.commit()
    row = broker.claimable_bundle_pairs("inventory-test")[0]
    assert row["pair_state"] == "error" and row["bundle_path"] is None


def test_missing_store_fails_without_creating_it(tmp_path):
    broker = DispatchBroker(tmp_path / "missing", tmp_path / "dispatches", intelligence_enabled=False)
    with pytest.raises(sqlite3.OperationalError):
        broker.claimable_bundle_pairs("inventory-test")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("state", ["", "unknown", 7])
def test_unknown_broker_state_refuses(broker, state):
    broker.register("unknown", "Read")
    with get_connection(broker._state_dir) as conn:
        conn.execute("UPDATE dispatches SET state = ?", (state,))
        conn.commit()
    with pytest.raises(BrokerError, match="Unknown broker state"):
        broker.claimable_bundle_pairs("inventory-test")


@pytest.mark.parametrize("project_id", ["../escape", "/absolute", ""])
def test_invalid_project_refuses_before_store_access(tmp_path, project_id):
    broker = DispatchBroker(tmp_path, tmp_path, intelligence_enabled=False)
    with pytest.raises(ValueError, match="project_id"):
        broker.claimable_bundle_pairs(project_id)
