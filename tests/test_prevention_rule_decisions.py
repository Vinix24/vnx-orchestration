"""prevention_rule_store decisions: a decision is a record with a minted proof (loop D3, split 1)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import prevention_rule_store as store  # noqa: E402

NOW = "2026-11-10T12:00:00+00:00"
PID = "tproj"
CRITERION = "dispatches_with_class:timeout drops below 2 per week"
STOP = "stop when a worker is blocked twice by the rule"


def _queue(state, *patterns):
    proposals = [
        {"pattern": p, "terminal_constraint": "T1", "failure_class": "timeout", "prevention": "prevent",
         "confidence": 0.8, "occurrence_count": 3, "source_dispatch_ids": ["d-1", "d-2"]}
        for p in patterns
    ]
    store.queue_rules(state, proposals, project_id=PID, now=NOW)
    return [store.rule_id(p) for p in proposals]


def _reject(state, ids, now=NOW):
    return store.decide(state, ids, "rejected", reason="r", decided_by="v", project_id=PID, now=now)


def _approve(state, ids, now=NOW, **kw):
    kw = {"effect_criterion": CRITERION, "stop_condition": STOP, "reason": "r", **kw}
    return store.decide(state, ids, "approved", decided_by="v", project_id=PID, now=now, **kw)


def _status(state, rid):
    return next(r["status"] for r in store.load_queue(state) if r["id"] == rid)


def _edit_queue(state, **fields):
    path = state / store.QUEUE_FILENAME
    data = json.loads(path.read_text())
    data["pending_rules"][0].update(fields)
    path.write_text(json.dumps(data))


def test_reject_writes_one_record_with_a_minted_approval_id(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    (record,) = _reject(tmp_path, [rid])
    assert store.decisions(tmp_path) == [record]
    assert record["decision"] == "rejected" and record["rule_id"] == rid
    assert re.fullmatch(r"apr-[0-9a-f]{12}", record["approval_id"])
    assert record["approval_id"] == store.mint_approval_id(PID, rid, "rejected", NOW, "v")
    assert "bundle_ref" not in record
    assert record["baseline_metric"] == "dispatches_with_class:timeout" and record["baseline_value"] == 3
    assert record["rollout_scope"] == "project" and record["project_id"] == PID
    assert _status(tmp_path, rid) == "rejected"


def test_approve_is_one_bundle_with_criteria_and_recovery_path(tmp_path):
    a, b = _queue(tmp_path, "p1", "p2")
    recs = _approve(tmp_path, [b, a])
    assert {r["rule_id"] for r in recs} == {a, b}
    assert {r["bundle_ref"] for r in recs} == {store.mint_bundle_ref([a, b], NOW)}
    assert re.fullmatch(r"bundle-[0-9a-f]{12}", recs[0]["bundle_ref"])
    assert all(r["effect_criterion"] == CRITERION and r["stop_condition"] == STOP for r in recs)
    assert all(r["recovery_path"] == store.RECOVERY_PATH for r in recs)
    assert store.approved_decision(tmp_path, a)["rule_id"] == a
    assert _status(tmp_path, a) == _status(tmp_path, b) == "approved"


@pytest.mark.parametrize("kw", [
    {"effect_criterion": None}, {"stop_condition": None}, {"effect_criterion": " "}, {"stop_condition": 5},
])
def test_approve_without_both_criteria_raises_and_writes_nothing(tmp_path, kw):
    (rid,) = _queue(tmp_path, "p1")
    with pytest.raises(ValueError):
        _approve(tmp_path, [rid], **kw)
    assert store.decisions(tmp_path) == [] and _status(tmp_path, rid) == "pending"


def test_decide_validates_its_arguments(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    kw = dict(reason="r", decided_by="v", project_id=PID, now=NOW)
    for ids, decision, over in (([rid], "deactivated", {}), ([], "rejected", {}), ([rid], "rejected", {"decided_by": None})):
        with pytest.raises(ValueError):
            store.decide(tmp_path, ids, decision, **{**kw, **over})
    assert store.decisions(tmp_path) == []


def test_identical_repeat_writes_no_second_record(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    assert len(_reject(tmp_path, [rid])) == 1
    assert _reject(tmp_path, [rid], now="2026-11-10T13:00:00+00:00") == []
    assert len(store.decisions(tmp_path)) == 1


def test_unknown_rule_raises_and_a_bundle_is_all_or_nothing(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    with pytest.raises(store.RuleNotFound):
        _reject(tmp_path, [rid, "rule-00000000"])
    assert store.decisions(tmp_path) == [] and _status(tmp_path, rid) == "pending"


def test_conflicting_decision_is_refused(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    _reject(tmp_path, [rid])
    with pytest.raises(store.DecisionRefused):
        _approve(tmp_path, [rid])
    assert len(store.decisions(tmp_path)) == 1 and _status(tmp_path, rid) == "rejected"


@pytest.mark.parametrize("status", ["approved", "ingested", "bogus", None])
def test_a_status_without_a_record_cannot_be_decided(tmp_path, status):
    (rid,) = _queue(tmp_path, "p1")
    _edit_queue(tmp_path, status=status)
    with pytest.raises(store.DecisionRefused):
        _reject(tmp_path, [rid])
    assert store.approved_decision(tmp_path, rid) is None and store.decisions(tmp_path) == []


@pytest.mark.parametrize("field,value", [
    ("proposal_hash", "0" * 64), ("proposal_hash", None), ("project_id", "other"), ("project_id", None),
    ("prevention", "edited after the proposal"),
])
def test_a_tampered_or_foreign_entry_is_refused(tmp_path, field, value):
    (rid,) = _queue(tmp_path, "p1")
    _edit_queue(tmp_path, **{field: value})
    with pytest.raises(store.DecisionRefused):
        _reject(tmp_path, [rid])
    assert store.decisions(tmp_path) == []


@pytest.mark.parametrize("content", [
    "not json\n", '{"rule_id": "x"}\n', "[1]\n", '{"rule_id": "x", "decision": "maybe"}\n',
])
def test_an_unreadable_record_raises_never_reads_as_empty(tmp_path, content):
    (rid,) = _queue(tmp_path, "p1")
    (tmp_path / store.DECISIONS_FILENAME).write_text(content)
    for call in (lambda: store.decisions(tmp_path), lambda: store.approved_decision(tmp_path, rid),
                 lambda: store.reconcile(tmp_path), lambda: _reject(tmp_path, [rid])):
        with pytest.raises(store.DecisionsUnreadable):
            call()
    assert _status(tmp_path, rid) == "pending"


def test_a_crash_between_record_and_projection_is_repaired_by_reconcile(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    with patch.object(store, "_write_atomic", side_effect=OSError("crash")):
        with pytest.raises(OSError):
            _reject(tmp_path, [rid])
    assert len(store.decisions(tmp_path)) == 1 and _status(tmp_path, rid) == "pending"
    assert store.reconcile(tmp_path) == 1
    assert _status(tmp_path, rid) == "rejected"
    assert store.reconcile(tmp_path) == 0
    assert _reject(tmp_path, [rid]) == []


def test_a_decide_after_a_crash_reconciles_first(tmp_path):
    a, b = _queue(tmp_path, "p1", "p2")
    with patch.object(store, "_write_atomic", side_effect=OSError("crash")):
        with pytest.raises(OSError):
            _reject(tmp_path, [a])
    assert len(_reject(tmp_path, [b])) == 1
    assert _status(tmp_path, a) == _status(tmp_path, b) == "rejected"


def test_a_new_approval_after_a_deactivation_gets_a_new_approval_id(tmp_path):
    (rid,) = _queue(tmp_path, "p1")
    (first,) = _approve(tmp_path, [rid])
    store._append_records(tmp_path / store.DECISIONS_FILENAME, [{**first, "decision": "deactivated"}])
    assert store.approved_decision(tmp_path, rid) is None
    assert store.reconcile(tmp_path) == 1 and _status(tmp_path, rid) == "deactivated"
    (second,) = _approve(tmp_path, [rid], now="2026-11-11T12:00:00+00:00")
    assert second["approval_id"] != first["approval_id"]
    assert store.approved_decision(tmp_path, rid)["approval_id"] == second["approval_id"]


def test_only_the_store_forms_an_approval_or_bundle_string():
    tracked = subprocess.run(["git", "ls-files", "scripts", "vnx_cli", "bin"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    pattern = re.compile(r"""f?["']apr-|f?["']bundle-""")
    # audit_bundle.py names an unrelated audit bundle ("bundle-<uuid4>"), not a decision bundle.
    skip = ("scripts/lib/prevention_rule_store.py", "scripts/lib/audit_bundle.py")
    offenders = [n for n in tracked if n.endswith(".py") and n not in skip
                 and pattern.search((REPO_ROOT / n).read_text(encoding="utf-8", errors="replace"))]
    assert offenders == []


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c"])
def test_a_reason_with_a_unicode_line_separator_reads_back_byte_equal(tmp_path, sep):
    (rid,) = _queue(tmp_path, "p1")
    (record,) = _approve(tmp_path, [rid], reason=f"before{sep}after")
    assert [r["reason"] for r in store.decisions(tmp_path)] == [f"before{sep}after"]
    assert store.approved_decision(tmp_path, rid) == record
