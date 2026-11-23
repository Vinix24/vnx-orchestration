"""Characterisation of ``run_dispatch`` (golf schoon R29b): call order, worker env, stderr, logger lines
and exit codes of the moved blocks, collaborators replaced on ``dispatch_cli`` (where it looks them up)."""
from __future__ import annotations

import contextlib
from collections import namedtuple as NT
import dataclasses
import logging
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli  # noqa: E402
import dispatch_exit  # noqa: E402
from dispatch_serialization import LockWaitTimeout  # noqa: E402

_ENV = ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO",
        "VNX_WORK_REF", "VNX_PR_ID", "VNX_CURRENT_MODEL", "VNX_CURRENT_PROVIDER", "VNX_CURRENT_TERMINAL")
_FULL_ENV = dict(zip(_ENV, ("parent-1", "03_refactoring", "t-a", "t-b", "dispatch/x", "2899", "opus", "claude", "T9")))
_PLAN = {"dispatch_id": "d-r29b", "lane": "claude_headless", "route_reason": "plan-reason", "warnings": (),
         "parent_dispatch": "parent-1", "task_class": "03_refactoring", "tier_from": "t-a", "tier_to": "t-b",
         "work_ref": "dispatch/x", "pr_id": "2899", "model": "opus", "provider": NT("Prov", "value")("claude"),
         "target_id": "T9", "serialization_class": "claude"}
_Plan = dataclasses.make_dataclass("_Plan", [(k, object, dataclasses.field(default=v)) for k, v in _PLAN.items()],
                                   frozen=True)


def _door(mp, tmp_path, *, plan=None, track_id="trk", scout_exc=None, **over):
    calls, sd = [], tmp_path / "state"
    spec = NS(dispatch_id="d-r29b", dispatch_paths=[NS(path="a.py")], task_class="03_refactoring",
              track_id=track_id, role="backend-developer")
    vspec = NS(spec=spec, instruction_text="do it")
    def rec(name, result=None):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            if isinstance(result, BaseException):
                raise result
            return result
        return _fn

    @contextlib.contextmanager
    def serialize(cls, *, dispatch_id):
        calls.append(("lane_slot", (cls, dispatch_id), {}))
        yield
    for name in _ENV:
        mp.delenv(name, raising=False)
    fns = {"_resolve_door_authority": rec("authority", ("vnx-dev", tmp_path, tmp_path / "data", sd)),
           "_load_spec_with_track_override": rec("load", "S"), "_refire_guard_reject": rec("refire"),
           "_route_provider_pre_validate": rec("route", (None, "S", "door")), "validate": rec("validate", vspec),
           "_fill_gate_and_check_required": rec("gate", (None, vspec, "door2")),
           "build_runtime_snapshot": rec("snapshot", NS(worker_claude_override_reason="ovr")),
           "_compile_plan_checked": rec("compile", plan or _Plan()),
           "_headless_isolation_guard": rec("iso", "iso-warn"), "_lane_verdict": rec("lane"), "_blast_radius_reject": rec("blast"),
           "_record_bookkeeping_failure": rec("bk_fail"), "_persist_dispatch_row": rec("row", "att-1"),
           "_register_gate_obligation": rec("obligation"), "_door_track_bookkeeping": rec("track"),
           "issue_permit": rec("permit", "P"), "require_permit": rec("require"),
           "fingerprint": rec("fp", "fp-1"), "_commit_fire": rec("commit", False), "_print_plan": rec("print"),
           "_door_lifecycle_sweep": rec("sweep"), "_owner_hand_over": rec("hand_over", ("H", "base")),
           "serialize_lane": serialize, "_maybe_stage_escalation": rec("escalate"),
           "run_envelope_plan": rec("envelope", NS(status="success", error=None, returncode=0)),
           "_owner_finish": rec("finish"), "_execute_claude_headless": rec("headless", 0),
           "_finalize_door_exit": rec("finalize")}
    fns.update({k: (v if callable(v) else rec(k, v)) for k, v in over.items()})
    for name, fn in fns.items():
        mp.setattr(dispatch_cli, name, fn)
    mp.setitem(sys.modules, "scout_prepass", types.ModuleType("scout_prepass"))
    sys.modules["scout_prepass"].maybe_run_scout = rec("scout", scout_exc)
    return calls, sd


