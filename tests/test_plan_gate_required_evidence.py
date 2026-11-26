"""OF-5: required evidence refuses; every exception to the guarantee travels with the plan."""
from dataclasses import replace
import sqlite3

import pytest
import dispatch_cli as door
import plan_gate_enforcement as pge
from test_plan_gate_enforcement import _make_db, _spec
from test_dispatch_plan import _make_vspec, _healthy_snapshot
from dispatch_plan import compile_plan


@pytest.mark.parametrize("evidence", ["unsupported", "unreadable"])
def test_required_refuses_evidence(tmp_path, monkeypatch, evidence):
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
    if evidence == "unsupported":
        db = _make_db(tmp_path, tracks={"t": "active"})
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE track_open_items DROP COLUMN resolved_at")
    verdict = door._check_track_link_verdict(_spec("t"), state_dir=tmp_path)
    assert verdict is not None
    assert (verdict.code, verdict.severity) == (f"plan_gate_evidence_{evidence}", "blocking")


@pytest.mark.parametrize("override", ["0", "1"])
def test_unknown_mode_refuses(tmp_path, monkeypatch, override):
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "bogus")
    monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE", override)
    _make_db(tmp_path, tracks={"t": "active"})
    verdict = door._check_track_link_verdict(_spec("t"), state_dir=tmp_path)
    assert verdict is not None
    assert verdict.code == "plan_gate_mode_unknown" and verdict.severity == "blocking"
    assert "bogus" in verdict.message and "VNX_PLAN_GATE_ENFORCE" in verdict.message


@pytest.mark.parametrize("mode", ["required", "advisory", "off"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("evidence", ["db", "read", "schema", None, "", "bogus", {}, "passed", "unresolved"])
def test_evidence_matrix(tmp_path, monkeypatch, mode, override, evidence):
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", mode)
    monkeypatch.setenv("VNX_OVERRIDE_PLAN_GATE", "1" if override else "0")
    if evidence != "db":
        _make_db(tmp_path, tracks={"t": "active"}, with_open_items=evidence != "schema")
    def read(*args):
        if evidence == "read":
            raise OSError("evidence read failed")
        return evidence
    if evidence not in ("db", "schema"):
        monkeypatch.setattr(pge, "plan_gate_state", read)
    waived = []
    verdict = door._check_track_link_verdict(_spec("t"), state_dir=tmp_path, waived_guarantees=waived)
    code = ("plan_gate_evidence_unreadable" if evidence in ("db", "read") else
            "plan-gate-unresolved" if evidence == "unresolved" else "plan_gate_evidence_unsupported")
    if mode == "required" and evidence != "passed":
        assert (verdict.code, verdict.severity) == (code, "warn" if override else "blocking")
        if evidence == "read":
            assert "evidence read failed" in verdict.message
        assert verdict.override_applied == override
        assert ([r for r in waived if r["key"] == "VNX_OVERRIDE_PLAN_GATE"] ==
                ([dict(feature="plan_gate", key="VNX_OVERRIDE_PLAN_GATE", value="1", source="override", reason=code)] if override else []))
    else:
        expected = "tracks-db-unavailable" if evidence == "db" else "plan-gate-unresolved" if evidence == "unresolved" and mode == "advisory" else None
        assert (verdict.code if verdict else None) == expected
        assert verdict is None or verdict.severity == "warn"
        assert not any(r["key"] == "VNX_OVERRIDE_PLAN_GATE" for r in waived)


@pytest.mark.parametrize("mode", ["required", "advisory", "off"])
@pytest.mark.parametrize("escape", [False, True])
def test_unlinked_plan_and_receipt(tmp_path, monkeypatch, mode, escape):
    from dispatch_internal import issue_permit, require_permit
    from report_to_receipt_converter import _load_route_decision
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", mode)
    monkeypatch.setenv("VNX_REQUIRE_DISPATCH_TRACK", "1" if escape else "0")
    waived = []
    spec = replace(_spec(None), tags=("no-track:research",) if escape else ())
    verdict = door._check_track_link_verdict(spec, state_dir=tmp_path, waived_guarantees=waived)
    assert (verdict.code if verdict else None) == (None if escape else "track_unlinked")
    assert dict(feature="plan_gate", key="track_id", value="none", source="no-track:research" if escape else "track_unlinked",
                guarantee="this build hangs on no plan") in waived
    assert any(r["key"] == "VNX_PLAN_GATE_ENFORCE" for r in waived) == (mode != "required")
    plan = compile_plan(_make_vspec(tmp_path=tmp_path), replace(_healthy_snapshot(), waived_guarantees=tuple(waived)))
    assert list(plan.waived_guarantees) == waived
    assert replace(plan, waived_guarantees=()).digest() != plan.digest()
    with pytest.raises(PermissionError):
        require_permit(replace(plan, waived_guarantees=()), issue_permit(plan))
    door._persist_route_decision(plan, issue_permit(plan), state_dir=tmp_path)
    assert _load_route_decision(plan.dispatch_id, tmp_path)["decision"]["waived_guarantees"] == waived


def test_unreadable_mode_refuses_at_door(tmp_path, monkeypatch):
    import config_runtime
    def unreadable(*args):
        raise OSError("config unreadable")
    monkeypatch.setattr(config_runtime, "strict_resolver", lambda: (unreadable, "vnx-dev"))
    verdict = door._check_track_link_verdict(_spec("t"), state_dir=tmp_path)
    assert (verdict.code, verdict.severity) == ("plan_gate_mode_unreadable", "blocking")
