"""fabric-state-herstel D4a: weekly_digest counts dispatches through `receipt_outcome`.

The digest used to classify every ledger line on its own status: a dispatch that
writes two task_complete receipts counted twice, and bookkeeping counted as
unknown. `dispatch_outcomes` now counts one outcome per dispatch.
ADR-007: every ledger also carries the same dispatch ids from a second project,
which must not leak into the counts.
"""
from __future__ import annotations

import json
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PROJECT = "vnx-dev"
OTHER = "other-project"
GOOD = {"method": "pytest", "tests_run": 3, "tests_passed": 3, "tests_failed": 0}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_NOW = datetime.now(tz=timezone.utc)
_RECENT = _iso(_NOW - timedelta(hours=1))
_RECENT2 = _iso(_NOW - timedelta(minutes=30))
_OLD = _iso(_NOW - timedelta(days=10))
_FROZEN = _iso(_NOW - timedelta(days=20))


def _a(did: str, status: str, ts: str = _RECENT, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": ts, "project_id": PROJECT, **kw}


def _b(did: str, status: str = "done", ts: str = _RECENT, verification: Any = None,
       **kw: Any) -> Dict[str, Any]:
    return _a(did, status, ts, report_file=f"{did}.md", verification=verification or GOOD, **kw)


def _outcomes(records: List[Dict[str, Any]], *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
              days: int = 7) -> Dict[str, Any]:
    """Run collect_metrics against a tmp ledger; no DB and no pending edits."""
    import weekly_digest

    receipts_path = tmp_path / "state" / "t0_receipts.ndjson"
    receipts_path.parent.mkdir(parents=True, exist_ok=True)
    receipts_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    with (
        mock.patch.object(weekly_digest, "STATE_DIR", receipts_path.parent),
        mock.patch.object(weekly_digest, "RECEIPTS_PATH", receipts_path),
        mock.patch.object(weekly_digest, "DB_PATH", tmp_path / "nonexistent.db"),
        mock.patch.object(weekly_digest, "PENDING_PATH", tmp_path / "nonexistent.json"),
    ):
        return weekly_digest.collect_metrics(days=days)["dispatch_outcomes"]


def _counts(out: Dict[str, Any]) -> tuple:
    return out["total"], out["success"], out["failure"], out["unknown"]


def test_two_task_completes_of_one_dispatch_count_once(tmp_path, monkeypatch):
    out = _outcomes([_a("d1", "success"), _b("d1"),
                     _a("d1", "failure", project_id=OTHER)], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 1, 0, 0)


def test_later_failure_after_success_counts_one_failure(tmp_path, monkeypatch):
    out = _outcomes([_a("d1", "success", _RECENT), _a("d1", "failure", _RECENT2)],
                    tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 0, 1, 0)


def test_writer_b_done_does_not_hide_a_lane_failure(tmp_path, monkeypatch):
    out = _outcomes([_a("d1", "failure"), _b("d1", "done")], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 0, 1, 0)


def test_contract_invalid_after_the_last_lane_status_is_a_failure(tmp_path, monkeypatch):
    invalid = {"event_type": "report_contract_invalid", "dispatch_id": "d1",
               "status": "contract_invalid", "timestamp": _RECENT2, "project_id": PROJECT}
    out = _outcomes([_a("d1", "success"), invalid], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 0, 1, 0)


def test_lane_success_without_verification_counts_as_unknown_not_success(tmp_path, monkeypatch):
    out = _outcomes([_a("d1", "success"), _b("d1", "unknown", verification={"method": "unknown"})],
                    tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 0, 0, 1)
    assert out["verdict_counts"]["investigate"] == 1


@pytest.mark.parametrize("event_type", ["state_mutation", "review_gate_request"])
def test_bookkeeping_is_not_a_dispatch(event_type, tmp_path, monkeypatch):
    records = [{"event_type": event_type, "dispatch_id": "d1", "status": "success",
                "timestamp": _RECENT, "project_id": PROJECT}]
    out = _outcomes(records, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (0, 0, 0, 0)


def test_lines_without_a_dispatch_id_are_noise(tmp_path, monkeypatch):
    out = _outcomes([_a("unknown", "success"), _a("", "failure")],
                    tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (0, 0, 0, 0)


def test_test_noise_is_not_counted(tmp_path, monkeypatch):
    out = _outcomes([_a("real", "success"), _b("real"),
                     _a("leak-a", "success", source="pytest"),
                     _a("leak-b", "success", report_path="/var/folders/xx/r.md"),
                     _a("leak-c", "failure", title="<MagicMock id='4'>")],
                    tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 1, 0, 0)


def test_foreign_project_with_a_colliding_id_never_leaks(tmp_path, monkeypatch):
    out = _outcomes([_a("shared", "failure", project_id=OTHER), _a("shared", "failure", project_id=OTHER),
                     _a("mine", "success"), _b("mine")], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 1, 0, 0)


def test_window_reports_dispatches_touched_inside_it(tmp_path, monkeypatch):
    out = _outcomes([_a("old", "success", _OLD), _b("old", ts=_OLD), _a("retried", "failure", _OLD),
                     _a("retried", "success", _RECENT), _b("retried", ts=_RECENT)], tmp_path=tmp_path, monkeypatch=monkeypatch, days=7)
    assert _counts(out) == (1, 1, 0, 0)


def test_unknown_status_is_unknown(tmp_path, monkeypatch):
    out = _outcomes([_a("d1", "bananas")], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (1, 0, 0, 1)


def test_failed_and_timeout_events_are_failures(tmp_path, monkeypatch):
    records = [{"event_type": "task_failed", "dispatch_id": "f1", "status": "failed",
                "timestamp": _RECENT, "project_id": PROJECT},
               {"event_type": "task_timeout", "dispatch_id": "t1", "status": "timeout",
                "timestamp": _RECENT, "project_id": PROJECT}]
    out = _outcomes(records, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (2, 0, 2, 0)


def test_mixed_ledger_counts_dispatches_not_lines(tmp_path, monkeypatch):
    records = [
        _a("ok1", "success"), _b("ok1"),
        _a("ok2", "success"), _b("ok2"),
        _a("bad", "failure"), _b("bad", "done"),
        _a("odd", "bananas"),
        {"event_type": "state_mutation", "dispatch_id": "ok1", "timestamp": _RECENT, "project_id": PROJECT},
    ]
    out = _outcomes(records, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (4, 2, 1, 1)


def test_empty_and_missing_ledger(tmp_path, monkeypatch):
    out = _outcomes([], tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert _counts(out) == (0, 0, 0, 0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_frozen_contract_invalid_batch_is_not_counted(tmp_path, monkeypatch):
    old_ci = {"event_type": "report_contract_invalid", "dispatch_id": "frozen", "status": "contract_invalid",
              "project_id": PROJECT, "timestamp": _FROZEN, "ingested_at": _FROZEN}
    out = _outcomes([old_ci, {**old_ci, "project_id": OTHER}, _a("d1", "success"), _b("d1")],
                    tmp_path=tmp_path, monkeypatch=monkeypatch, days=30)
    assert _counts(out) == (1, 1, 0, 0)


def test_fresh_contract_invalid_with_an_old_report_timestamp_is_counted(tmp_path, monkeypatch):
    ci = {"event_type": "report_contract_invalid", "dispatch_id": "d1", "status": "contract_invalid",
          "project_id": PROJECT, "timestamp": _iso(_NOW - timedelta(days=30)), "ingested_at": _RECENT}
    out = _outcomes([ci, {**ci, "project_id": OTHER, "dispatch_id": "d1"}],
                    tmp_path=tmp_path, monkeypatch=monkeypatch, days=7)
    assert _counts(out) == (1, 0, 1, 0)
