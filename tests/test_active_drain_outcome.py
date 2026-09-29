"""fabric-state-herstel D4a: check_active_drain reads `receipt_outcome`.

The drain let a failure win over everything: an old failure stayed a failure
after a successful retry, and a foreign project's failure under a colliding
dispatch id dead-lettered a live dispatch. It now folds the processed receipts
of a dispatch through `receipt_outcome.summarize`.
ADR-007: the ledgers carry a colliding id from a second project.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from check_active_drain import build_receipt_status_index, drain_active  # noqa: E402

PROJECT = "vnx-dev"
OTHER = "other-project"
GOOD = {"method": "pytest", "tests_run": 3, "tests_passed": 3, "tests_failed": 0}


@pytest.fixture(autouse=True)
def _project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


def _store(tmp_path: Path) -> Path:
    data = tmp_path / ".vnx-data"
    for sub in ("active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    return data


def _write(data: Path, seq: int, receipt: Dict[str, Any]) -> None:
    name = f"{1780000000 + seq}-{receipt.get('dispatch_id') or 'none'}-{seq}.json"
    (data / "receipts" / "processed" / name).write_text(json.dumps(receipt), encoding="utf-8")


def _a(did: str, status: str, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": "2026-09-29T10:00:00Z", "project_id": PROJECT, **kw}


def _b(did: str, status: str = "done", verification: Any = None, **kw: Any) -> Dict[str, Any]:
    return _a(did, status, report_file=f"{did}.md", verification=verification or GOOD, **kw)


def _index(tmp_path: Path, receipts: List[Dict[str, Any]]) -> Dict[str, str]:
    data = _store(tmp_path)
    for seq, receipt in enumerate(receipts):
        _write(data, seq, receipt)
    return build_receipt_status_index(data / "receipts")


def test_old_failure_with_a_successful_retry_is_a_success(tmp_path: Path) -> None:
    assert _index(tmp_path, [_a("d1", "failure"), _a("d1", "success"), _b("d1")]) == {"d1": "success"}


def test_failure_after_an_earlier_success_is_a_failure(tmp_path: Path) -> None:
    assert _index(tmp_path, [_a("d1", "success"), _b("d1"), _a("d1", "failure")]) == {"d1": "failure"}


def test_writer_b_done_does_not_hide_a_lane_failure(tmp_path: Path) -> None:
    assert _index(tmp_path, [_a("d1", "failure"), _b("d1", "done")]) == {"d1": "failure"}


def test_lane_success_with_unknown_verification_is_investigate_not_success(tmp_path: Path) -> None:
    receipts = [_a("d1", "success"), _b("d1", "unknown", {"method": "unknown"})]
    assert _index(tmp_path, receipts) == {"d1": "investigate"}


def test_contract_invalid_after_the_last_lane_status_is_a_failure(tmp_path: Path) -> None:
    invalid = {"event_type": "report_contract_invalid", "dispatch_id": "d1",
               "status": "contract_invalid", "project_id": PROJECT,
               "timestamp": "2026-09-29T10:05:00Z"}
    assert _index(tmp_path, [_a("d1", "success"), invalid]) == {"d1": "failure"}


def test_unrecognised_lane_status_is_investigate(tmp_path: Path) -> None:
    assert _index(tmp_path, [_a("d1", "bananas")]) == {"d1": "investigate"}


def test_foreign_project_with_a_colliding_id_never_leaks(tmp_path: Path) -> None:
    receipts = [_a("shared", "failure", project_id=OTHER), _a("mine", "success"), _b("mine"),
                _a("shared", "failure", project_id=OTHER)]
    assert _index(tmp_path, receipts) == {"mine": "success"}


def test_test_noise_and_bookkeeping_and_id_less_lines_make_no_status(tmp_path: Path) -> None:
    receipts = [
        _a("leak-a", "failure", source="pytest"),
        _a("leak-b", "failure", report_path="/var/folders/xx/r.md"),
        _a("leak-c", "failure", title="<MagicMock id='3'>"),
        {"event_type": "state_mutation", "dispatch_id": "d1", "status": "failure",
         "project_id": PROJECT},
        {"event_type": "review_gate_request", "dispatch_id": "d1", "status": "requested",
         "project_id": PROJECT},
        _a("unknown", "failure"),
        _a("", "failure"),
    ]
    assert _index(tmp_path, receipts) == {}


def test_receipts_fold_in_file_name_order_not_directory_order(tmp_path: Path) -> None:
    data = _store(tmp_path)
    processed = data / "receipts" / "processed"
    (processed / "1780000002-d1-2.json").write_text(json.dumps(_a("d1", "success")), encoding="utf-8")
    (processed / "1780000003-d1-3.json").write_text(json.dumps(_b("d1")), encoding="utf-8")
    (processed / "1780000001-d1-1.json").write_text(json.dumps(_a("d1", "failure")), encoding="utf-8")
    assert build_receipt_status_index(data / "receipts") == {"d1": "success"}


def test_malformed_and_non_dict_files_are_skipped(tmp_path: Path) -> None:
    data = _store(tmp_path)
    processed = data / "receipts" / "processed"
    (processed / "1780000001-bad-1.json").write_text("{not json", encoding="utf-8")
    (processed / "1780000002-list-2.json").write_text("[1, 2]", encoding="utf-8")
    _write(data, 3, _a("d1", "success"))
    _write(data, 4, _b("d1"))
    assert build_receipt_status_index(data / "receipts") == {"d1": "success"}


def _active(data: Path, did: str, hours_old: float = 2.0) -> None:
    d = data / "dispatches" / "active" / did
    d.mkdir(parents=True)
    ts = datetime.now(tz=timezone.utc) - timedelta(hours=hours_old)
    (d / "manifest.json").write_text(json.dumps({"dispatch_id": did, "timestamp": ts.isoformat()}),
                                     encoding="utf-8")


def test_drain_completes_a_dispatch_whose_retry_succeeded(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1")
    for seq, receipt in enumerate([_a("d1", "failure"), _a("d1", "success"), _b("d1")]):
        _write(data, seq, receipt)
    results = drain_active(data)
    assert [(r.dispatch_id, r.action) for r in results] == [("d1", "completed")]
    assert (data / "dispatches" / "completed" / "d1").is_dir()


def test_drain_dead_letters_a_dispatch_with_a_later_failure(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1")
    for seq, receipt in enumerate([_a("d1", "success"), _b("d1"), _a("d1", "failure")]):
        _write(data, seq, receipt)
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "dead_letter")]


def test_drain_leaves_a_young_dispatch_with_only_bookkeeping_alone(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1", hours_old=0.1)
    _write(data, 0, {"event_type": "state_mutation", "dispatch_id": "d1", "project_id": PROJECT})
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "skipped")]


def _fresh() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_drain_leaves_a_success_without_verification_in_active(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1")
    _active(data, "d2")
    for seq, receipt in enumerate([_a("d1", "success"), _b("d1", "unknown", {"method": "unknown"}),
                                   _a("d1", "success", project_id=OTHER), _a("d2", "success"), _b("d2")]):
        _write(data, seq, receipt)
    results = {r.dispatch_id: r.action for r in drain_active(data)}
    assert results == {"d1": "skipped", "d2": "completed"}
    assert (data / "dispatches" / "active" / "d1").is_dir()


def test_frozen_contract_invalid_does_not_dead_letter_an_active_dispatch(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1", hours_old=0.1)
    old_ci = {"event_type": "report_contract_invalid", "dispatch_id": "d1", "status": "contract_invalid",
              "project_id": PROJECT, "timestamp": "2026-01-01T00:00:00Z"}
    _write(data, 0, old_ci)
    _write(data, 1, {**old_ci, "project_id": OTHER})
    assert build_receipt_status_index(data / "receipts") == {}
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "skipped")]


def test_fresh_contract_invalid_still_dead_letters_an_active_dispatch(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1", hours_old=0.1)
    _write(data, 0, {"event_type": "report_contract_invalid", "dispatch_id": "d1",
                     "status": "contract_invalid", "project_id": PROJECT,
                     "timestamp": "2026-01-01T00:00:00Z", "ingested_at": _fresh()})
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "dead_letter")]


def _status_only_ci(did: str, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": "contract_invalid", "project_id": PROJECT, **kw}


def test_frozen_status_only_contract_invalid_does_not_dead_letter_an_active_dispatch(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1", hours_old=0.1)
    old_ci = _status_only_ci("d1", timestamp="2026-01-01T00:00:00Z")
    _write(data, 0, old_ci)
    _write(data, 1, {**old_ci, "project_id": OTHER, "ingested_at": _fresh()})
    assert build_receipt_status_index(data / "receipts") == {}
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "skipped")]
    assert (data / "dispatches" / "active" / "d1").is_dir()


def test_fresh_status_only_contract_invalid_after_success_dead_letters(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d1", hours_old=0.1)
    receipts = [_a("d1", "success"), _b("d1"),
                _status_only_ci("d1", report_file="d1.md", timestamp="2026-01-01T00:00:00Z",
                                ingested_at=_fresh()),
                _a("d1", "failure", project_id=OTHER)]
    for seq, receipt in enumerate(receipts):
        _write(data, seq, receipt)
    assert build_receipt_status_index(data / "receipts") == {"d1": "failure"}
    assert [(r.dispatch_id, r.action) for r in drain_active(data)] == [("d1", "dead_letter")]
