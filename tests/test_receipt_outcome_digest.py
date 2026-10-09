#!/usr/bin/env python3
"""fabric-state-herstel D3b — `receipt_query digest` counts one outcome per dispatch.

Behaviour tests (a)-(h) from the plan, driven through the real
`compute_digest` on a real ledger file. They import only `receipt_query` and
read every result key with `.get`, so they run unchanged on the pre-D3b digest
and fail there on the counts (the red run), not on a missing symbol or key.

ADR-007: every ledger written here carries, for each receipt, a twin from a
second project (`other-project`) with the SAME dispatch id and a hard-failure
status. The digest runs on its default project (`vnx-dev`). If the project
filter leaked, those twins would turn up as rejects.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

import receipt_query as rq
from receipt_verdict import compute_verdict

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
TS = "2026-09-29T10:00:00Z"
GOOD = {"method": "pytest", "tests_run": 5, "tests_passed": 5, "tests_failed": 0}
UNKNOWN = {"method": "unknown", "tests_run": None, "tests_passed": None, "tests_failed": None}


class _NoOIM:
    def load_items(self):
        raise AssertionError("no oi_pending warnings in these ledgers")

    def _find_by_dedup_key(self, data, key):
        raise AssertionError("no oi_pending warnings in these ledgers")


def _stamp(receipt: Dict[str, Any]) -> Dict[str, Any]:
    # The writer stamps compute_verdict on every line (receipt_finalize.py).
    receipt.setdefault("timestamp", TS)
    receipt.setdefault("schema_version", 2)
    receipt["verdict"] = compute_verdict(receipt)
    return receipt


def _a(did: str, status: str, **kw: Any) -> Dict[str, Any]:
    return _stamp({"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
                   "status": status, "verification": {"method": "pending-report"}, **kw})


def _b(did: str, status: str, verification: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    return _stamp({"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
                   "status": status, "report_file": f"{did}.md", "verification": verification, **kw})


def _event(did: str, event_type: str, status: Any, **kw: Any) -> Dict[str, Any]:
    return _stamp({"event_type": event_type, "dispatch_id": did, "status": status, **kw})


def _digest(tmp_path: Path, *receipts: Dict[str, Any]) -> Dict[str, Any]:
    lines = []
    for receipt in receipts:
        lines.append(receipt)
        twin = dict(receipt, project_id="other-project", status="failure")
        lines.append(_stamp(twin))
    (tmp_path / rq.LEDGER_NAME).write_text(
        "".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8",
    )
    return rq.compute_digest(
        tmp_path / rq.LEDGER_NAME, window="24h", now=NOW,
        open_items_manager_module=_NoOIM(),
        project_id="vnx-dev",
    )


def _counts(result: Dict[str, Any]) -> Dict[str, int]:
    vc = result["verdict_counts"]
    return {k: vc.get(k, 0) for k in ("accept", "investigate", "reject", "superseded")}


def _outcome(result: Dict[str, Any], did: str) -> Optional[Dict[str, Any]]:
    return next((o for o in result.get("outcomes", []) if o["dispatch_id"] == did), None)


def test_a_failure_then_unknown_from_b_is_one_reject(tmp_path):
    result = _digest(tmp_path, _a("d1", "failure"), _b("d1", "unknown", UNKNOWN))
    assert _counts(result) == {"accept": 0, "investigate": 0, "reject": 1, "superseded": 0}


def test_b_three_bookkeeping_events_and_a_success_is_one_accept(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "success"),
        _event("d1", "review_gate_request", "requested", receipt_kind="review_gate"),
        _b("d1", "done", GOOD),
        _event("d1", "state_mutation", None, receipt_kind="state_mutation"),
        _event("d1", "roadmap_dispatch_step", None),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}
    assert sum(result.get("bookkeeping_counts", {}).values()) == 3


def test_c_failure_then_success_under_same_id_is_accept(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "failure"), _b("d1", "unknown", UNKNOWN),
        _a("d1", "success"), _b("d1", "done", GOOD),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}


def test_d_test_row_with_temp_report_path_does_not_count(tmp_path):
    result = _digest(
        tmp_path,
        _a("20260527-tmuxint-test", "failed",
           report_path="/var/folders/xy/T/pytest-1/unified_reports/r.md"),
        _a("d1", "success"), _b("d1", "done", GOOD),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}
    assert result.get("noise_counts", {}).get("temp_report_path", 0) >= 1


def test_e_gate_fail_is_evidence_not_the_dispatch_status(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "success", pr_id="42"),
        _event("d1", "review_gate_result", "failed", receipt_kind="review_gate",
               gate="kimi_gate", pr_id="42", verification={"method": "gate_review"}),
        _b("d1", "unknown", UNKNOWN),
    )
    assert _counts(result) == {"accept": 0, "investigate": 1, "reject": 0, "superseded": 0}
    outcome = _outcome(result, "d1")
    assert outcome is not None and outcome["status"] == "success"
    assert outcome["evidence"] == [{"event_type": "review_gate_result", "gate": "kimi_gate",
                                    "pr": "42", "status": "failed", "dispatch_id": "d1"}]


def test_f_accept_then_pr_merge_refused_is_investigate(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "success", pr_id="42"), _b("d1", "done", GOOD),
        _event("d1", "pr_merge_refused", "blocked", receipt_kind="state_mutation", pr_number=42),
    )
    assert _counts(result) == {"accept": 0, "investigate": 1, "reject": 0, "superseded": 0}
    outcome = _outcome(result, "d1")
    assert outcome is not None and "pr_merge_refused" in outcome["reason"]


def test_f_refused_then_merged_is_accept_again(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "success", pr_id="42"), _b("d1", "done", GOOD),
        _event("d1", "pr_merge_refused", "blocked", receipt_kind="state_mutation", pr_number=42),
        _event("d1", "pr_merged", "success", receipt_kind="state_mutation", pr_number=42),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}


def test_f_refused_by_a_merge_runner_id_blocks_the_work_dispatch(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "success", pr_id="42"), _b("d1", "done", GOOD),
        _event("merge-runner-42", "pr_merge_refused", "blocked", pr_number=42),
    )
    assert _counts(result) == {"accept": 0, "investigate": 1, "reject": 0, "superseded": 0}
    assert [o["dispatch_id"] for o in result.get("outcomes", [])] == ["d1"]


def test_g_reject_then_accept_under_other_id_without_link_stays_reject(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "failure"),
        _a("d2", "success"), _b("d2", "done", GOOD),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 1, "superseded": 0}


def test_g_reject_with_parent_dispatch_link_is_superseded(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "failure"),
        _a("d2", "success", parent_dispatch="d1"), _b("d2", "done", GOOD),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 1}


def test_g_reject_with_branch_link_is_superseded(tmp_path):
    result = _digest(
        tmp_path,
        _a("d1", "failure", branch="dispatch/d1"),
        _a("d2", "success", branch="dispatch/d1"), _b("d2", "done", GOOD),
    )
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 1}


def test_h_receipt_of_another_project_does_not_count(tmp_path):
    result = _digest(tmp_path, _a("d1", "success"), _b("d1", "done", GOOD))
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}
    assert result.get("noise_counts", {}).get("foreign_project", 0) == 2


def test_h_other_project_digest_sees_only_its_own_twins(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "other-project")
    # Stamped with their own project: an unstamped line is the ledger's own.
    _digest(tmp_path, _a("d1", "success", project_id="vnx-dev"),
            _b("d1", "done", GOOD, project_id="vnx-dev"))
    rc = rq.main(["digest", "--state-dir", str(tmp_path), "--window", "36500d", "--json"])
    assert rc == 0
    other = json.loads(capsys.readouterr().out)
    assert _counts(other) == {"accept": 0, "investigate": 0, "reject": 1, "superseded": 0}
    assert other.get("noise_counts") == {"foreign_project": 2}


def test_digest_dates_the_switch_with_the_reader_epoch(tmp_path):
    result = _digest(tmp_path, _a("d1", "success"), _b("d1", "done", GOOD))
    assert result.get("outcome_reader_epoch") == "2026-09-29T00:00:00+00:00"


def test_digest_survives_non_object_lines_and_out_of_range_epochs(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    good = [_a("d1", "failure"), _a("d1", "success", timestamp=10**20), _b("d1", "done", GOOD)]
    ledger.write_text("[1, 2]\n\"text\"\n" + "".join(json.dumps(r) + "\n" for r in good),
                      encoding="utf-8")
    result = rq.compute_digest(ledger, window="24h", now=NOW, open_items_manager_module=_NoOIM(), project_id="vnx-dev")
    assert _counts(result) == {"accept": 1, "investigate": 0, "reject": 0, "superseded": 0}


def test_digest_cli_prints_per_dispatch_counts(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    _digest(tmp_path, _a("d1", "failure"), _b("d1", "unknown", UNKNOWN))
    rc = rq.main(["digest", "--project-id", "vnx-dev", "--state-dir", str(tmp_path), "--window", "36500d", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict_counts"]["reject"] == 1
    # The per-line tally still counts both writers, but not the foreign twins (ADR-007).
    assert out.get("line_verdict_counts", {}).get("reject") == 1
    assert out.get("line_verdict_counts", {}).get("investigate") == 1


def test_digest_cli_text_names_per_dispatch_and_project(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    _digest(tmp_path, _a("d1", "success"), _b("d1", "done", GOOD))
    rc = rq.main(["digest", "--project-id", "vnx-dev", "--state-dir", str(tmp_path), "--window", "36500d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "project=vnx-dev" in out
    assert "per dispatch: accept=1 investigate=0 reject=0 superseded=0" in out
