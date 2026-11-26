"""Characterisation of ``build_runtime_snapshot`` (golf schoon R29, dlv-df171737df2b), green
before and after the extract-method; only the constraint engine, pins, classifier, project-state
and stop verdicts and the git lag probe are replaced."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli  # noqa: E402
from dispatch_plan import ConstraintVerdict, ModelPin  # noqa: E402
from dispatch_spec import Reject, validate  # noqa: E402

_PINS = {"T1": ModelPin(model="sonnet", semantics="floor"), "T0": ModelPin(model="opus", semantics="default")}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV, raising=False)
    monkeypatch.delenv(dispatch_cli.WORKER_CLAUDE_OVERRIDE_REASON_ENV, raising=False)


def _snapshot(tmp_path, monkeypatch, *, violations=(), raise_exc=None, lag=0,
              classify="02_testing", target_slot="T1", model=None, **extra):
    data_dir = tmp_path / "vnx-data"
    bundle = data_dir / "dispatches" / "pending" / "20261114-r29-staging"
    bundle.mkdir(parents=True)
    inst = bundle / "instruction.md"
    inst.write_text("# R29 probe\n\nRole: backend-developer\n\nDo one thing.\n", encoding="utf-8")
    spec = {"schema_version": 1, "project_id": "vnx-dev", "dispatch_id": "20261114-r29-dispatch",
            "staging_id": "20261114-r29-staging", "instruction_file": str(inst),
            "role": "backend-developer", "target_slot": target_slot, "gate": "codex_gate",
            "dispatch_paths": [], "provider": "claude", "model": model,
            "deadline_seconds": 3600, "isolation": "worktree", **extra}
    spec_file = bundle / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    vspec = validate(dispatch_cli.load_spec(spec_file), project_id="vnx-dev",
                     repo_root=dispatch_cli._resolve_repo_root())
    assert not isinstance(vspec, Reject), vspec
    calls = []

    def _check(**kw):
        calls.append(kw)
        if raise_exc is not None:
            raise raise_exc
        return list(violations) if kw.get("check_registry") else []

    with patch("providers.constraint_enforcer.check_constraints", _check), \
         patch.object(dispatch_cli, "_load_model_pins_from_yaml", return_value=dict(_PINS)), \
         patch.object(dispatch_cli, "main_checkout_lag", return_value=lag) as lag_probe, \
         patch.object(dispatch_cli, "_project_state_verdicts", return_value=()), \
         patch.object(dispatch_cli, "_check_stop_conditions_verdict", return_value=None), \
         patch("smart_router.classify_task", return_value=classify):
        snap = dispatch_cli.build_runtime_snapshot(vspec, data_dir=data_dir, spec_file=spec_file)
    assert calls and lag_probe.called
    return snap, calls


def test_floor_pin_and_chain_fields(tmp_path, monkeypatch):
    snap, calls = _snapshot(tmp_path, monkeypatch, model="opus")
    assert (calls[0]["model"], calls[0]["via"], calls[0]["terminal_id"]) == ("opus", "headless", "T1")
    assert calls[0]["check_registry"] is True
    assert snap.constraint_verdicts == () and snap.staging_promoted is True
    lane = dispatch_cli.HEADLESS_LANE_TERMINAL
    assert (snap.target_health, snap.target_capable) == ({lane: "healthy"}, {lane: True})
    assert snap.model_pins == _PINS and snap.task_class == "02_testing"
    assert snap.parent_dispatch is None and snap.tier_from is None
    assert snap.worker_claude_override_reason is None
    assert snap.repo_root == dispatch_cli._resolve_repo_root()


def test_default_pin_lets_spec_model_win(tmp_path, monkeypatch):
    _, calls = _snapshot(tmp_path, monkeypatch, target_slot="T0", model="haiku")
    assert calls[0]["model"] == "haiku"


def test_explicit_task_class_skips_classifier(tmp_path, monkeypatch):
    snap, _ = _snapshot(tmp_path, monkeypatch, task_class="03_refactoring", classify="never")
    assert snap.task_class == "03_refactoring"


def test_registry_unavailable_is_blocking(tmp_path, monkeypatch):
    snap, _ = _snapshot(tmp_path, monkeypatch, raise_exc=RuntimeError("boom"))
    assert snap.constraint_verdicts[0] == ConstraintVerdict(
        code="registry-unavailable", severity="blocking",
        message="Constraint registry unavailable — fail-closed: boom")


def test_violations_keep_order(tmp_path, monkeypatch):
    v = [NS(code="a", severity="warn", message="m1", override_applied=False),
         NS(code="b", severity="blocking", message="m2", override_applied=True)]
    snap, _ = _snapshot(tmp_path, monkeypatch, violations=v)
    assert [(x.code, x.severity, x.message, x.override_applied) for x in snap.constraint_verdicts] \
        == [("a", "warn", "m1", False), ("b", "blocking", "m2", True)]


def test_override_without_reason_prepends_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV, "1")
    v = [NS(code="x", severity="warn", message="m", override_applied=False)]
    snap, calls = _snapshot(tmp_path, monkeypatch, model="opus", violations=v)
    assert [x.code for x in snap.constraint_verdicts] == ["worker-claude-override-reason-required", "x"]
    assert snap.constraint_verdicts[0].severity == "blocking" and calls[0]["model"] == "opus"
    assert snap.model_pins == _PINS


def test_override_with_reason_appends_audit_and_strips_pin(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(dispatch_cli.WORKER_CLAUDE_OVERRIDE_ENV, "1")
    monkeypatch.setenv(dispatch_cli.WORKER_CLAUDE_OVERRIDE_REASON_ENV, "  why  ")
    caplog.set_level("WARNING", logger=dispatch_cli.logger.name)
    snap, calls = _snapshot(tmp_path, monkeypatch, model="opus")
    last = snap.constraint_verdicts[-1]
    assert calls[0]["model"] == "opus" and snap.worker_claude_override_reason == "why"
    assert (last.code, last.severity, last.override_applied) == ("worker-claude-override-applied", "warn", True)
    assert last.message.endswith("Reason: why") and "T1" not in snap.model_pins and "T0" in snap.model_pins
    assert any("AUDIT worker-claude-override-applied target=T1 model=opus reason=why"
               in r.getMessage() for r in caplog.records)


def test_lag_blocks_post_merge_verification_last(tmp_path, monkeypatch):
    snap, _ = _snapshot(tmp_path, monkeypatch, lag=3, post_merge_verification=True)
    assert snap.constraint_verdicts[-1] == dispatch_cli._post_merge_verification_lag_verdict(3)


def test_lag_ignored_for_build(tmp_path, monkeypatch):
    assert _snapshot(tmp_path, monkeypatch, lag=3)[0].constraint_verdicts == ()
