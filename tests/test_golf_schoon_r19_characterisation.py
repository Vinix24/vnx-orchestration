"""Characterisation tests for golf schoon R19 (dlv-adae69fd025e).

Pin the observable behaviour of ``FPCCertificationChecksMixin._certify_task_class_routing``
(rows, order, ``VNX_HEADLESS_ROUTING`` restore) and ``governance_emit.emit_dispatch_receipt``
(ledger line, chain-link env fallback, token harvest, model normalisation, exception types,
confidence update only for an appended receipt) across the extract-method refactor.
Real temporary dirs; only the transcript harvest and failure injection points are patched.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import governance_emit
from append_receipt_internals.common import AppendReceiptError
from fpc_certification import FPCCertificationRunner
from runtime_coordination import init_schema

_CHAIN_ENV = ("VNX_PARENT_DISPATCH", "VNX_TASK_CLASS", "VNX_TIER_FROM", "VNX_TIER_TO")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _CHAIN_ENV + ("VNX_AGENT_LAYOUT", "VNX_HEADLESS_ROUTING"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# _certify_task_class_routing
# ---------------------------------------------------------------------------

_SCENARIOS = [
    ("1.1", "coding_interactive routes to interactive"),
    ("1.2", "research_structured routes headless when enabled"),
    ("1.3", "research_structured falls back to interactive"),
    ("1.4", "docs_synthesis routes headless when enabled"),
    ("1.5", "channel_response without inbox rejected"),
    ("1.6", "ops_watchdog prefers interactive"),
    ("1.7", "unknown skill maps to coding_interactive"),
    ("1.8", "T0 override to healthy target respected"),
    ("1.9", "T0 override to unhealthy target rejected"),
]
_R3 = "reason=R-3: channel_response dispatch has no channel_origin; must enter via inbound inb"
_R6 = "reason=R-6: override target 'cert_unhealthy' health is 'unhealthy', not routing-eligibl"


def _routing_rows(tmp_path, seeded: bool):
    runner = FPCCertificationRunner(state_dir=tmp_path / "state")
    if seeded:
        runner._init_state()
    else:
        init_schema(runner._state_dir)
    runner._certify_task_class_routing()
    return [(r.section, r.row_id, r.status, r.scenario, r.evidence) for r in runner._report.rows]


def test_routing_seeded_rows_order_and_evidence(tmp_path):
    evidence = [
        "target_type=interactive_tmux_claude", "target_type=headless_claude_cli",
        "target_type=interactive_tmux_claude", "target_type=headless_claude_cli",
        _R3, "target_type=interactive_tmux_claude", "resolved=coding_interactive",
        "target=cert_interactive_T1", _R6,
    ]
    expected = [("1", rid, "pass", sc, ev) for (rid, sc), ev in zip(_SCENARIOS, evidence)]
    assert _routing_rows(tmp_path, seeded=True) == expected
    assert "VNX_HEADLESS_ROUTING" not in os.environ


def test_routing_unseeded_rows_fail_paths(tmp_path):
    statuses = ["fail", "fail", "fail", "fail", "pass", "fail", "pass", "fail", "pass"]
    evidence = [
        "routed=False, type=None", "routed=False, type=None", "routed=False, type=None",
        "routed=False, type=None", _R3, "routed=False, type=None",
        "resolved=coding_interactive", "routed=False", _R6,
    ]
    expected = [("1", rid, st, sc, ev) for (rid, sc), st, ev in zip(_SCENARIOS, statuses, evidence)]
    assert _routing_rows(tmp_path, seeded=False) == expected


def test_routing_restores_preexisting_headless_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HEADLESS_ROUTING", "keep-me")
    rows = _routing_rows(tmp_path, seeded=True)
    assert os.environ["VNX_HEADLESS_ROUTING"] == "keep-me"
    assert [r[2] for r in rows] == ["pass"] * 9


def test_routing_restores_env_when_route_raises(tmp_path, monkeypatch):
    import dispatch_router

    monkeypatch.setenv("VNX_HEADLESS_ROUTING", "orig")
    runner = FPCCertificationRunner(state_dir=tmp_path / "state")
    runner._init_state()
    real_route = dispatch_router.DispatchRouter.route
    seen = []

    def _route(self, did, task_class, **kw):
        seen.append((task_class, os.environ.get("VNX_HEADLESS_ROUTING")))
        if did.startswith("cert_1_3_"):
            raise RuntimeError("boom-1.3")
        return real_route(self, did, task_class, **kw)

    monkeypatch.setattr(dispatch_router.DispatchRouter, "route", _route)
    with pytest.raises(RuntimeError, match="boom-1.3"):
        runner._certify_task_class_routing()
    assert os.environ["VNX_HEADLESS_ROUTING"] == "orig"
    assert seen == [("coding_interactive", "orig"), ("research_structured", "1"),
                    ("research_structured", "0")]
    assert [r.row_id for r in runner._report.rows] == ["1.1", "1.2"]


# ---------------------------------------------------------------------------
# emit_dispatch_receipt
# ---------------------------------------------------------------------------

_RECEIPT_KEYS = [
    "schema_version", "dispatch_id", "terminal_id", "provider", "model", "role",
    "receipt_kind", "status", "event_type", "completion_pct", "risk", "duration_seconds",
    "token_usage", "cost_usd", "findings", "pr_id", "report_path", "events_path", "timestamp",
]


def _receipt_kwargs(state_dir, **over):
    kw = dict(
        dispatch_id="r19-d-001", terminal_id="T1", provider="claude", model="sonnet",
        pr_id=None, status="success", completion_pct=100, risk=0.0, findings=[],
        duration_seconds=1.0, token_usage={"input": 1, "output": 2}, cost_usd=None,
        state_dir=state_dir, receipt_kind="dispatch",
    )
    kw.update(over)
    return kw


def _lines(state_dir):
    path = Path(state_dir) / "t0_receipts.ndjson"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_receipt_written_shape_return_and_log(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="governance_emit")
    out = governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path))
    assert out == tmp_path / "t0_receipts.ndjson"
    (line,) = _lines(tmp_path)
    assert list(line)[: len(_RECEIPT_KEYS)] == _RECEIPT_KEYS
    assert "task_class" not in line and "parent_dispatch" not in line
    assert (line["event_type"], line["token_usage"], line["model"]) == (
        "task_complete", {"input": 1, "output": 2}, "sonnet")
    assert "governance_emit: receipt written dispatch=r19-d-001 provider=claude status=success" in caplog.text
    assert (tmp_path / "receipt_idempotency_recent.ndjson").exists()


def test_receipt_v3_is_dispatch_governed(tmp_path):
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, lifecycle_v3=True))
    assert _lines(tmp_path)[0]["event_type"] == "dispatch_governed"


def test_receipt_invalid_provider_raises_before_write(tmp_path):
    with pytest.raises(ValueError, match="Invalid provider 'openai'"):
        governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, provider="openai"))
    assert not (tmp_path / "t0_receipts.ndjson").exists()


def test_receipt_chain_link_env_fallback_and_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PARENT_DISPATCH", "env-parent")
    monkeypatch.setenv("VNX_TASK_CLASS", "env-class")
    monkeypatch.setenv("VNX_TIER_FROM", "")
    monkeypatch.setenv("VNX_TIER_TO", "env-tier-to")
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, task_class="explicit-class"))
    line = _lines(tmp_path)[0]
    assert line["parent_dispatch"] == "env-parent"
    assert line["task_class"] == "explicit-class"
    assert line["tier_to"] == "env-tier-to"
    assert "tier_from" not in line


def test_receipt_model_normalised_and_resolved(tmp_path):
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(
        tmp_path, model="claude-sonnet-5", model_resolved="deepseek/deepseek-v4-pro"))
    line = _lines(tmp_path)[0]
    assert (line["model"], line["model_resolved"]) == ("sonnet-5", "deepseek-v4-pro")


def test_receipt_normaliser_failure_keeps_model(tmp_path, monkeypatch):
    import providers.model_normalizer as mn

    def _boom(_name):
        raise RuntimeError("normaliser down")

    monkeypatch.setattr(mn, "normalize_model_name", _boom)
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, model="claude-sonnet-5"))
    assert _lines(tmp_path)[0]["model"] == "claude-sonnet-5"


def test_receipt_harvest_backfills_empty_usage(tmp_path, monkeypatch):
    import token_harvest

    calls = []
    monkeypatch.setattr(token_harvest, "harvest_session_tokens",
                        lambda sid: calls.append(sid) or {"input": 7, "output": 9})
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, token_usage={}, session_id="s-1"))
    assert calls == ["s-1"]
    assert _lines(tmp_path)[0]["token_usage"] == {"input": 7, "output": 9}


@pytest.mark.parametrize("harvest", [
    lambda sid: {"unavailable": True},
    lambda sid: None,
    lambda sid: (_ for _ in ()).throw(OSError("no transcript")),
])
def test_receipt_harvest_unusable_keeps_caller_usage(tmp_path, monkeypatch, harvest):
    import token_harvest

    monkeypatch.setattr(token_harvest, "harvest_session_tokens", harvest)
    usage = {"unavailable": True, "reason": "subscription"}
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path, token_usage=usage, session_id="s-2"))
    assert _lines(tmp_path)[0]["token_usage"] == usage


def test_receipt_harvest_skipped_for_non_harness_and_real_usage(tmp_path, monkeypatch):
    import token_harvest

    calls = []
    monkeypatch.setattr(token_harvest, "harvest_session_tokens",
                        lambda sid: calls.append(sid) or {"input": 1})
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(
        tmp_path, provider="codex", token_usage={}, session_id="s-3", dispatch_id="r19-a"))
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(
        tmp_path, session_id="s-4", dispatch_id="r19-b"))
    assert calls == []


def test_receipt_append_error_becomes_runtime_error(tmp_path, monkeypatch):
    def _raise(*_a, **_k):
        raise AppendReceiptError("disk", 3, "lock lost")

    monkeypatch.setattr(governance_emit, "_write_receipt_under_lock", _raise)
    with pytest.raises(RuntimeError, match="governance_emit: receipt write failed for dispatch=r19-d-001: lock lost$") as ei:
        governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path))
    assert type(ei.value) is RuntimeError
    assert isinstance(ei.value.__cause__, AppendReceiptError)


@pytest.mark.parametrize("status,expected", [("appended", 1), ("duplicate", 0)])
def test_receipt_confidence_update_only_when_appended(tmp_path, monkeypatch, status, expected):
    order = []
    monkeypatch.setattr(governance_emit, "_write_receipt_under_lock",
                        lambda *a, **k: order.append("write") or SimpleNamespace(status=status))
    monkeypatch.setattr(governance_emit, "_update_confidence_from_receipt",
                        lambda receipt, state_dir: order.append(("conf", receipt["dispatch_id"], state_dir)))
    governance_emit.emit_dispatch_receipt(**_receipt_kwargs(tmp_path))
    assert order == ["write"] + [("conf", "r19-d-001", tmp_path)] * expected
