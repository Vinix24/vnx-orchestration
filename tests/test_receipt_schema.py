"""test_receipt_schema.py — receipt-quality PR-B0 schema-contract tests.

Verifies the codified receipt contracts (``receipt_schema.ReceiptV2`` /
``SynthesizedLaneReceipt``) serialize BYTE-IDENTICALLY to the pre-PR-B0
literal dict construction at the emit sites (``governance_emit`` /
``dispatch_govern.ensure_receipt``) — this PR is a no-behavior-change
refactor, so the emitted NDJSON must not change for the same inputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from receipt_schema import ReceiptV2, SynthesizedLaneReceipt  # noqa: E402


def _serialize(receipt: dict) -> str:
    """Mirror the ledger writer's serialization (append primitive:
    ``json.dumps(..., separators=(",", ":"), sort_keys=False)``).
    """
    return json.dumps(receipt, separators=(",", ":"), sort_keys=False)


# ---------------------------------------------------------------------------
# Pre-PR-B0 reference constructions (copied verbatim from the old emit sites)
# ---------------------------------------------------------------------------

def _legacy_v2_build(**kw) -> dict:
    """The pre-PR-B0 literal from governance_emit.emit_dispatch_receipt."""
    receipt = {
        "schema_version": 2,
        "dispatch_id": kw["dispatch_id"],
        "terminal_id": kw["terminal_id"],
        "provider": kw["provider"],
        "model": kw["model"],
        "role": kw.get("role"),
        "receipt_kind": kw["receipt_kind"],
        "status": kw["status"],
        "event_type": "task_complete",
        "completion_pct": kw["completion_pct"],
        "risk": kw["risk"],
        "duration_seconds": round(float(kw["duration_seconds"]), 3),
        "token_usage": kw["token_usage"],
        "cost_usd": kw.get("cost_usd"),
        "findings": kw["findings"],
        "pr_id": kw.get("pr_id"),
        "report_path": kw.get("report_path"),
        "events_path": kw.get("events_path"),
        "timestamp": kw["timestamp"],
    }
    if kw.get("permission_enforcement"):
        receipt["permission_enforcement"] = kw["permission_enforcement"]
    if kw.get("mandate_id"):
        receipt["mandate_id"] = kw["mandate_id"]
    if kw.get("final_prompt_path") is not None:
        receipt["final_prompt_path"] = kw["final_prompt_path"]
    if kw.get("final_prompt_sha256") is not None:
        receipt["final_prompt_sha256"] = kw["final_prompt_sha256"]
    if kw.get("injection_reconstructs") is not None:
        receipt["injection_reconstructs"] = kw["injection_reconstructs"]
    if kw.get("verification") is not None:
        receipt["verification"] = kw["verification"]
    if kw.get("warnings") is not None:
        receipt["warnings"] = kw["warnings"]
    return receipt


def _legacy_synthesized_build(**kw) -> dict:
    """The pre-PR-B0 literal from dispatch_govern.ensure_receipt."""
    receipt = {
        "event_type": "subprocess_completion",
        "dispatch_id": kw["dispatch_id"],
        "terminal": kw["terminal_id"],
        "terminal_id": kw["terminal_id"],
        "status": "failed",
        "source": "tmux_interactive_lane_synthesized",
        "synthesized": True,
        "failure_reason": kw.get("failure_reason"),
        "contract_status": kw["contract_status"],
        "permission_enforcement": kw["permission_enforcement"],
        "timestamp": kw["timestamp"],
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": kw["model"],
        "lane": kw["lane"],
        "role": kw.get("role"),
        "receipt_kind": "dispatch",
    }
    if kw.get("worker_permission_enforcement") is not None:
        receipt["worker_permission_enforcement"] = kw["worker_permission_enforcement"]
    if kw.get("report_path") is not None:
        receipt["report_path"] = kw["report_path"]
    return receipt


_TS = "2026-07-28T12:00:00Z"


def _v2_kwargs_full() -> dict:
    return dict(
        dispatch_id="dispatch-20260728-test",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        role="backend-developer",
        receipt_kind="dispatch",
        status="success",
        completion_pct=100,
        risk=0.25,
        duration_seconds=3.4567,
        token_usage={"input": 100, "output": 50, "cache_hit": 0},
        cost_usd=0.0123,
        findings=[{"severity": "low", "message": "note"}],
        pr_id="PR-1",
        report_path="unified_reports/dispatch-20260728-test.md",
        events_path="events/archive/T1/dispatch-20260728-test.ndjson",
        timestamp=_TS,
        permission_enforcement="enforced",
        mandate_id="M-1",
        final_prompt_path="prompts/final.md",
        final_prompt_sha256="abc123",
        injection_reconstructs=True,
        verification={"method": "report-parser", "ok": True},
        warnings=[{"code": "W1", "severity": "low", "message": "warn"}],
    )


def _v2_kwargs_minimal() -> dict:
    return dict(
        dispatch_id="dispatch-20260728-min",
        terminal_id="T2",
        provider="kimi",
        model="kimi-k3",
        role=None,
        receipt_kind="build",
        status="failed",
        completion_pct=40,
        risk=0.9,
        duration_seconds=1.2,
        token_usage={"input": 0, "output": 0, "cache_hit": 0},
        cost_usd=None,
        findings=[],
        pr_id=None,
        report_path=None,
        events_path=None,
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# ReceiptV2 round-trip: old-literal vs contract serialize to equal JSON
# ---------------------------------------------------------------------------

def test_receipt_v2_roundtrip_full_fields_byte_identical():
    kw = _v2_kwargs_full()
    legacy_line = _serialize(_legacy_v2_build(**kw))
    contract_line = _serialize(ReceiptV2(**kw).to_dict())
    assert contract_line == legacy_line


def test_receipt_v2_roundtrip_minimal_fields_byte_identical():
    kw = _v2_kwargs_minimal()
    legacy_line = _serialize(_legacy_v2_build(**kw))
    contract_line = _serialize(ReceiptV2(**kw).to_dict())
    assert contract_line == legacy_line


def test_receipt_v2_unconditional_nulls_stamped():
    """role/pr_id/report_path/events_path/cost_usd serialize as null, never
    omitted — the ledger distinguishes "unresolved" from "pre-feature"."""
    receipt = ReceiptV2(**_v2_kwargs_minimal()).to_dict()
    for key in ("role", "pr_id", "report_path", "events_path", "cost_usd"):
        assert key in receipt
        assert receipt[key] is None


def test_receipt_v2_conditional_fields_omitted_when_absent():
    receipt = ReceiptV2(**_v2_kwargs_minimal()).to_dict()
    for key in (
        "permission_enforcement",
        "mandate_id",
        "final_prompt_path",
        "final_prompt_sha256",
        "injection_reconstructs",
        "verification",
        "warnings",
    ):
        assert key not in receipt


def test_receipt_v2_permission_enforcement_falsy_omitted():
    """Pre-PR-B0 guard was truthiness (not is-not-None): empty string omits."""
    kw = _v2_kwargs_minimal()
    kw["permission_enforcement"] = ""
    kw["mandate_id"] = ""
    receipt = ReceiptV2(**kw).to_dict()
    assert "permission_enforcement" not in receipt
    assert "mandate_id" not in receipt


def test_receipt_v2_defaults_schema_version_event_type_and_timestamp():
    kw = _v2_kwargs_minimal()
    del kw["timestamp"]
    receipt = ReceiptV2(**kw).to_dict()
    assert receipt["schema_version"] == 2
    assert receipt["event_type"] == "task_complete"
    assert receipt["timestamp"]  # auto-stamped


def test_receipt_v2_schema_version_event_type_not_overridable():
    """OI-817: schema_version/event_type are contract identity — a caller
    cannot stamp a non-v2 receipt or a different event_type by passing them."""
    kw = _v2_kwargs_minimal()
    kw["schema_version"] = 99
    kw["event_type"] = "forged"
    receipt = ReceiptV2(**kw).to_dict()
    assert receipt["schema_version"] == 2
    assert receipt["event_type"] == "task_complete"


def test_receipt_v2_duration_rounded_three_decimals():
    kw = _v2_kwargs_minimal()
    kw["duration_seconds"] = 3.456789
    assert ReceiptV2(**kw).to_dict()["duration_seconds"] == 3.457


def test_receipt_v2_receipt_kind_closed_set_raises():
    kw = _v2_kwargs_minimal()
    kw["receipt_kind"] = "not-a-kind"
    with pytest.raises(ValueError, match="Invalid receipt_kind"):
        ReceiptV2(**kw)


def test_receipt_v2_receipt_kind_missing_raises():
    kw = _v2_kwargs_minimal()
    kw["receipt_kind"] = None
    with pytest.raises(ValueError, match="Invalid receipt_kind"):
        ReceiptV2(**kw)


# ---------------------------------------------------------------------------
# receipt-quality PR-B2: tool-call signal fields (additive, conditionally
# stamped like final_prompt_path/verification/warnings — omitted when None)
# ---------------------------------------------------------------------------

def test_receipt_v2_toolcall_fields_omitted_when_absent():
    """Minimal kwargs (no toolcall fields passed) stays byte-identical to the
    pre-PR-B2 shape — proven separately by the roundtrip tests above."""
    receipt = ReceiptV2(**_v2_kwargs_minimal()).to_dict()
    for key in ("tool_call_count", "tool_call_failures", "tool_call_retries"):
        assert key not in receipt


def test_receipt_v2_toolcall_fields_stamped_when_provided():
    kw = _v2_kwargs_minimal()
    kw["tool_call_count"] = 5
    kw["tool_call_failures"] = 1
    kw["tool_call_retries"] = 2
    receipt = ReceiptV2(**kw).to_dict()
    assert receipt["tool_call_count"] == 5
    assert receipt["tool_call_failures"] == 1
    assert receipt["tool_call_retries"] == 2


def test_receipt_v2_toolcall_zero_is_stamped_not_omitted():
    """0 is a real observation ('confirmed zero'), distinct from None
    ('no signal log') -- must not be treated as falsy-omit."""
    kw = _v2_kwargs_minimal()
    kw["tool_call_count"] = 0
    kw["tool_call_failures"] = 0
    kw["tool_call_retries"] = 0
    receipt = ReceiptV2(**kw).to_dict()
    assert receipt["tool_call_count"] == 0
    assert receipt["tool_call_failures"] == 0
    assert receipt["tool_call_retries"] == 0


# ---------------------------------------------------------------------------
# SynthesizedLaneReceipt round-trip
# ---------------------------------------------------------------------------

def _synth_kwargs() -> dict:
    return dict(
        dispatch_id="dispatch-20260728-synth",
        terminal_id="T3",
        model="unknown",
        lane="tmux_interactive",
        failure_reason="worker_timeout",
        contract_status="synthesized",
        permission_enforcement="off",
        role="identity_unresolved",
        timestamp=_TS,
    )


def test_synthesized_receipt_roundtrip_byte_identical():
    kw = _synth_kwargs()
    legacy_line = _serialize(_legacy_synthesized_build(**kw))
    contract_line = _serialize(SynthesizedLaneReceipt(**kw).to_dict())
    assert contract_line == legacy_line


def test_synthesized_receipt_roundtrip_with_optional_fields_byte_identical():
    kw = _synth_kwargs()
    kw["worker_permission_enforcement"] = "enforced"
    kw["report_path"] = "unified_reports/dispatch-20260728-synth.md"
    legacy_line = _serialize(_legacy_synthesized_build(**kw))
    contract_line = _serialize(SynthesizedLaneReceipt(**kw).to_dict())
    assert contract_line == legacy_line


def test_synthesized_receipt_optional_fields_omitted_when_absent():
    receipt = SynthesizedLaneReceipt(**_synth_kwargs()).to_dict()
    assert "worker_permission_enforcement" not in receipt
    assert "report_path" not in receipt


def test_synthesized_receipt_receipt_kind_closed_set_raises():
    kw = _synth_kwargs()
    kw["receipt_kind"] = "bogus"
    with pytest.raises(ValueError, match="Invalid receipt_kind"):
        SynthesizedLaneReceipt(**kw)
