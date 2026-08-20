"""tests/test_dispatch_bridge_escalation.py — the escalation CALLER's write path (OI-1221).

``escalate_tier``/``next_tier`` already computed the climb (tier_to = tier_from + 1,
parent_dispatch = the rejected attempt), but no caller ever fired a followup, so
``tier_from``/``tier_to`` stayed empty on 0 of 761 filled specs. ``stage_escalation_bundle``
is that caller: it resolves the climb from the cost ladder, resolves the target rung's
route, and writes a genuinely-staged ``dispatch-spec.json`` carrying the three chain-link
fields.

These tests pin the WRITE PATH, not a dataclass: every assertion reads the spec back
from disk. A dataclass test proves nothing about the receipt — the bytes the door reads
are the bytes the receipt processor copies.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402

_REJECTED_ID = "20260815-094500-rejected-attempt"
_FOLLOWUP_ID = "20260815-100000-escalated-followup"


def _force_kimi(monkeypatch, present: bool = True):
    """Deterministically set kimi CLI presence (kimi-via-cli-only gate)."""
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil,
        "which",
        (lambda name: "/usr/local/bin/kimi") if present else (lambda name: None),
    )


def _stage_escalation(tmp_path, **over):
    base = dict(
        rejected_dispatch_id=_REJECTED_ID,
        tier_from="tier-low",
        failure_class="model_error",
        instruction_text="escalate: the cheap attempt was rejected",
        dispatch_id=_FOLLOWUP_ID,
        role="dev",
        target_slot="T1",
        project_id="p1",
        data_dir=tmp_path,
        env={},
        state_dir=tmp_path / "state",
        now=0.0,
    )
    base.update(over)
    return dispatch_bridge.stage_escalation_bundle(**base)


def _read_payload(spec_file: Path) -> dict:
    return json.loads(spec_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The escalation followup carries the three chain-link fields — on disk
# ---------------------------------------------------------------------------

def test_escalation_bundle_writes_chain_link_fields_to_disk(tmp_path, monkeypatch):
    """A REAL write: the staged spec carries tier_from/tier_to/parent_dispatch filled."""
    _force_kimi(monkeypatch, present=True)
    spec_file = _stage_escalation(tmp_path)
    payload = _read_payload(spec_file)

    assert payload["tier_from"] == "tier-low"
    assert payload["tier_to"] == "tier-mid"
    assert payload["parent_dispatch"] == _REJECTED_ID
    assert payload["dispatch_id"] == _FOLLOWUP_ID


def test_escalation_followup_runs_on_the_escalated_model(tmp_path, monkeypatch):
    """The followup resolves the TARGET rung's route — not re-classified to the
    cheap tier it climbed out of (tier-low's deepseek primary)."""
    _force_kimi(monkeypatch, present=True)
    payload = _read_payload(_stage_escalation(tmp_path))

    assert payload["provider"] == "claude"
    assert payload["model"] == "sonnet-5"


def test_escalation_at_top_rung_refuses_to_stage(tmp_path, monkeypatch):
    """fable-5 is the top rung: escalation has nowhere to climb and fails loud."""
    _force_kimi(monkeypatch, present=True)
    with pytest.raises(ValueError, match="top rung"):
        _stage_escalation(tmp_path, tier_from="fable-5")


def test_escalation_auth_rejected_refuses_to_stage(tmp_path, monkeypatch):
    """auth_rejected must not climb: a higher tier has the same auth problem, so
    staging a followup would just burn the more expensive model the same way."""
    _force_kimi(monkeypatch, present=True)
    with pytest.raises(ValueError, match="does not climb"):
        _stage_escalation(tmp_path, failure_class="auth_rejected")


def test_escalation_unknown_class_refuses_to_stage(tmp_path, monkeypatch):
    """unknown must not climb, and the refusal names the unknown class loudly."""
    _force_kimi(monkeypatch, present=True)
    with pytest.raises(ValueError, match="UNKNOWN"):
        _stage_escalation(tmp_path, failure_class="unknown")


def test_escalation_timeout_retries_same_tier(tmp_path, monkeypatch):
    """timeout retries on the SAME tier: the staged spec's tier_to == tier_from
    (the ladder does not climb), and the followup still resolves a real route."""
    _force_kimi(monkeypatch, present=True)
    spec_file = _stage_escalation(tmp_path, failure_class="timeout")
    payload = _read_payload(spec_file)
    assert payload["tier_from"] == "tier-low"
    assert payload["tier_to"] == "tier-low"
    assert payload["provider"]  # a real route, not an empty staged spec


# ---------------------------------------------------------------------------
# A normal dispatch keeps the fields empty — the distinction stays countable
# ---------------------------------------------------------------------------

def test_normal_bundle_keeps_chain_link_fields_empty(tmp_path):
    """A non-escalated dispatch stages with all three chain-link fields unset, so
    the escalation signal on receipts remains a countable property (293 of 761
    specs already carry the empty keys — this is the write-path half of that)."""
    spec_file = dispatch_bridge.stage_spec_bundle(
        instruction_text="normal work",
        dispatch_id="20260815-110000-normal",
        role="dev",
        target_slot="T1",
        project_id="p1",
        data_dir=tmp_path,
    )
    payload = _read_payload(spec_file)

    assert payload["tier_from"] is None
    assert payload["tier_to"] is None
    assert payload["parent_dispatch"] is None


# ---------------------------------------------------------------------------
# Regression: the fields must survive the write path, not just the kwargs
# ---------------------------------------------------------------------------

def test_escalation_fields_are_written_not_just_passed(tmp_path, monkeypatch):
    """Fails if the chain-link fields fall OUT of stage_spec_bundle's spec_payload:
    stage_escalation_bundle passes them as kwargs, but a payload that drops the
    keys (or writes them empty) must be caught here — the receipt reads the file,
    not the call."""
    _force_kimi(monkeypatch, present=True)
    payload = _read_payload(_stage_escalation(tmp_path))

    # Non-empty STRINGS in the written file — the exact bytes the door → receipt
    # chain copies. A missing key would raise KeyError; a None would fail these.
    for field in ("tier_from", "tier_to", "parent_dispatch"):
        assert isinstance(payload[field], str) and payload[field].strip(), field
