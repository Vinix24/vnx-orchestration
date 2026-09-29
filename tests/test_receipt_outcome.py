#!/usr/bin/env python3
"""fabric-state-herstel D3 — unit tests for `receipt_outcome.summarize`.

These pin the module's own rules; wiring the outcome into the digest is D3b.
ADR-007: every ledger here also carries a colliding dispatch id from a second
project that must not leak into the outcome.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import receipt_outcome as ro

GOOD = {"method": "pytest", "tests_run": 3, "tests_passed": 3, "tests_failed": 0}
TS = "2026-09-29T10:00:00Z"
FOREIGN = {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": "d1",
           "status": "failure", "project_id": "other-project", "timestamp": TS}


def _a(did: str, status: str, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": TS, **kw}


def _b(did: str, status: str = "done", verification: Any = None, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "report_file": f"{did}.md", "timestamp": TS,
            "verification": verification or GOOD, **kw}


def _run(*receipts: Dict[str, Any], cutoff: Any = None) -> Dict[str, Any]:
    ledger: List[Dict[str, Any]] = [FOREIGN, *receipts, FOREIGN]
    return ro.summarize(ledger, project_id="vnx-dev", cutoff=cutoff)


def _outcome(result: Dict[str, Any], did: str) -> Dict[str, Any]:
    return next(o for o in result["outcomes"] if o["dispatch_id"] == did)


def test_gate_dispatches_group_on_pr_and_link_to_the_work_dispatch():
    result = _run(
        _a("d1", "success", pr_id="42"), _b("d1"),
        _a("kimi-gate-pr42-1790000000", "success", branch="dispatch/other"),
        _a("glm-gate-pr42-1790000100", "failure"),
    )
    assert result["verdict_counts"]["accept"] == 1
    assert [o["dispatch_id"] for o in result["outcomes"]] == ["d1"]
    gates = [(e["gate"], e["pr"], e["status"]) for e in _outcome(result, "d1")["evidence"]]
    assert gates == [("kimi_gate", "42", "success"), ("glm_gate", "42", "failure")]
    # A gate-runner receipt never links its owner to another dispatch.
    assert result["verdict_counts"]["superseded"] == 0


def test_gate_dispatch_without_work_dispatch_is_counted_apart():
    result = _run(_a("kimi-gate-pr9998-1789000000", "failure"))
    assert result["outcomes"] == []
    assert result["unlinked_gate_evidence"] == 1


def test_noise_rules():
    result = _run(
        _a("DISP-007", "failed", source="pytest"),
        _a("m1", "failed", payload="<MagicMock id='1'>"),
        _a("unknown", "failed"), _a("?", "failed"), _a("", "failed"),
        _a("t1", "failed", report_path="/tmp/pytest-7/r.md"),
    )
    assert result["outcomes"] == []
    assert result["noise_counts"] == {"pytest": 1, "magicmock": 1, "missing_dispatch_id": 3,
                                      "temp_report_path": 1, "foreign_project": 2}


def test_synthesized_lane_is_not_filtered():
    result = _run(
        {"event_type": "subprocess_completion", "receipt_kind": "dispatch", "timestamp": TS,
         "dispatch_id": "plan-gate-x-opus-1", "status": "done",
         "source": "tmux_interactive_lane_synthesized"},
        _b("plan-gate-x-opus-1"),
    )
    assert result["verdict_counts"]["accept"] == 1


def test_contract_invalid_after_the_last_a_wins_as_reject():
    result = _run(
        _a("d1", "success"), _b("d1"),
        {"event_type": "report_contract_invalid", "receipt_kind": "dispatch", "timestamp": TS,
         "dispatch_id": "d1", "status": "contract_invalid"},
    )
    assert result["verdict_counts"]["reject"] == 1
    assert "report_contract_invalid" in _outcome(result, "d1")["reason"]


def test_retry_after_contract_invalid_decides_again():
    result = _run(
        _a("d1", "failure"),
        {"event_type": "report_contract_invalid", "dispatch_id": "d1", "timestamp": TS,
         "status": "contract_invalid"},
        _a("d1", "success"), _b("d1"),
    )
    assert result["verdict_counts"]["accept"] == 1


def test_phantom_guard_overturns_a_claimed_success():
    result = _run(
        _a("d1", "success"), _b("d1"),
        {"event_type": "subprocess_completion", "receipt_kind": "dispatch", "dispatch_id": "d1",
         "status": "failed", "source": "phantom_guard", "timestamp": TS},
    )
    assert result["verdict_counts"]["reject"] == 1


def test_reopened_obligation_resolved_by_later_gate_result_for_that_gate():
    reopened = {"event_type": "gate_obligation_reopened_stale_evidence", "dispatch_id": "d1",
                "gate": "codex_gate", "pr_number": 42, "status": "reopened", "timestamp": TS}
    other_gate = {"event_type": "review_gate_result", "dispatch_id": "d1", "gate": "kimi_gate",
                  "pr_id": "42", "status": "completed", "timestamp": TS}
    same_gate = dict(other_gate, gate="codex_gate")
    assert _run(_a("d1", "success"), _b("d1"), reopened,
                other_gate)["verdict_counts"]["investigate"] == 1
    assert _run(_a("d1", "success"), _b("d1"), reopened,
                same_gate)["verdict_counts"]["accept"] == 1


def test_door_bookkeeping_failed_is_never_resolved_and_blocking_without_id_uses_pr():
    result = _run(
        _a("d1", "success", pr_id="42"), _b("d1"),
        {"event_type": "door_bookkeeping_failed", "dispatch_id": "d1", "timestamp": TS},
        {"event_type": "pr_merged", "dispatch_id": "d1", "pr_number": 42, "timestamp": TS},
    )
    assert result["verdict_counts"]["investigate"] == 1
    orphan = _run(
        _a("d2", "success", pr_id="43"), _b("d2"),
        {"event_type": "pr_merge_refused", "pr_number": 43, "status": "blocked", "timestamp": TS},
    )
    assert orphan["verdict_counts"]["investigate"] == 1


def test_blocking_before_the_last_outcome_does_not_count():
    result = _run(
        {"event_type": "pr_merge_refused", "dispatch_id": "d1", "pr_number": 42, "timestamp": TS},
        _a("d1", "success"), _b("d1"),
    )
    assert result["verdict_counts"]["accept"] == 1


def test_window_reports_recent_dispatches_but_reads_full_history():
    cutoff = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
    result = _run(
        _a("old", "failure", timestamp="2026-09-20T00:00:00Z"),
        _a("d1", "failure", timestamp="2026-09-28T00:00:00Z"),
        _a("d1", "success", timestamp=1790000000), _b("d1", timestamp=TS),
        cutoff=cutoff,
    )
    assert [o["dispatch_id"] for o in result["outcomes"]] == ["d1"]
    assert result["verdict_counts"]["accept"] == 1


def test_evidence_only_dispatch_is_unknown_and_bookkeeping_is_counted():
    result = _run(
        {"event_type": "review_gate_request", "dispatch_id": "d1", "timestamp": TS},
        {"event_type": "pr_merged", "dispatch_id": "d1", "pr_number": 42, "timestamp": TS},
        {"event_type": "state_mutation", "timestamp": TS},
    )
    assert result["verdict_counts"]["unknown"] == 1
    assert result["bookkeeping_counts"] == {"review_gate_request": 1, "state_mutation": 1}


def test_refused_merge_without_pr_is_not_resolved_by_a_merge_without_pr():
    refused = {"event_type": "pr_merge_refused", "dispatch_id": "d1", "status": "blocked",
               "timestamp": TS}
    merged = {"event_type": "pr_merged", "dispatch_id": "d1", "timestamp": TS}
    result = _run(_a("d1", "success"), _b("d1"), refused, merged)
    assert result["verdict_counts"]["investigate"] == 1
    assert "pr_merge_refused" in _outcome(result, "d1")["reason"]


def test_refused_merge_is_resolved_by_a_merge_on_the_same_pr():
    refused = {"event_type": "pr_merge_refused", "dispatch_id": "d1", "pr_number": 42,
               "status": "blocked", "timestamp": TS}
    assert _run(_a("d1", "success"), _b("d1"), refused,
                {"event_type": "pr_merged", "dispatch_id": "d1", "pr_number": 42,
                 "timestamp": TS})["verdict_counts"]["accept"] == 1
    assert _run(_a("d1", "success"), _b("d1"), refused,
                {"event_type": "pr_merged", "dispatch_id": "d1", "pr_number": 43,
                 "timestamp": TS})["verdict_counts"]["investigate"] == 1


def test_reopened_obligation_without_pr_stays_open():
    reopened = {"event_type": "gate_obligation_reopened_stale_evidence", "dispatch_id": "d1",
                "gate": "codex_gate", "status": "reopened", "timestamp": TS}
    result_line = {"event_type": "review_gate_result", "dispatch_id": "d1", "gate": "codex_gate",
                   "status": "completed", "timestamp": TS}
    result = _run(_a("d1", "success"), _b("d1"), reopened, result_line)
    assert result["verdict_counts"]["investigate"] == 1


def test_out_of_range_numeric_timestamp_is_unknown_but_keeps_file_order():
    cutoff = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
    for bad in (10**20, -(10**20), float("nan")):
        result = _run(_a("d1", "failure"), _a("d1", "success", timestamp=bad), _b("d1"),
                      cutoff=cutoff)
        assert result["verdict_counts"]["accept"] == 1, bad
        only_bad = _run(_a("d2", "failure", timestamp=bad), cutoff=cutoff)
        assert only_bad["outcomes"] == [], bad
        assert _run(_a("d2", "failure", timestamp=bad))["verdict_counts"]["reject"] == 1, bad


def test_contract_invalid_before_the_last_a_does_not_reject():
    result = _run(
        {"event_type": "report_contract_invalid", "receipt_kind": "dispatch", "timestamp": TS,
         "dispatch_id": "d1", "status": "contract_invalid"},
        _a("d1", "success"), _b("d1"),
    )
    assert result["verdict_counts"] == {"accept": 1, "investigate": 0, "reject": 0,
                                        "superseded": 0, "unknown": 0}


def _foreign(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return dict(receipt, project_id="other-project")


def test_task_failed_alone_is_a_failure_whatever_its_status():
    for status in ("failure", "done"):
        failed = {"event_type": "task_failed", "receipt_kind": "dispatch", "dispatch_id": "d1",
                  "status": status, "timestamp": TS}
        result = _run(_foreign(dict(failed, dispatch_id="d2")), failed)
        assert result["verdict_counts"]["reject"] == 1, status
        assert [o["dispatch_id"] for o in result["outcomes"]] == ["d1"], status


def test_task_failed_after_a_success_overturns_it():
    failed = {"event_type": "task_failed", "receipt_kind": "dispatch", "dispatch_id": "d1",
              "status": "failure", "timestamp": TS}
    result = _run(_a("d1", "success"), _b("d1"), failed, _foreign(_a("d1", "success")))
    assert result["verdict_counts"]["reject"] == 1


def test_task_timeout_terminal_is_a_failure_and_pending_is_no_outcome():
    timeout = {"event_type": "task_timeout", "receipt_kind": "dispatch", "dispatch_id": "d1",
               "timestamp": TS}
    for status in ("timeout", "stalled", ""):
        result = _run(_foreign(dict(timeout, status="no_confirmation")),
                      dict(timeout, status=status))
        assert result["verdict_counts"]["reject"] == 1, status
    pending = dict(timeout, status="no_confirmation")
    assert _run(pending)["verdict_counts"]["unknown"] == 1
    after_success = _run(_a("d1", "success"), _b("d1"), pending,
                         _foreign(dict(timeout, status="timeout")))
    assert after_success["verdict_counts"]["accept"] == 1


def test_task_completed_is_an_outcome_receipt():
    completed = {"event_type": "task_completed", "receipt_kind": "dispatch", "dispatch_id": "d1",
                 "status": "failed", "timestamp": TS}
    result = _run(_foreign(dict(completed, status="success")), completed)
    assert result["verdict_counts"]["reject"] == 1


def test_pr_zero_is_no_pr_number():
    foreign_owner = _foreign(_a("d2", "success", pr_id="0"))
    result = _run(
        foreign_owner,
        _a("d1", "success", pr_id="0"), _b("d1"),
        _a("codex-gate-pr0-123", "failure"),
        {"event_type": "review_gate_result", "dispatch_id": "unknown", "gate": "kimi_gate",
         "pr_id": "#0", "status": "completed", "timestamp": TS},
    )
    assert _outcome(result, "d1")["evidence"] == []
    assert result["unlinked_gate_evidence"] == 1
    assert result["noise_counts"]["missing_dispatch_id"] == 1
    for zero in (0, "0", "#0"):
        refused = {"event_type": "pr_merge_refused", "dispatch_id": "d1", "pr_number": zero,
                   "status": "blocked", "timestamp": TS}
        merged = {"event_type": "pr_merged", "dispatch_id": "d1", "pr_number": zero,
                  "timestamp": TS}
        blocked = _run(_a("d1", "success"), _b("d1"), refused, merged, _foreign(merged))
        assert blocked["verdict_counts"]["investigate"] == 1, zero
        assert _outcome(blocked, "d1")["blocking"][0]["pr"] is None, zero


def test_pr_owner_is_the_first_work_dispatch_that_names_the_pr():
    result = _run(
        _foreign(_a("d1-ff", "success", pr_id="42")),
        _a("d1", "success", pr_id="42"), _b("d1"),
        _a("kimi-gate-pr42-1790000000", "failure"),
        _a("d1-ff", "success", pr_id="42", branch="dispatch/d1-ff"), _b("d1-ff"),
        {"event_type": "review_gate_result", "dispatch_id": "", "gate": "codex_gate",
         "pr_id": "42", "status": "completed", "timestamp": TS},
    )
    assert [e["dispatch_id"] for e in _outcome(result, "d1")["evidence"]] == [
        "kimi-gate-pr42-1790000000", ""]
    assert _outcome(result, "d1-ff")["evidence"] == []
