"""The confidence hook books the outcome of a v3 dispatch from its governance record.

K-fix-3 (#2436, ADR-054) made the envelope write that record as ``dispatch_governed``
instead of ``task_complete``. The hook returned early on it (OI-2597). The end line
(``dispatch_finished``) carries no ``failure_class`` and no terminal, so it stays
without a signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import append_receipt  # registers the facade
from append_receipt_internals.payload import _WORK_FAILURE_CLASSES
from failure_classification import FAILURE_CLASSES
from receipt_schema import DISPATCH_GOVERNED_EVENT_TYPE
from test_confidence_loop_restart import _bootstrap_like_production, _events, _seed_pattern


@pytest.fixture()
def state(tmp_path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _bootstrap_like_production(state_dir)
    return state_dir


def _record(dispatch_id: str, status, event_type: str = DISPATCH_GOVERNED_EVENT_TYPE, **extra) -> dict:
    return {
        "event_type": event_type,
        "status": status,
        "dispatch_id": dispatch_id,
        "terminal_id": "T1",
        **extra,
    }


def _infra_class() -> str:
    candidates = sorted(set(FAILURE_CLASSES) - set(_WORK_FAILURE_CLASSES))
    assert candidates
    return candidates[0]


def test_governed_success_updates_confidence_once(state):
    _seed_pattern(state, "gov-ok")
    append_receipt._update_confidence_from_receipt(_record("gov-ok", "success"), state_dir=state)
    events = _events(state, "gov-ok")
    assert len(events) == 1
    assert events[0][0] == "success"
    assert events[0][3] == "T1"


def test_governed_work_failure_records_a_failure(state):
    _seed_pattern(state, "gov-work")
    record = _record("gov-work", "failed", failure_class="completion_without_execution")
    append_receipt._update_confidence_from_receipt(record, state_dir=state)
    assert [e[0] for e in _events(state, "gov-work")] == ["failure"]


def test_governed_infrastructure_failure_records_nothing(state):
    _seed_pattern(state, "gov-infra")
    record = _record("gov-infra", "failed", failure_class=_infra_class())
    append_receipt._update_confidence_from_receipt(record, state_dir=state)
    assert _events(state, "gov-infra") == []


def test_dispatch_finished_line_records_nothing(state):
    _seed_pattern(state, "gov-end")
    line = _record("gov-end", "success", event_type="dispatch_finished", end_state="done")
    append_receipt._update_confidence_from_receipt(line, state_dir=state)
    assert _events(state, "gov-end") == []


def test_same_governed_record_twice_records_once(state):
    _seed_pattern(state, "gov-twice")
    record = _record("gov-twice", "success")
    append_receipt._update_confidence_from_receipt(record, state_dir=state)
    after_first = _events(state, "gov-twice")
    append_receipt._update_confidence_from_receipt(record, state_dir=state)
    assert _events(state, "gov-twice") == after_first
    assert len(after_first) <= 1


@pytest.mark.parametrize("status", [None, "", "weird-unknown"])
def test_governed_record_without_a_known_status_records_nothing(state, status):
    _seed_pattern(state, "gov-bad")
    append_receipt._update_confidence_from_receipt(_record("gov-bad", status), state_dir=state)
    assert _events(state, "gov-bad") == []
