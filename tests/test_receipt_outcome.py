#!/usr/bin/env python3
"""fabric-state-herstel D3a: `receipt_outcome.compute_outcomes` rules 1-7.

ADR-007: every project-scoped case carries a second project with a
colliding dispatch id that must not leak. No real ledger is read.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "scripts" / "lib"))

import receipt_outcome as ro  # noqa: E402

VERIFIED = {"method": "pytest", "tests_run": 4, "tests_passed": 4, "tests_failed": 0}


def _tc(did: str, status: str = "success", **kw: Any) -> Dict[str, Any]:
    r = {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
         "status": status, "verification": VERIFIED, "timestamp": "2026-09-29T10:00:00Z"}
    r.update(kw)
    return r


def _ev(event: str, did: str, **kw: Any) -> Dict[str, Any]:
    r = {"event_type": event, "dispatch_id": did, "timestamp": "2026-09-29T10:00:00Z"}
    r.update(kw)
    return r


def _decisions(report: Dict[str, Any]) -> Dict[str, str]:
    return {d: o["decision"] for d, o in report["outcomes"].items()}


def test_noise_reasons_are_excluded_and_counted():
    report = ro.compute_outcomes([
        _tc("t1", source="pytest"),
        _tc("t2", report_path="/tmp/pytest-of-x/r.md"),
        _tc("t3", note="<MagicMock id='1'>"),
        _tc("unknown"), _tc("?"), _tc(""),
        _tc("real"),
    ])
    assert _decisions(report) == {"real": "accept"}
    assert report["noise"] == {"pytest": 1, "temp_report_path": 1, "magicmock": 1, "unresolved_id": 3}


def test_lane_is_not_a_noise_filter():
    report = ro.compute_outcomes([_tc("plan-gate-run", synthesized=True, lane="tmux_interactive_lane_synthesized")])
    assert _decisions(report) == {"plan-gate-run": "accept"}


def test_project_filter_drops_foreign_receipts_with_colliding_id():
    receipts = [
        _tc("d1", "success", project_id="proj-a"),
        _tc("d1", "failure", project_id="proj-b"),
        _tc("d2", "failure", project_id="proj-b"),
    ]
    scoped = ro.compute_outcomes(receipts, project_id="proj-a")
    assert _decisions(scoped) == {"d1": "accept"}
    assert scoped["noise"] == {"foreign_project": 2}
    assert _decisions(ro.compute_outcomes(receipts, project_id="proj-b")) == {"d1": "reject", "d2": "reject"}


def test_last_a_in_file_order_wins_not_timestamp_order():
    late_ts_first = _tc("d1", "failure", timestamp="2026-09-29T11:00:00Z")
    early_ts_last = _tc("d1", "success", timestamp="2026-09-29T09:00:00Z")
    assert _decisions(ro.compute_outcomes([late_ts_first, early_ts_last])) == {"d1": "accept"}


def test_review_gate_receipt_kind_is_not_writer_a():
    gate = _ev("review_gate_result", "d1", receipt_kind="review_gate", status="completed", gate="codex_gate")
    report = ro.compute_outcomes([gate])
    assert report["outcomes"] == {} and report["evidence_only"] == 1


def test_b_only_dispatch_uses_b_status_and_verification():
    b = _tc("d1", "done", report_file="d1.md")
    assert _decisions(ro.compute_outcomes([b])) == {"d1": "accept"}


def test_contract_invalid_after_last_a_wins_as_reject():
    report = ro.compute_outcomes([_tc("d1"), _ev("report_contract_invalid", "d1", status="contract_invalid")])
    assert _decisions(report) == {"d1": "reject"}


def test_repeat_after_contract_invalid_is_accept():
    report = ro.compute_outcomes([_ev("report_contract_invalid", "d1", status="contract_invalid"), _tc("d1")])
    assert _decisions(report) == {"d1": "accept"}


def test_gate_dispatch_id_groups_on_pr_and_links_to_work_dispatch():
    receipts = [
        _tc("work-1", pr_number=1884),
        _ev("review_gate_result", "kimi-gate-pr1884-abc", receipt_kind="review_gate",
            gate="kimi_gate", status="completed", pr_number=1884),
        _ev("pr_merged", "work-1", pr_number=1884, conclusion="merged"),
        _ev("review_gate_result", "kimi-gate-pr555-zzz", pr_number=555, gate="kimi_gate", status="completed"),
    ]
    report = ro.compute_outcomes(receipts)
    assert [e["kind"] for e in report["outcomes"]["work-1"]["evidence"]] == ["review_gate_result", "pr_merged"]
    assert report["evidence_unlinked"] == 1
    assert "kimi-gate-pr1884-abc" not in report["outcomes"]


def test_blocking_event_after_last_outcome_only():
    before = ro.compute_outcomes([_ev("door_bookkeeping_failed", "d1"), _tc("d1")])
    after = ro.compute_outcomes([_tc("d1"), _ev("gate_obligation_reopened_stale_evidence", "d1")])
    assert _decisions(before) == {"d1": "accept"}
    assert _decisions(after) == {"d1": "investigate"}
    assert "gate_obligation_reopened_stale_evidence" in after["outcomes"]["d1"]["reason"]


def test_blocking_event_does_not_soften_a_reject():
    report = ro.compute_outcomes([_tc("d1", "failure"), _ev("pr_merge_refused", "d1")])
    assert _decisions(report) == {"d1": "reject"}


def test_blocking_event_without_outcome_is_investigate():
    assert _decisions(ro.compute_outcomes([_ev("pr_merge_refused", "d1")])) == {"d1": "investigate"}


def test_supersede_via_branch_of_parent():
    receipts = [_tc("p", "failure"), _tc("c", branch="dispatch/p")]
    report = ro.compute_outcomes(receipts)
    assert _decisions(report) == {"p": "superseded", "c": "accept"}


def test_own_branch_is_not_a_link():
    report = ro.compute_outcomes([_tc("p", "failure"), _tc("c", branch="dispatch/c")])
    assert _decisions(report) == {"p": "reject", "c": "accept"}


def test_rejected_child_does_not_supersede_parent():
    report = ro.compute_outcomes([_tc("p", "failure"), _tc("c", "failure", parent_dispatch="p")])
    assert _decisions(report) == {"p": "reject", "c": "reject"}


def test_since_window_scopes_outcomes_and_counts():
    old = _tc("old", timestamp="2026-09-01T00:00:00Z")
    new = _tc("new")
    since = datetime(2026, 9, 28, tzinfo=timezone.utc)
    report = ro.compute_outcomes([old, new], since=since)
    assert _decisions(report) == {"new": "accept"} and report["counts"]["accept"] == 1


def test_integer_timestamp_and_malformed_rows_do_not_crash():
    report = ro.compute_outcomes([_tc("d1", timestamp=1790000000), "junk", None, {"dispatch_id": "d2"}])
    assert _decisions(report) == {"d1": "accept", "d2": "unknown"}
    assert ro.parse_timestamp(True) is None and ro.parse_timestamp("nope") is None


def test_gate_dispatch_task_complete_is_evidence_for_the_pr_not_an_outcome():
    receipts = [_tc("work-1", pr_number=1890), _tc("kimi-gate-pr1890-1790155600", "failure")]
    report = ro.compute_outcomes(receipts)
    assert _decisions(report) == {"work-1": "accept"}
    assert report["outcomes"]["work-1"]["evidence"] == [
        {"kind": "gate_run", "gate": "kimi-gate-pr1890-1790155600", "pr": None, "result": "failure"}
    ]


# Deliverable "klaar"-punten (a)-(c), (e)-(g) at module level; (d) and (h) are
# test_noise_reasons_* and test_project_filter_* above.

def _b(did: str, status: str = "unknown", **kw: Any) -> Dict[str, Any]:
    unverified = {"method": "unknown", "tests_run": None, "tests_passed": None, "tests_failed": None}
    return _tc(did, status, report_file=f"{did}.md", verification=unverified, **kw)


def test_a_failure_of_a_then_unknown_of_b_is_reject():
    assert _decisions(ro.compute_outcomes([_tc("d1", "failure"), _b("d1")])) == {"d1": "reject"}


def test_b_three_bookkeeping_events_and_success_is_one_accept():
    report = ro.compute_outcomes([
        _ev("state_mutation", "d1"), _ev("review_gate_request", "d1", status="requested"),
        _ev("track_reconcile_advisory", "d1"), _tc("d1"),
    ])
    assert _decisions(report) == {"d1": "accept"} and report["bookkeeping"] == 3


def test_c_failure_then_success_under_same_id_is_accept():
    assert _decisions(ro.compute_outcomes([_tc("d1", "failure"), _tc("d1")])) == {"d1": "accept"}


def test_e_gate_fail_is_evidence_status_stays_success():
    gate = _ev("review_gate_result", "d1", receipt_kind="review_gate", gate="codex_gate",
               gate_status="fail", status="fail", pr_number=7)
    b_receipt = _b("d1")
    b_receipt["verification"] = VERIFIED
    outcome = ro.compute_outcomes([_tc("d1"), gate, b_receipt])["outcomes"]["d1"]
    assert (outcome["status"], outcome["decision"]) == ("success", "accept")
    assert outcome["evidence"][0]["result"] == "fail"


def test_f_accept_then_pr_merge_refused_is_investigate():
    outcome = ro.compute_outcomes([_tc("d1"), _ev("pr_merge_refused", "d1", status="blocked")])["outcomes"]["d1"]
    assert outcome["decision"] == "investigate" and "pr_merge_refused" in outcome["reason"]


def test_g_reject_then_accept_under_other_id_without_link_stays_reject():
    assert _decisions(ro.compute_outcomes([_tc("d1", "failure"), _tc("d2")])) == {"d1": "reject", "d2": "accept"}
