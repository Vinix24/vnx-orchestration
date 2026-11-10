"""ADR-065 §1 and §2: one failure class per dispatch, one failure per dispatch
with merged evidence (learning loop D1)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402
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


def _loop(tmp_path: Path, monkeypatch, receipts: list[dict]):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop.receipts_path = state_dir / "t0_receipts.ndjson"
    loop.db_path = state_dir / "quality_intelligence.db"
    loop.receipts_path.write_text(
        "\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8"
    )
    return loop


def _window():
    return datetime.now(timezone.utc) - timedelta(days=2)


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


# --- extraction and counts ---------------------------------------------------

def test_two_receipts_of_one_dispatch_are_one_failure_with_the_text(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [
        _r("dispatch_finished", "failure"),
        _r("subprocess_completion", "failed", failure_reason="Exhausted 3 retries"),
    ])
    failures = loop.extract_failure_patterns(start_time=_window())
    assert len(failures) == 1
    assert failures[0]["error"] == "Exhausted 3 retries"
    assert failures[0]["dispatch_id"] == "d-1"
    assert failures[0]["event_type"] == "dispatch_finished"
    assert failures[0]["failure_class"] == "failed"


def test_count_invariants_and_class_counts(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [
        _r("dispatch_finished", "failure", dispatch_id="a"),
        _r(dispatch_id="a", failure_reason="PHANTOM: empty"),
        _r(dispatch_id="b", failure_reason="x"),
        _r(dispatch_id=None, failure_reason="y"),
        _r(dispatch_id="", failure_reason="y"),
    ])
    failures = loop.extract_failure_patterns(start_time=_window())
    stats = loop.receipt_stats
    assert stats["failure_receipts"] == 5
    assert stats["dispatches"] == 2
    assert stats["without_dispatch_id"] == 2
    assert stats["merged_duplicates"] == 1
    assert len(failures) == stats["dispatches"] + stats["without_dispatch_id"]
    assert stats["failure_receipts"] == len(failures) + stats["merged_duplicates"]
    assert set(stats["class_counts"]) == set(FAILURE_CLASSES)
    assert stats["class_counts"]["phantom"] == 1
    assert stats["class_counts"]["failed"] == 3
    assert stats["class_counts"]["unclassified"] == 0
    assert sum(stats["class_counts"].values()) == len(failures)


def test_empty_stream_keeps_receipt_stats_shape(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [])
    assert loop.extract_failure_patterns(start_time=_window()) == []


# --- rules -------------------------------------------------------------------

def test_dispatches_without_any_text_make_no_rule_and_count_as_unclassified(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [
        _r("task_complete", "weird", dispatch_id="a"),
        _r("task_complete", "weird", dispatch_id="b"),
    ])
    # a status outside _FAILURE_STATUSES never reaches extraction; feed patterns directly
    patterns = [
        {"error": "Unknown error", "terminal": "T1", "agent": "claude",
         "failure_class": "unclassified", "dispatch_id": d}
        for d in ("a", "b")
    ]
    assert loop.generate_prevention_rules(patterns) == []
    assert loop.generate_prevention_rules(
        [dict(p, failure_class="mystery") for p in patterns]
    ) == []
    assert loop.generate_prevention_rules(
        [dict(p, failure_class="contract_violations_empty") for p in patterns]
    ) == []


def test_textless_failure_receipts_are_unclassified_through_extraction(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [
        _r("dispatch_finished", "failure", dispatch_id="a"),
        _r("dispatch_finished", "failure", dispatch_id="b"),
    ])
    failures = loop.extract_failure_patterns(start_time=_window())
    assert {f["error"] for f in failures} == {"Unknown error"}
    assert {f["failure_class"] for f in failures} == {"unclassified"}
    assert loop.receipt_stats["class_counts"]["unclassified"] == 2
    assert loop.generate_prevention_rules(failures) == []
    assert classify_failure([{"event_type": "task_complete", "status": "x"}]) == "unclassified"


def test_rule_counts_dispatches_and_carries_class_and_sources(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [])
    patterns = [
        {"error": "boom", "terminal": "T1", "agent": "claude",
         "failure_class": "failed", "dispatch_id": f"d-{n:02d}"}
        for n in range(25, 0, -1)
    ]
    rules = loop.generate_prevention_rules(patterns)
    assert len(rules) == 1
    assert rules[0]["failure_class"] == "failed"
    assert rules[0]["occurrence_count"] == 25
    ids = rules[0]["source_dispatch_ids"]
    assert ids == sorted(ids) and len(ids) == 20
    assert loop.generate_prevention_rules(patterns[:1]) == []


def test_same_error_in_two_classes_is_two_groups(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [])
    patterns = [
        {"error": "boom", "terminal": "T1", "agent": "claude", "failure_class": "failed", "dispatch_id": "a"},
        {"error": "boom", "terminal": "T1", "agent": "claude", "failure_class": "timeout", "dispatch_id": "b"},
    ]
    assert loop.generate_prevention_rules(patterns) == []


# --- round 2: the text follows the group's class, the id carries the class ---

def test_contract_invalid_group_under_a_failed_completion_keeps_the_violations_text(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [
        _r("subprocess_completion", "failed"),
        _r("report_contract_invalid", "contract_invalid", contract_violations=["## Summary", "## Open Items"]),
    ])
    failures = loop.extract_failure_patterns(start_time=_window())
    assert len(failures) == 1
    assert failures[0]["failure_class"] == "contract_invalid"
    assert failures[0]["error"] == "Contract violations: ## Summary, ## Open Items"


def test_violations_text_is_not_used_outside_the_contract_invalid_class(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch, [])
    receipt = {"contract_violations": ["## Summary"]}
    assert loop._failure_error_message(receipt, "failed") == "Unknown error"
    assert loop._failure_error_message(receipt) == "Unknown error"
    assert loop._failure_error_message(receipt, None) == "Unknown error"
    assert loop._failure_error_message(receipt, "contract_invalid") == "Contract violations: ## Summary"
    assert loop._failure_error_message(
        {"failure_reason": "kept", "contract_violations": ["x"]}, "contract_invalid"
    ) == "kept"


def test_same_error_terminal_agent_in_two_classes_gets_two_store_ids(tmp_path, monkeypatch):
    from prevention_rule_store import rule_id

    loop = _loop(tmp_path, monkeypatch, [])
    patterns = [
        {"error": "boom", "terminal": "T1", "agent": "claude", "failure_class": cls, "dispatch_id": f"{cls}-{n}"}
        for cls in ("contract_invalid", "timeout") for n in range(2)
    ]
    rules = loop.generate_prevention_rules(patterns)
    assert {r["failure_class"] for r in rules} == {"contract_invalid", "timeout"}
    ids = [ll._rule_id(r) for r in rules]
    assert len(set(ids)) == 2
    assert ids == [rule_id(r) for r in rules]
