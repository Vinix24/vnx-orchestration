"""ADR-045 K-7a (OI-2524): a door refusal is neither an end nor a witness.

A door refusal is booked as a ``task_complete`` with ``status: blocked`` and
``delivery_state: delivery_refused`` and no ``attempt``. Two readers used to
misread it: ``end_line`` returned it as attempt 1's end while attempt 1 was
still running, and ``dedupe_completions`` kept it over the later real success.

Dispatch-ID: dlv-e2a5aa50e9c4
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from completion_reading import (  # noqa: E402
    count_completions,
    dedupe_completions,
    is_refusal,
)
from ledger_end_line import end_line  # noqa: E402

PROJECT = "vnx-dev"
DISPATCH = "20261103-120000-k7a"

# The K-1 start line, the shape the ledger holds for a live attempt.
STARTED = {
    "event_type": "dispatch_started",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": 1,
    "lifecycle_mode": "v3",
}

# The door refusal: blocked, delivery_refused, and no ``attempt`` at all.
REFUSAL = {
    "event_type": "task_complete",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "status": "blocked",
    "delivery_state": "delivery_refused",
}

SUCCESS = {
    "event_type": "task_complete",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "status": "success",
}

LANE_LINE = {
    "event_type": "subprocess_completion",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "status": "done",
}


def _finished(attempt: int = 1, end_state: str = "completed") -> dict:
    return {
        "event_type": "dispatch_finished",
        "project_id": PROJECT,
        "dispatch_id": DISPATCH,
        "attempt": attempt,
        "end_state": end_state,
        "status": "success" if end_state == "completed" else "failure",
    }


# ---------------------------------------------------------------------------
# is_refusal: exactly the one positive value
# ---------------------------------------------------------------------------

def test_is_refusal_is_true_only_for_the_exact_delivery_refused_value():
    assert is_refusal(REFUSAL) is True
    for other in ("delivery_refused ", "Delivery_Refused", "refused", "", None):
        assert is_refusal(dict(REFUSAL, delivery_state=other)) is False
    assert is_refusal({"event_type": "task_complete", "status": "blocked"}) is False


# ---------------------------------------------------------------------------
# end_line: a refusal never ends an attempt that has a start line
# ---------------------------------------------------------------------------

def test_a_refusal_is_never_the_end_of_a_started_attempt():
    records = [STARTED, REFUSAL]
    assert end_line(records, PROJECT, DISPATCH) is None
    assert end_line(records, PROJECT, DISPATCH, attempt=1) is None


def test_the_refusal_loses_whatever_its_position_in_the_ledger():
    assert end_line([STARTED, REFUSAL], PROJECT, DISPATCH, attempt=1) is None
    assert end_line([REFUSAL, STARTED], PROJECT, DISPATCH, attempt=1) is None


def test_the_real_finish_after_a_refusal_is_that_attempts_end():
    finished = _finished(1)
    records = [STARTED, REFUSAL, finished]
    assert end_line(records, PROJECT, DISPATCH) is finished
    assert end_line(records, PROJECT, DISPATCH, attempt=1) is finished


def test_a_refusal_for_a_started_attempt_does_not_end_another_attempt():
    # The refusal names no attempt, so it belongs to attempt 1; a start line
    # for attempt 1 is enough to disqualify it even when attempt 2 is asked for.
    assert end_line([STARTED, REFUSAL], PROJECT, DISPATCH, attempt=2) is None


def test_a_legacy_refusal_without_a_start_line_is_still_the_end():
    assert end_line([REFUSAL], PROJECT, DISPATCH) is REFUSAL
    assert end_line([REFUSAL], PROJECT, DISPATCH, attempt=1) is REFUSAL


def test_a_refusal_next_to_an_unrelated_start_line_stays_an_end():
    other_start = dict(STARTED, dispatch_id="some-other-dispatch")
    assert end_line([other_start, REFUSAL], PROJECT, DISPATCH) is REFUSAL


# ---------------------------------------------------------------------------
# dedupe_completions: a refusal ranks below every non-refusal completion
# ---------------------------------------------------------------------------

def test_a_success_after_a_refusal_is_the_witness():
    assert dedupe_completions([REFUSAL, SUCCESS]) == [SUCCESS]
    assert count_completions([REFUSAL, SUCCESS]) == 1


def test_a_success_before_a_refusal_is_the_witness():
    assert dedupe_completions([SUCCESS, REFUSAL]) == [SUCCESS]
    assert count_completions([SUCCESS, REFUSAL]) == 1


def test_two_refusals_of_one_attempt_collapse_to_the_first():
    second = dict(REFUSAL, terminal="T2")
    assert dedupe_completions([REFUSAL, second]) == [REFUSAL]
    assert count_completions([REFUSAL, second]) == 1


def test_the_lane_line_outranks_a_refusal():
    assert dedupe_completions([REFUSAL, LANE_LINE]) == [LANE_LINE]
    assert dedupe_completions([LANE_LINE, REFUSAL]) == [LANE_LINE]
    assert count_completions([REFUSAL, LANE_LINE]) == 1


def test_the_v3_end_line_still_outranks_a_legacy_success():
    finished = _finished(1)
    assert dedupe_completions([SUCCESS, finished]) == [finished]