def _run(**kw):
    return dispatch_cli.run_dispatch(Path("/x/spec.json"), **kw)


def _get(calls, name):
    return next(c for c in calls if c[0] == name)


def _env():  # the worker env run_dispatch exported, minus the unset names
    return {n: os.environ[n] for n in _ENV if os.environ.get(n)}


def test_headless_fire_order_env_and_output(monkeypatch, tmp_path, capsys, caplog):
    calls, sd = _door(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO, logger=dispatch_cli.logger.name)
    assert _run(refire_reason="rf") == 0
    assert [c[0] for c in calls][9:] == ["lane", "blast", "scout", "row", "obligation", "track", "permit", "require",
                                         "fp", "commit", "sweep", "hand_over", "lane_slot", "headless", "finish"]
    assert _env() == _FULL_ENV
    assert _get(calls, "scout")[2] == {"dispatch_id": "d-r29b", "instruction_text": "do it",
                                       "dispatch_paths": ["a.py"], "state_dir": sd,
                                       "task_class": "03_refactoring", "lane": "claude_headless"}
    plan = _get(calls, "permit")[1][0]
    assert (plan.route_reason, plan.warnings) == ("door2;plan-reason", ("iso-warn",))
    assert _get(calls, "commit")[2]["headless_isolation_warning"] == "iso-warn" and _get(calls, "lane_slot")[1] == (
        "claude", "d-r29b")
    assert _get(calls, "headless")[2] == {"state_dir": sd, "data_dir": tmp_path / "data",
                                          "role": "backend-developer", "handover": "H", "base_sha": "base"}
    assert _get(calls, "finish")[1:] == (("d-r29b", "att-1"), {"state_dir": sd, "success": True, "failure_reason": None})
    assert capsys.readouterr().err == "[dispatch_cli] [WARN] iso-warn\n"
    assert [r.getMessage() for r in caplog.records] == ["[dispatch_cli] permit fingerprint: fp-1", "[dispatch_cli] iso-warn"]


def test_headless_nonzero_bare_env_gate_reject_scout_failure(monkeypatch, tmp_path, capsys):
    bare = _Plan(parent_dispatch=None, task_class=None, tier_from=None, tier_to=None, work_ref=None,
                 pr_id=None, provider=NT("Prov", "value")(""), target_id=None)
    calls, _ = _door(monkeypatch, tmp_path, plan=bare, track_id=None, _headless_isolation_guard=None,
                     _execute_claude_headless=7)
    assert _run() == 7 and _env() == {"VNX_CURRENT_MODEL": "opus"} and "track" not in [c[0] for c in calls]
    assert _get(calls, "finish")[2] == {"state_dir": tmp_path / "state", "success": False,
                                        "failure_reason": "claude_headless lane returned non-zero"}
    _door(monkeypatch, tmp_path, _register_gate_obligation=NS(code="obl", message="none"))
    assert _run() == 1 and _env() == {}
    assert capsys.readouterr().err == "[dispatch_cli] REJECT [obl]: none\n"
    boom = RuntimeError("scout down")
    calls, sd = _door(monkeypatch, tmp_path, scout_exc=boom)
    assert _run() == 0 and [c[0] for c in calls][11:14] == ["scout", "bk_fail", "row"]
    assert _get(calls, "bk_fail")[1:] == (("scout_prepass", "d-r29b", boom), {"state_dir": sd})


