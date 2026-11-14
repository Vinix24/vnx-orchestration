"""Characterisation of ``provider_dispatch._emit_governance`` and ``main`` (golf schoon R37).

Written and run green on the old code first; the extract-method refactor keeps them green.
The real receipt and report writers run against tmp state/data dirs; only the spawn
handlers, the env loader and the operator switch/constraint readers are patched.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import governance_emit
import provider_dispatch as pd

_RECEIPT_KWARG_ORDER = """dispatch_id terminal_id provider model pr_id status completion_pct risk findings
duration_seconds token_usage cost_usd cost_source cost_ledger_run_ids token_usage_normalized state_dir report_path
events_path permission_enforcement permission_posture permission_profile permission_allow_pattern_count mandate_id
final_prompt_path final_prompt_sha256 injection_reconstructs verification role receipt_kind role_applied role_tier
role_not_applied_reason role_source_path session_id tool_call_count tool_call_failures tool_call_retries
deadline_seconds failure_reason failure_class delivery_state""".split()


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    for name in ("state", "data"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    for var in ("VNX_CONSTRAINTS_STRICT", "VNX_UNIFIED_ENVELOPE", "VNX_UNIFIED_ENVELOPE_LANES",
                "VNX_SIGNED_DELEGATION", "VNX_SESSION_ID", "VNX_BENCH_SEED_MATERIALIZE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _receipts(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "t0_receipts.ndjson"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []


def _Result(error=None, returncode=0):
    return SimpleNamespace(error=error, completion_text="done", timed_out=False, returncode=returncode,
                           token_usage={"input": 3, "output": 4}, session_id=None)


def _args(**kw):
    base = dict(dispatch_id="r37-emit", terminal_id="T1", pr_id="PR-9", instruction="Reply OK.",
                model="sonnet", provider="kimi", role="backend-developer", mandate_id="m-1",
                deadline_seconds=600)
    base.update(kw)
    return argparse.Namespace(**base)


def _emit(args, status="success", result=None, **kw):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    pd._emit_governance(args, "kimi", "kimi-k2", result or _Result(), now, now, status, **kw)


def test_receipt_kwargs_and_side_effect_order(dirs, capsys):
    order: list[str] = []
    real_receipt, real_report = governance_emit.emit_dispatch_receipt, governance_emit.emit_unified_report
    store = SimpleNamespace(archive=lambda t, d: order.append("archive") or Path("/tmp/archived.ndjson"),
                            clear=lambda t, dispatch_id=None: order.append("clear"))
    args = _args()
    args._final_prompt_integrity = SimpleNamespace(final_prompt_path="/p", final_prompt_sha256="abc",
                                                   injection_reconstructs=True)
    args._role_application = SimpleNamespace(role_applied=False, tier="t2", reason="missing", source_path="/r.md")
    posture = {"permission_posture": "scoped", "permission_profile": "be", "permission_allow_pattern_count": 4}
    with patch("governance_emit.emit_dispatch_receipt",
               side_effect=lambda **kw: order.append("receipt") or real_receipt(**kw)) as m_rec, \
            patch("governance_emit.emit_unified_report",
                  side_effect=lambda **kw: order.append("report") or real_report(**kw)), \
            patch.object(pd, "_application_verdicts", side_effect=lambda a, e, r, app: (app, None)):
        _emit(args, event_store=store, permission_posture=posture)
    assert order == ["archive", "receipt", "clear", "report"]
    kw = m_rec.call_args.kwargs
    assert list(kw) == _RECEIPT_KWARG_ORDER
    assert (kw["permission_posture"], kw["permission_profile"], kw["permission_allow_pattern_count"]) == ("scoped", "be", 4)
    assert (kw["final_prompt_path"], kw["final_prompt_sha256"], kw["injection_reconstructs"]) == ("/p", "abc", True)
    assert (kw["role_applied"], kw["role_tier"], kw["role_not_applied_reason"], kw["role_source_path"]) == \
        (False, "t2", "missing", "/r.md")
    assert kw["events_path"] == "/tmp/archived.ndjson"
    assert kw["verification"]["method"] == "pending-report"
    assert (kw["mandate_id"], kw["deadline_seconds"], kw["pr_id"], kw["completion_pct"]) == ("m-1", 600, "PR-9", 100)
    assert (kw["failure_reason"], kw["failure_class"], kw["receipt_kind"]) == (None, None, "dispatch")
    err = capsys.readouterr().err
    assert err.index("Receipt: ") < err.index("Report: ")
    assert (dirs / "data" / "unified_reports" / "r37-emit.md").exists()


def test_no_posture_no_integrity_no_role_app_stamps_none(dirs):
    with patch("governance_emit.emit_dispatch_receipt", wraps=governance_emit.emit_dispatch_receipt) as m_rec:
        _emit(_args(dispatch_id="r37-none"))
    kw = m_rec.call_args.kwargs
    for key in ("permission_posture", "permission_profile", "permission_allow_pattern_count", "final_prompt_path",
                "final_prompt_sha256", "injection_reconstructs", "role_applied", "role_tier",
                "role_not_applied_reason", "role_source_path", "events_path"):
        assert kw[key] is None, key
    assert len(_receipts(dirs)) == 1


def test_failure_classified_and_passed_to_outcome(dirs):
    with patch("failure_classification.classify_failure_safe",
               return_value={"failure_reason": "boom-reason", "failure_class": "auth"}) as m_cls, \
            patch.object(pd, "_record_dispatch_outcome") as m_out:
        _emit(_args(dispatch_id="r37-fail"), status="failure", result=_Result(error="boom", returncode=1))
    assert m_cls.call_args.kwargs["dispatch_id"] == "r37-fail"
    assert m_out.call_args.kwargs["failure_reason"] == "boom-reason"
    assert m_out.call_args.kwargs["failure_class"] == "auth"
    receipt = _receipts(dirs)[-1]
    assert receipt["failure_class"] == "auth" and receipt["completion_pct"] == 0


def test_classification_unavailable_stamps_unknown(dirs):
    with patch("failure_classification.classify_failure_safe", side_effect=ImportError("gone")), \
            patch.object(pd, "_record_dispatch_outcome") as m_out:
        _emit(_args(dispatch_id="r37-unk"), status="failure", result=_Result(error="x", returncode=1))
    assert m_out.call_args.kwargs["failure_class"] == "unknown"
    assert m_out.call_args.kwargs["failure_reason"] == \
        "failure_classification unavailable: ImportError('gone') (dispatch=r37-unk)"


def test_classification_reruns_per_retry_attempt(dirs):
    with patch("governance_emit.emit_dispatch_receipt", side_effect=[RuntimeError("lock"), "r.ndjson"]) as m_rec, \
            patch.object(pd.time, "sleep") as m_sleep, \
            patch("failure_classification.classify_failure_safe",
                  return_value={"failure_reason": "r", "failure_class": "c"}) as m_cls:
        _emit(_args(dispatch_id="r37-retry"), status="failure", result=_Result(error="x", returncode=1))
    assert m_cls.call_count == 2 and m_rec.call_count == 2
    m_sleep.assert_called_once_with(pd._EMIT_RETRY_DELAY * 1)


def test_value_error_reraises_without_report(dirs):
    with patch("governance_emit.emit_dispatch_receipt", side_effect=ValueError("bad provider")), \
            patch("governance_emit.emit_unified_report") as m_rep, pytest.raises(ValueError):
        _emit(_args(dispatch_id="r37-val"))
    m_rep.assert_not_called()


@pytest.fixture
def main_env(dirs):
    with patch("env_loader.load_env"), patch.object(pd, "_provider_switch_refusal", return_value=None), \
            patch.object(pd, "_check_constraints") as m_constraints, \
            patch("paid_lane_budget.enforce_daily_budget") as m_budget:
        yield dirs, m_constraints, m_budget


def _argv(provider="codex", dispatch_id="r37-main"):
    return ["--provider", provider, "--terminal-id", "T1", "--dispatch-id", dispatch_id,
            "--instruction", "noop", "--model", "gpt-5"]


def test_main_routes_codex_plain_and_envelope(main_env, monkeypatch):
    with patch.object(pd, "_dispatch_codex", return_value=7), patch.object(pd, "_dispatch_codex_via_envelope",
                                                                           return_value=8):
        assert pd.main(_argv()) == 7
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE", "1")
        monkeypatch.setenv("VNX_UNIFIED_ENVELOPE_LANES", " kimi , codex ")
        assert pd.main(_argv()) == 8
    main_env[2].assert_called_with("codex", state_dir=pd._resolve_state_dir())


def test_main_unknown_provider_exits_2(main_env):
    with pytest.raises(SystemExit) as exc:
        pd.main(_argv(provider="nope"))
    assert exc.value.code == 2


def test_main_constraints_missing_strict_and_lenient(main_env, monkeypatch, capsys):
    main_env[1].side_effect = FileNotFoundError("yaml")
    with patch.object(pd, "_dispatch_codex", return_value=5):
        assert pd.main(_argv()) == 5
        monkeypatch.setenv("VNX_CONSTRAINTS_STRICT", "1")
        assert pd.main(_argv()) == 1
    assert "provider_constraints.yaml not found and VNX_CONSTRAINTS_STRICT=1" in capsys.readouterr().err


def test_main_constraint_violation_refusal_receipt(main_env, capsys):
    from providers.constraint_enforcer import ConstraintViolationError

    exc = ConstraintViolationError.__new__(ConstraintViolationError)
    RuntimeError.__init__(exc, "[C1] too big")
    main_env[1].side_effect = exc
    assert pd.main(_argv(dispatch_id="r37-cv")) == 1
    assert "provider_dispatch: constraint violation - [C1] too big" in capsys.readouterr().err
    assert _receipts(main_env[0])[-1]["failure_reason"].startswith("constraint violation: [C1] too big")


def test_main_other_constraint_exception_raises(main_env):
    main_env[1].side_effect = KeyError("x")
    with pytest.raises(KeyError):
        pd.main(_argv())


def test_main_delegation_without_anchor_rejects(main_env, capsys):
    with patch("delegation_mandate.is_signed_delegation_enabled", return_value=True), \
            patch("delegation_mandate.resolve_allowed_signers_for_runtime", return_value=None), \
            patch.object(pd, "_dispatch_codex") as m_codex:
        assert pd.main(_argv(dispatch_id="r37-anchor")) == 1
    m_codex.assert_not_called()
    assert "REJECT: VNX_SIGNED_DELEGATION=1 but no" in capsys.readouterr().err
    assert "trust anchor found" in _receipts(main_env[0])[-1]["failure_reason"]


def test_main_delegation_gate_fail_and_pass(main_env, capsys):
    seen = {}
    with patch("delegation_mandate.is_signed_delegation_enabled", return_value=True), \
            patch("delegation_mandate.resolve_allowed_signers_for_runtime", return_value=Path("/sig")), \
            patch("delegation_mandate.resolve_signed_delegation", return_value=(False, None, "expired")), \
            patch.object(pd, "_dispatch_codex", side_effect=lambda a: seen.setdefault("m", a.mandate_id) and 0):
        assert pd.main(_argv(dispatch_id="r37-gate")) == 1
        assert "signed-delegation gate failed: expired" in capsys.readouterr().err
        with patch("delegation_mandate.resolve_signed_delegation", return_value=(True, "mand-7", "ok")) as m_res:
            assert pd.main(_argv(dispatch_id="r37-gate2")) == 0
    assert seen["m"] == "mand-7"
    ctx = m_res.call_args.args[0]
    assert (ctx.dispatch_id, ctx.project_id) == ("r37-gate2", "data")


def test_main_budget_exceeded_refuses(main_env, capsys):
    from paid_lane_budget import PaidLaneBudgetExceededError

    main_env[2].side_effect = PaidLaneBudgetExceededError("codex", 2.0, 1.0)
    with patch.object(pd, "_dispatch_codex") as m_codex:
        assert pd.main(_argv(dispatch_id="r37-budget")) == 1
    m_codex.assert_not_called()
    assert capsys.readouterr().err.startswith("provider_dispatch: ")
    assert _receipts(main_env[0])[-1]["dispatch_id"] == "r37-budget"


def test_main_spawn_exception_records_deliver_failed(main_env):
    with patch.object(pd, "_dispatch_codex", side_effect=OSError("spawn")):
        assert pd.main(_argv(dispatch_id="r37-spawn")) == 1
    receipt = _receipts(main_env[0])[-1]
    assert receipt["delivery_state"] == pd._DELIVERY_STATE_DELIVER_FAILED
    assert receipt["status"] == "failure"
    assert "uncaught spawn exception: OSError('spawn')" in receipt["failure_reason"]


def test_main_switched_off_at_launch(main_env):
    import provider_switch

    err = provider_switch.ProviderDisabledError.__new__(provider_switch.ProviderDisabledError)
    err.decision = "decision"
    with patch.object(pd, "_dispatch_kimi", side_effect=err), \
            patch.object(pd, "_refuse_switched_off", return_value=42) as m_ref:
        assert pd.main(_argv(provider="kimi")) == 42
    assert m_ref.call_args.args[1:] == ("kimi", "decision")
