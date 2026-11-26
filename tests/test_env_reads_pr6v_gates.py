"""config-een-oppervlak PR-6v-b1: the plan gate and the evidence-bound gate read through config_runtime.

A stored project_config row beats an exported env value for VNX_PLAN_GATE_ENFORCE;
VNX_OVERRIDE_<NAME> still beats the row. VNX_EVIDENCE_BOUND_GATE reads a stored row.
Unknown plan-gate values refuse; unknown evidence-gate values read ``off``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from optional_features import OptionalFeatureRefused

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import config_registry  # noqa: E402
import config_runtime  # noqa: E402
import config_store_db  # noqa: E402
import evidence_bound_gate  # noqa: E402
import plan_gate_enforcement  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real store wired for project pr6v; returns a function that writes one row."""
    sd = tmp_path / "state"
    sd.mkdir()
    for key in ("VNX_PLAN_GATE_ENFORCE", "VNX_EVIDENCE_BOUND_GATE"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_" + key[4:], raising=False)
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", "pr6v")
    config_runtime._wired_for.clear()

    def put(key, value):
        conn = sqlite3.connect(sd / "runtime_coordination.db")
        config_store_db.set_config(conn, "pr6v", key, value, actor="op", approval_id="pr6v")
        conn.close()

    yield put
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    config_runtime._wired_for.clear()


def test_plan_gate_stored_row_beats_env(store, monkeypatch):
    store("VNX_PLAN_GATE_ENFORCE", "required")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "advisory")
    assert plan_gate_enforcement.enforce_mode() == "required"


def test_plan_gate_override_beats_stored_row(store, monkeypatch):
    store("VNX_PLAN_GATE_ENFORCE", "required")
    monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE_ENFORCE", "advisory")
    assert plan_gate_enforcement.enforce_mode() == "advisory"


def test_evidence_gate_reads_stored_row_without_env(store):
    store("VNX_EVIDENCE_BOUND_GATE", "required")
    assert evidence_bound_gate.gate_state() == "required"


def test_evidence_gate_stored_row_beats_env(store, monkeypatch):
    store("VNX_EVIDENCE_BOUND_GATE", "required")
    monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "advisory")
    assert evidence_bound_gate.gate_state() == "required"


def test_evidence_gate_override_beats_stored_row(store, monkeypatch):
    store("VNX_EVIDENCE_BOUND_GATE", "required")
    monkeypatch.setenv("VNX_OVERRIDE_EVIDENCE_BOUND_GATE", "advisory")
    assert evidence_bound_gate.gate_state() == "advisory"


@pytest.mark.parametrize("source", ["env", "row"])
def test_unknown_value_refuses_plan_gate(store, monkeypatch, source):
    for key in ("VNX_PLAN_GATE_ENFORCE", "VNX_EVIDENCE_BOUND_GATE"):
        if source == "env":
            monkeypatch.setenv(key, "enforce-maybe")
        else:
            store(key, "enforce-maybe")
    with pytest.raises(OptionalFeatureRefused, match="plan_gate_mode_unknown"):
        plan_gate_enforcement.enforce_mode()
    assert evidence_bound_gate.gate_state() == "off"