def test_provider_lanes_and_dry_run(monkeypatch, tmp_path, capsys):
    calls, _ = _door(monkeypatch, tmp_path, plan=_Plan(lane="provider", provider="kimi"),
                     run_envelope_plan=NS(status="failed", error=None, returncode=3))
    assert _run() == 3 and os.environ["VNX_CURRENT_PROVIDER"] == "kimi"
    assert [c[0] for c in calls][-3:] == ["run_envelope_plan", "escalate", "finish"]
    assert _get(calls, "finish")[2]["failure_reason"] == "provider lane status=failed"
    assert capsys.readouterr().err == "[dispatch_cli] provider lane failed: (no error captured)\n"
    calls, _ = _door(monkeypatch, tmp_path, plan=_Plan(lane="provider"))
    assert _run() == 0 and "escalate" not in [c[0] for c in calls]
    assert _get(calls, "envelope")[2]["base_sha"] == "base" and _get(calls, "finish")[2]["success"] is True
    assert capsys.readouterr().err == ""
    calls, _ = _door(monkeypatch, tmp_path, _fill_gate_and_check_required=lambda v, r: (None, v, ""),
                     _headless_isolation_guard=None)
    assert _run(dry_run=True) == 0 and _env() == {}
    assert [c[0] for c in calls][8:] == ["lane", "blast", "permit", "require", "fp", "print"]
    assert (_get(calls, "print")[1][0].route_reason, _get(calls, "print")[1][0].warnings) == ("plan-reason", ())


def test_invariant_and_lock_wait_exits(monkeypatch, tmp_path, capsys, caplog):
    caplog.set_level(logging.ERROR, logger=dispatch_cli.logger.name)
    calls, _ = _door(monkeypatch, tmp_path, require_permit=PermissionError("nope"))
    assert _run() == 1 and _get(calls, "finalize")[1:] == ((None,), {
        "state_dir": tmp_path / "state", "reason": dispatch_exit.REASON_PRE_SPAWN_ERROR})
    calls, _ = _door(monkeypatch, tmp_path, plan=_Plan(lane="mystery"))
    assert _run() == 1 and [c[0] for c in calls][-2:] == ["lane_slot", "finalize"] and calls[-1][1] == ("H",)
    assert capsys.readouterr().err == (
        "[dispatch_cli] REJECT [invariant-violation]: permit invariant breached: nope\n"
        "[dispatch_cli] REJECT [invariant-violation]: closed set violated — unknown lane: 'mystery'\n")
    assert [r.getMessage() for r in caplog.records] == [
        "[dispatch_cli] INVARIANT VIOLATION dispatch=d-r29b: permit invariant breached: nope",
        "[dispatch_cli] INVARIANT VIOLATION dispatch=d-r29b: closed set violated — unknown lane: 'mystery'"]
    caplog.clear()
    calls, sd = _door(monkeypatch, tmp_path, serialize_lane=LockWaitTimeout("slot busy"))
    assert _run() == 1 and [c[0] for c in calls][-3:] == ["serialize_lane", "finalize", "finish"]
    assert _get(calls, "finalize")[1:] == (("H",), {"state_dir": sd, "reason": dispatch_exit.REASON_SLOT_TIMEOUT})
    assert _get(calls, "finish")[1:] == (("d-r29b", "att-1"), {
        "state_dir": sd, "success": False, "failure_reason": "lock-wait timeout: slot busy"})
    assert capsys.readouterr().err == "[dispatch_cli] REJECT [lock-wait-timeout]: slot busy\n"
    assert [r.getMessage() for r in caplog.records] == ["[dispatch_cli] LOCK-WAIT TIMEOUT dispatch=d-r29b: slot busy"]


def test_blast_radius_refusal_stops_the_door_before_scout_permit_and_lane(monkeypatch, tmp_path):
    calls, _ = _door(monkeypatch, tmp_path, _blast_radius_reject=lambda *a: calls.append(("blast", a, {})) or dispatch_cli.Reject("c", "m"))
    assert _run() == 1 and [c[0] for c in calls][-2:] == ["lane", "blast"] and _get(calls, "blast")[1][1:] == (tmp_path, tmp_path / "state", None, False)
    assert not {"scout", "row", "permit", "commit", "lane_slot", "headless", "envelope"} & {c[0] for c in calls}
