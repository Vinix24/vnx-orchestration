"""zelfst PR-7c (ADR-056): the decide, merge and override writers record the derived actor_class.

The class is derived by ``actor_class.derive()`` at the write and never passed in. The decision
also carries its ``delivery_id``; the rotated T0 keeps its orchestrator marker.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "scripts"), str(ROOT / "scripts" / "lib"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import actor_class  # noqa: E402
import coordination_db  # noqa: E402
import governance_escalation  # noqa: E402
import governance_receipts  # noqa: E402
import open_outcomes  # noqa: E402

MARKERS = ("CLAUDE_ROLE", "VNX_TERMINAL", "VNX_TERMINAL_ID", "VNX_DISPATCH_ID",
           "VNX_CURRENT_DISPATCH_ID", "VNX_PROCESS_OWNER")


@pytest.fixture()
def clean_env(monkeypatch):
    for key in MARKERS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(actor_class, "_stdin_isatty", lambda: False)
    return monkeypatch


def _as(monkeypatch, cls):
    if cls == "t0":
        monkeypatch.setenv("CLAUDE_ROLE", "orchestrator")
    elif cls == "worker":
        monkeypatch.setenv("VNX_DISPATCH_ID", "dlv-abc")
    return cls


CLASSES = ["t0", "worker", "unknown"]


def test_decision_has_class_and_delivery(clean_env, tmp_path):
    _as(clean_env, "t0")
    rec = open_outcomes.record_outcome_decision(
        tmp_path / "log.ndjson", dispatch_id="dlv-abc-r2", project_id="p-test", decision="accept")
    assert rec.get("delivery_id") == "dlv-abc"
    assert rec.get("actor_class") == "t0"
    assert rec["project_id"] == "p-test"
    assert json.loads((tmp_path / "log.ndjson").read_text().splitlines()[-1])["delivery_id"] == "dlv-abc"


@pytest.mark.parametrize("cls", CLASSES)
def test_merge_identity_has_class(clean_env, tmp_path, cls):
    _as(clean_env, cls)
    clean_env.setattr(governance_receipts, "_resolve_receipt_project_id", lambda _f: "p-test")
    fields = governance_receipts.dispatch_identity_fields("dlv-abc", str(tmp_path / "r.ndjson"))
    assert fields["actor_class"] == cls
    assert fields["delivery_id"] == "dlv-abc"


@pytest.mark.parametrize("cls", CLASSES)
def test_override_row_carries_class(clean_env, tmp_path, cls):
    _as(clean_env, cls)
    state = tmp_path / "state"
    state.mkdir()
    coordination_db.init_schema(state)
    with coordination_db.get_connection(state) as conn:
        governance_escalation.record_override(
            conn, entity_type="delivery", entity_id="dlv-abc", actor="operator",
            override_type="gate_bypass", justification="one-line fix",
            project_id="p-test", delivery_id="dlv-abc")
        conn.commit()
        row = conn.execute("SELECT metadata_json FROM governance_overrides").fetchone()
    meta = json.loads(row["metadata_json"])
    assert meta["actor_class"] == cls
    assert meta["project_id"] == "p-test" and meta["delivery_id"] == "dlv-abc"


def test_decision_log_reads_with_and_without_new_fields(clean_env, tmp_path):
    log = tmp_path / "log.ndjson"
    old = {"decision_type": open_outcomes.OUTCOME_DECISION_TYPE, "decision": "accept",
           "project_id": "p-test", "dispatch_id": "dlv-old"}
    log.write_text(json.dumps(old) + "\n")
    _as(clean_env, "t0")
    open_outcomes.record_outcome_decision(log, dispatch_id="dlv-new-r1", project_id="p-test", decision="reject")
    read = open_outcomes.read_outcome_decisions(log, "p-test")
    assert set(read) == {"dlv-old", "dlv-new-r1"}
    assert "actor_class" not in read["dlv-old"] and read["dlv-new-r1"]["actor_class"] == "t0"


def test_rotate_launch_exports_role(rig_launch):
    assert "CLAUDE_ROLE=orchestrator" in rig_launch
    assert rig_launch.index("CLAUDE_ROLE=orchestrator") < rig_launch.index("claude --model opus")


@pytest.fixture()
def rig_launch(tmp_path):
    import test_t0_rotate_spawn as spawner

    rig = spawner._rig(tmp_path)
    spawner._handoff(rig, spawner.HANDOFF_BASE)
    assert spawner._spawn(rig).returncode == 0
    return spawner._launch_line(rig)
