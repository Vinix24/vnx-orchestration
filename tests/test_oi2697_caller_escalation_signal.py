"""OI-2697: a caller's deliberate escalation to a higher class is a seat signal.

Pure: no store, no ``gh``. The risk reading is a dict as ``review_risk_class``
records it (``caller`` next to ``derived`` and ``risk_class``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_seat_evaluation as rse

HEAD = "9f3c2a1b7e4d5c6a8b0f1e2d3c4b5a6978879605"  # sample residue 4: the sample does not fire
STACK = ("codex_gate", "kimi_gate")


def _risk(**overrides: Any) -> Dict[str, Any]:
    risk: Dict[str, Any] = {"caller": "high", "derived": "medium", "risk_class": "high", "deciding": []}
    risk.update(overrides)
    return risk


def _decision(risk: Dict[str, Any]) -> Any:
    risk_class, paths = rse.signal_risk(risk)
    return rse.seats_for_signals(rse.SeatSignalInput(
        stack=STACK, seat1_outcome="pass", seat1_reviewer="codex_gate", seat1_takeover_from="",
        real_warning_count=0, prescan_count=0, risk_class=risk_class, risk_paths=paths,
        irreversible=False, head_sha=HEAD, caller_escalated=rse.caller_escalated(risk),
    ))


def test_caller_high_over_derived_medium_keeps_the_second_seat():
    decision = _decision(_risk())
    assert decision.seats == STACK
    assert "caller_escalation" in decision.signals


def test_caller_equal_to_derived_is_no_signal():
    decision = _decision(_risk(caller="medium", risk_class="medium"))
    assert decision.seats == STACK[:1]
    assert "caller_escalation" not in decision.signals


def test_missing_caller_is_no_signal_and_raises_nothing():
    risk = _risk(risk_class="medium")
    del risk["caller"]
    decision = _decision(risk)
    assert decision.seats == STACK[:1]
    assert "caller_escalation" not in decision.signals


def test_caller_high_with_derived_high_escalated_nothing():
    decision = _decision(_risk(derived="high"))
    assert "caller_escalation" not in decision.signals


@pytest.mark.parametrize("caller", [None, "", "bogus", 7, ["high"]])
def test_unknown_caller_fires_nothing(caller):
    assert rse.caller_escalated(_risk(caller=caller)) is False


@pytest.mark.parametrize("derived", [None, "", "bogus", 7])
def test_unknown_derived_fires_nothing(derived):
    assert rse.caller_escalated(_risk(derived=derived)) is False


@pytest.mark.parametrize("risk", [None, "high", [], 3])
def test_unreadable_risk_fires_nothing(risk):
    assert rse.caller_escalated(risk) is False


def test_the_record_lists_the_new_signal():
    record = _decision(_risk()).to_record()
    assert "caller_escalation" in record["signals"]
    assert rse.SIGNAL_NAMES[-1] == "caller_escalation"
    assert rse.SIGNAL_NAMES[:5] == ("real_warning", "security_path", "schema", "irreversible", "sample")


def test_signal_input_for_fills_the_field_from_the_risk_reading():
    required = SimpleNamespace(stack=STACK, risk=_risk())
    assert rse.signal_input_for(required, [], None, HEAD).caller_escalated is True
    required = SimpleNamespace(stack=STACK, risk=_risk(caller="medium"))
    assert rse.signal_input_for(required, [], None, HEAD).caller_escalated is False
