"""ADR-065 §1 and §2: one failure class per dispatch, one failure per dispatch
with merged evidence (learning loop D1, split 1: the classifier on its own)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from learning_failure_class import (  # noqa: E402
    ELIGIBLE_CLASSES,
    FAILURE_CLASSES,
    classify_failure,
    merge_dispatch_group,
)


def _ts(hours: float = -1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _r(event_type="subprocess_completion", status="failed", dispatch_id="d-1", **extra) -> dict:
    receipt = {
        "event_type": event_type,
        "status": status,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "terminal": "T1",
        "timestamp": _ts(),
    }
    receipt.update(extra)
    return receipt


# --- the table ---------------------------------------------------------------

@pytest.mark.parametrize(
    "group, expected",
    [
        ([_r("task_complete", "contract_invalid", contract_violations=["## Summary"])], "contract_invalid"),
        ([_r("task_complete", "contract_invalid", contract_violations=[])], "contract_violations_empty"),
        ([_r("task_complete", "contract_invalid")], "contract_violations_empty"),
        ([_r(failure_reason="PHANTOM: empty diff")], "phantom"),
        ([_r(status="timeout")], "timeout"),
        ([_r(failure_reason="deadline exceeded after 3600s")], "timeout"),
        ([_r(failure_reason="kimi-cli: [quota_or_auth] 403")], "provider_quota"),
        ([_r("review_gate_result", "failed")], "review_failed"),
        ([_r(failure_reason="token budget exhausted")], "budget"),
        ([_r(failure_reason="Exhausted 3 retries")], "failed"),
        ([_r(status="blocked", error="gate blocked")], "failed"),
        ([_r(status="blocked")], "unclassified"),
        ([_r(status="weird", failure_reason="")], "unclassified"),
        ([], "unclassified"),
    ],
)
def test_table_rows(group, expected):
    assert classify_failure(group) == expected


def test_rank_order_phantom_beats_quota_and_contract_beats_phantom():
    assert classify_failure([_r(failure_reason="PHANTOM: x [quota_or_auth]")]) == "phantom"
    mixed = [_r(failure_reason="PHANTOM: x"),
             _r("task_complete", "contract_invalid", contract_violations=["a"])]
    assert classify_failure(mixed) == "contract_invalid"


def test_evidence_from_the_sister_receipt_decides_the_class():
    group = [_r("dispatch_finished", "failure"), _r(failure_reason="PHANTOM: empty")]
    assert classify_failure(group) == "phantom"


@pytest.mark.parametrize("bad", [None, 7, ["x"], {"a": 1}, "", "   "])
def test_unreadable_fields_fall_through_and_stay_in_vocabulary(bad):
    group = [{"event_type": bad, "status": bad, "failure_reason": bad, "contract_violations": bad}]
    result = classify_failure(group)
    assert result in FAILURE_CLASSES
    assert classify_failure([{"status": bad, "failure_reason": bad}, "not a mapping"]) in FAILURE_CLASSES


def test_vocabulary_and_eligibility():
    assert len(FAILURE_CLASSES) == 9
    assert ELIGIBLE_CLASSES == FAILURE_CLASSES - {"contract_violations_empty", "unclassified"}
    assert isinstance(FAILURE_CLASSES, frozenset) and isinstance(ELIGIBLE_CLASSES, frozenset)


# --- the merge ---------------------------------------------------------------

def test_merge_identity_from_top_rank_evidence_first_non_empty():
    finished = _r("dispatch_finished", "failure", terminal="T9", failure_reason="  ")
    completion = _r(failure_reason="boom", terminal="T1")
    merged = merge_dispatch_group([completion, finished])
    assert merged["terminal"] == "T9"
    assert merged["event_type"] == "dispatch_finished"
    assert merged["failure_reason"] == "boom"
    assert merged["error"] is None


def test_merge_of_nothing_is_empty():
    assert merge_dispatch_group([]) == {}
