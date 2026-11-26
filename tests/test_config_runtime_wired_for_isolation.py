"""Config-reader tests must ignore stores wired by earlier tests in the worker."""

import sqlite3

import config_registry
import config_runtime
import test_plan_gate_enforcement as plan_gate_tests


def test_plan_gate_config_ignores_previously_wired_store(tmp_path, monkeypatch):
    # Restore all autowire globals at teardown so this regression cannot leak itself.
    monkeypatch.setattr(config_runtime, "_wired_for", {})
    monkeypatch.setattr(config_runtime, "_wire_status", None)
    monkeypatch.setattr(config_registry, "_db_resolver", None)
    monkeypatch.setattr(config_registry, "_default_project_id", None)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    sqlite3.connect(state_dir / "runtime_coordination.db").close()
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    assert config_runtime.autowire(state_dir)
    assert config_runtime._wired_for[(str(state_dir), "vnx-dev")] is True

    plan_gate_tests.TestEnforceMode().test_config_plane_honored_when_env_unset(monkeypatch)
