"""ADR-045 (audit-spoor-kern K-2a): dispatch_finished next to task_complete.

Dispatch-ID: dlv-279f054c2d3b

Two layers, both deterministic:

  - ``ledger_end_line.end_line`` picks the one end record of a dispatch attempt
    (``dispatch_finished`` first, a legacy completion record as fallback), and
    never treats ``dispatch_started`` or ``report_ingested`` as an end. Its
    outcome is fail-closed: an absent/malformed/unknown ``end_state`` is never a
    success, and a ``dispatch_finished`` line's raw ``status`` is never read.
  - every state/outcome reader this PR changes gives the same picture over a
    ledger of the new types and a legacy ledger with the same dispatches.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import ledger_end_line  # noqa: E402
from ledger_end_line import end_line, end_outcome, end_status  # noqa: E402

PROJECT = "vnx-dev"
OTHER_PROJECT = "other-project"
DISPATCH = "20261028-120000-k2a"

FINISH_COMPLETED = {
    "event_type": "dispatch_finished",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": 1,
    "end_state": "completed",
    "status": "success",
    "head_sha": "a" * 40,
}
LEGACY_COMPLETE = {
    "event_type": "task_complete",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "status": "success",
}
REPORT_ONLY = {
    "event_type": "report_ingested",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": 1,
    "end_pending": True,
}
STARTED = {
    "event_type": "dispatch_started",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": 1,
    "lifecycle_mode": "v3",
}


def _finished(attempt: int, end_state: str = "completed", **extra) -> dict:
    record = {
        "event_type": "dispatch_finished",
        "project_id": PROJECT,
        "dispatch_id": DISPATCH,
        "attempt": attempt,
        "end_state": end_state,
        "status": {"completed": "success", "failed": "failure"}.get(end_state),
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# end_line: which record is the end
# ---------------------------------------------------------------------------

def test_dispatch_finished_wins_over_a_legacy_completion_record() -> None:
    records = [LEGACY_COMPLETE, FINISH_COMPLETED]
    assert end_line(records, PROJECT, DISPATCH) is FINISH_COMPLETED


def test_a_legacy_ledger_still_returns_its_completion_record() -> None:
    # The legacy branch is never dropped: a ledger with only task_complete
    # answers exactly as it did before K-2a.
    assert end_line([LEGACY_COMPLETE], PROJECT, DISPATCH) is LEGACY_COMPLETE


def test_attempt_none_returns_the_highest_attempt_present() -> None:
    first = _finished(1)
    second = _finished(2, "failed")
    records = [first, second]
    assert end_line(records, PROJECT, DISPATCH) is second
    assert end_line(records, PROJECT, DISPATCH, attempt=1) is first


def test_a_record_without_attempt_belongs_to_attempt_one() -> None:
    attempted = _finished(2)
    unmarked = dict(LEGACY_COMPLETE)
    assert end_line([unmarked, attempted], PROJECT, DISPATCH, attempt=1) is unmarked


def test_another_projects_line_with_the_same_dispatch_id_is_never_returned() -> None:
    foreign = dict(FINISH_COMPLETED, project_id=OTHER_PROJECT, end_state="failed")
    records = [foreign, LEGACY_COMPLETE]
    assert end_line(records, PROJECT, DISPATCH) is LEGACY_COMPLETE
    assert end_line(records, OTHER_PROJECT, DISPATCH) is foreign


def test_a_legacy_record_without_project_id_stays_readable() -> None:
    legacy = {k: v for k, v in LEGACY_COMPLETE.items() if k != "project_id"}
    assert end_line([legacy], PROJECT, DISPATCH) is legacy


def test_report_ingested_alone_is_never_an_end() -> None:
    assert end_line([REPORT_ONLY], PROJECT, DISPATCH) is None


def test_dispatch_started_is_never_an_end() -> None:
    assert end_line([STARTED], PROJECT, DISPATCH) is None


def test_a_newer_attempt_without_an_end_does_not_fall_back_to_an_older_end() -> None:
    first = _finished(1)
    report_for_second = dict(REPORT_ONLY, attempt=2)
    assert end_line([first, report_for_second], PROJECT, DISPATCH) is None


def test_a_malformed_attempt_is_not_folded_into_attempt_one() -> None:
    malformed = dict(FINISH_COMPLETED, attempt="two")
    assert end_line([malformed], PROJECT, DISPATCH) is None


def test_an_explicit_attempt_outside_the_records_returns_none() -> None:
    assert end_line([_finished(1)], PROJECT, DISPATCH, attempt=3) is None


def test_last_appended_end_of_the_winning_type_wins() -> None:
    first = _finished(1, "failed")
    second = _finished(1, "completed")
    assert end_line([first, second], PROJECT, DISPATCH) is second


def test_empty_dispatch_id_returns_none() -> None:
    assert end_line([FINISH_COMPLETED], PROJECT, "") is None
    assert end_line([FINISH_COMPLETED], PROJECT, None) is None


# ---------------------------------------------------------------------------
# end_status / end_outcome: fail-closed on evidence
# ---------------------------------------------------------------------------

def test_completed_and_failed_end_states_classify() -> None:
    assert end_outcome(_finished(1, "completed")) == "success"
    assert end_outcome(_finished(1, "failed")) == "failure"


@pytest.mark.parametrize("end_state", ["timed_out", "abandoned"])
def test_timeout_end_states_are_not_a_success(end_state: str) -> None:
    assert end_outcome(_finished(1, end_state)) is None


@pytest.mark.parametrize("end_state", [None, "", "done", "COMPLETED-ish", 7])
def test_a_malformed_or_unknown_end_state_is_never_a_success(end_state) -> None:
    record = _finished(1)
    record["end_state"] = end_state
    record["status"] = "success"  # a raw status must not rescue the line
    assert end_outcome(record) is None


def test_a_missing_end_state_is_never_a_success() -> None:
    record = {k: v for k, v in FINISH_COMPLETED.items() if k != "end_state"}
    record["status"] = "success"
    assert end_outcome(record) is None


def test_a_dispatch_finished_raw_status_is_never_read() -> None:
    # end_state is authoritative; a contradictory status field does not
    # downgrade a completed end ...
    completed = _finished(1, "completed", status="failure")
    assert end_outcome(completed) == "success"
    # ... and a raw success does not lift an end with no end_state.
    no_state = {k: v for k, v in FINISH_COMPLETED.items() if k != "end_state"}
    no_state["status"] = "success"
    assert end_status(no_state) is None
    assert end_outcome(no_state) is None


def test_legacy_status_still_classifies() -> None:
    assert end_outcome(dict(LEGACY_COMPLETE, status="success")) == "success"
    assert end_outcome(dict(LEGACY_COMPLETE, status="failed")) == "failure"
    assert end_outcome(dict(LEGACY_COMPLETE, status="unknown")) is None


def test_a_non_end_record_has_no_outcome() -> None:
    assert end_outcome(REPORT_ONLY) is None
    assert end_outcome(STARTED) is None
    assert end_outcome(None) is None


def test_end_status_projects_the_end_state() -> None:
    assert end_status(_finished(1, "completed")) == "success"
    assert end_status(_finished(1, "failed")) == "failure"
    assert end_status(_finished(1, "timed_out")) == "timeout"
    assert end_status(_finished(1, "abandoned")) == "timeout"
    assert end_status(LEGACY_COMPLETE) == "success"
    assert end_status(REPORT_ONLY) is None


# ---------------------------------------------------------------------------
# The readers: a new-type ledger and a legacy ledger give the same picture
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import build_t0_state  # noqa: E402
import queue_reconciler  # noqa: E402
import receipt_classifier  # noqa: E402
import state_rebuild_trigger  # noqa: E402
import stop_conditions  # noqa: E402
import terminal_state_reconciler  # noqa: E402
import unified_state_manager  # noqa: E402
from pr_discovery import PRDiscovery  # noqa: E402


def _stamp(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _end_record(kind: str, dispatch_id: str, *, ok: bool, minutes_ago: int,
                provider: str = "kimi", terminal: str = "T1") -> dict:
    record = {
        "project_id": PROJECT,
        "dispatch_id": dispatch_id,
        "attempt": 1,
        "provider": provider,
        "terminal": terminal,
        "timestamp": _stamp(minutes_ago),
        "title": "PR 701 finished",
    }
    if kind == "legacy":
        record["event_type"] = "task_complete"
        record["status"] = "success" if ok else "failure"
    else:
        record["event_type"] = "dispatch_finished"
        record["end_state"] = "completed" if ok else "failed"
        record["status"] = "success" if ok else "failure"
    return record


def _ledger(kind: str) -> list[dict]:
    """Two dispatches, one completed and one failed, in either vocabulary.

    The lifecycle ledger additionally carries the two non-outcome line types
    (the report arrives after the end; the start precedes it). Neither may
    change any reader's picture.
    """
    records = [
        _end_record(kind, "d-one", ok=True, minutes_ago=3),
        _end_record(kind, "d-two", ok=False, minutes_ago=2),
    ]
    if kind == "lifecycle":
        records.extend([
            {
                "event_type": "dispatch_started",
                "project_id": PROJECT,
                "dispatch_id": "d-one",
                "attempt": 1,
                "lifecycle_mode": "v3",
                "terminal": "T1",
                "timestamp": _stamp(4),
            },
            {
                "event_type": "report_ingested",
                "project_id": PROJECT,
                "dispatch_id": "d-one",
                "attempt": 1,
                "end_pending": True,
                "terminal": "T1",
                "timestamp": _stamp(1),
            },
        ])
    return records


def _write_ledger(path: Path, records: list[dict]) -> None:
    payload = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(payload, encoding="utf-8")


# -- build_t0_state._build_queues -------------------------------------------

def test_build_queues_counts_both_vocabularies_alike(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    pictures = []
    for kind in ("legacy", "lifecycle"):
        state_dir = tmp_path / kind / "state"
        state_dir.mkdir(parents=True)
        _write_ledger(state_dir / "t0_receipts.ndjson", _ledger(kind))
        queues = build_t0_state._build_queues(tmp_path / kind / "dispatches", state_dir)
        pictures.append(queues["completed_last_hour"])
    assert pictures == [2, 2]


# -- receipt_classifier.should_classify -------------------------------------

def test_receipt_classifier_classifies_both_vocabularies_alike(tmp_path: Path) -> None:
    legacy = [receipt_classifier.should_classify(r) for r in _ledger("legacy")]
    lifecycle = [receipt_classifier.should_classify(r) for r in _ledger("lifecycle")]
    assert legacy == [True, True]
    assert lifecycle == [True, True, False, False]


# -- state_rebuild_trigger --------------------------------------------------

def test_state_rebuild_treats_both_end_vocabularies_as_critical(
        tmp_path: Path, monkeypatch) -> None:
    def fires(kind: str, event_type: str) -> bool:
        (tmp_path / ".last_state_rebuild_ts").write_text(
            str(int(__import__("time").time())), encoding="utf-8")
        with mock.patch.object(state_rebuild_trigger, "_resolve_state_dir", return_value=tmp_path), \
             mock.patch("state_rebuild_trigger.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            return state_rebuild_trigger.maybe_trigger_state_rebuild(event_type=event_type)

    pictures = []
    for kind in ("legacy", "lifecycle"):
        end_event = "task_complete" if kind == "legacy" else "dispatch_finished"
        pictures.append((fires(kind, end_event), fires(kind, "report_ingested")))
    # Both end vocabularies bypass the throttle; a report line never does.
    assert pictures == [(True, False), (True, False)]


# -- stop_conditions.check_provider_exhausted -------------------------------

def _exhaustion_ledger(kind: str) -> list[dict]:
    records = []
    for index in range(3):
        record = _end_record(kind, f"d-exhausted-{index}", ok=False, minutes_ago=10 - index)
        record["failure_class"] = "auth_rejected"
        records.append(record)
    if kind == "lifecycle":
        records.append({
            "event_type": "report_ingested",
            "project_id": PROJECT,
            "dispatch_id": "d-exhausted-2",
            "attempt": 1,
            "end_pending": True,
            "provider": "kimi",
            "failure_class": "auth_rejected",
            "timestamp": _stamp(1),
        })
    return records


def test_provider_exhaustion_reads_both_end_vocabularies_alike(tmp_path: Path) -> None:
    statuses = []
    for kind in ("legacy", "lifecycle"):
        ledger_path = tmp_path / f"{kind}.ndjson"
        _write_ledger(ledger_path, _exhaustion_ledger(kind))
        result = stop_conditions.check_provider_exhausted(
            ledger_path, providers=["kimi"], threshold=3)
        statuses.append(result.status)
    assert statuses == [stop_conditions.CheckStatus.TRIGGERED] * 2


# -- terminal_state_reconciler ----------------------------------------------

def test_terminal_state_normalizes_both_end_vocabularies_alike() -> None:
    assert terminal_state_reconciler._normalize_receipt_event(
        _end_record("legacy", "d-one", ok=True, minutes_ago=3)) == "idle"
    assert terminal_state_reconciler._normalize_receipt_event(
        _end_record("lifecycle", "d-one", ok=True, minutes_ago=3)) == "idle"


def test_a_late_report_does_not_unfinish_the_terminal(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    statuses = []
    for kind in ("legacy", "lifecycle"):
        state_dir = tmp_path / kind
        state_dir.mkdir()
        _write_ledger(state_dir / "t0_receipts.ndjson", _ledger(kind))
        result = terminal_state_reconciler.reconcile_terminal_state(
            state_dir,
            now=now,
            tmux_probe=lambda *_args: {},
            process_probe=lambda: {},
        )
        statuses.append(result["terminals"]["T1"]["status"])
    assert statuses == ["idle", "idle"]


# -- pr_discovery -----------------------------------------------------------

def test_pr_discovery_marks_done_in_both_vocabularies() -> None:
    pictures = []
    for kind in ("legacy", "lifecycle"):
        discovered: dict = {}
        discovery = PRDiscovery([])
        for record in _ledger(kind):
            discovery._process_receipt_pr_refs(discovered, record)
        pictures.append(discovered[701]["receipt_done"])
    assert pictures == [True, True]


# -- unified_state_manager --------------------------------------------------

def test_unified_state_counts_both_vocabularies_alike(tmp_path: Path, monkeypatch) -> None:
    pictures = []
    for kind in ("legacy", "lifecycle"):
        manager = unified_state_manager.UnifiedStateManagerV2.__new__(
            unified_state_manager.UnifiedStateManagerV2)
        monkeypatch.setattr(manager, "get_recent_receipts", lambda hours=24, k=kind: _ledger(k))
        monkeypatch.setattr(manager, "get_recent_dispatches", lambda hours=24: [])
        monkeypatch.setattr(unified_state_manager, "UNIFIED_STATE_PATH",
                            str(tmp_path / f"{kind}-unified.ndjson"))
        agg = manager.write_unified_state()
        pictures.append(agg["completed_today"])
    assert pictures == [2, 2]


# -- queue_reconciler -------------------------------------------------------

def test_queue_reconciler_confirms_both_vocabularies_alike(tmp_path: Path) -> None:
    pictures = []
    for kind in ("legacy", "lifecycle"):
        ledger_path = tmp_path / f"{kind}-queue.ndjson"
        _write_ledger(ledger_path, _ledger(kind))
        pictures.append(queue_reconciler.load_receipt_dispatch_ids(ledger_path))
    assert pictures == [{"d-one", "d-two"}, {"d-one", "d-two"}]
