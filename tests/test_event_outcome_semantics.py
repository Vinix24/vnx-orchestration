#!/usr/bin/env python3
"""Tests for scripts/lib/event_outcome_semantics.py (OI-1148).

One test per (event_type, status) combination that the pre-fix code got
wrong, plus the combinations that must stay unchanged. See the three call
sites this module now feeds:
  - scripts/lib/append_receipt_internals/register_emit.py::_emit_dispatch_register
  - scripts/lib/append_receipt_internals/payload.py::_update_confidence_from_receipt
  - scripts/lib/outcome_signals.py::extract_from_receipts
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from event_outcome_semantics import (  # noqa: E402
    COMPLETION_EVENT_TYPES,
    FAILURE_STATUSES,
    IGNORABLE_STATUSES,
    SUCCESS_STATUSES,
    UnknownStatusError,
    classify_event_outcome,
    is_governed_failure,
    resolve_status_category,
)


# ---------------------------------------------------------------------------
# subprocess_completion — the tmux-interactive lane's default completion
# event. status="done" is its REAL success literal (see receipt_schema.py's
# SynthesizedLaneReceipt and dispatch_govern.py's status derivation), never
# "success". Pre-fix register_emit.py's SUCCESS_STATUSES omitted "done"
# entirely, so this combination fell through to the unclassified branch —
# neither dispatch_completed nor dispatch_failed — instead of "success".
# ---------------------------------------------------------------------------

def test_subprocess_completion_done_is_success_not_unclassified():
    assert classify_event_outcome("subprocess_completion", "done") == "success"


def test_subprocess_completion_success_literal_is_success():
    assert classify_event_outcome("subprocess_completion", "success") == "success"


def test_subprocess_completion_failed_is_failure():
    assert classify_event_outcome("subprocess_completion", "failed") == "failure"


def test_subprocess_completion_unknown_status_is_unclassified():
    assert classify_event_outcome("subprocess_completion", "pending") is None


# ---------------------------------------------------------------------------
# task_complete — status vocabulary includes "failure" (a distinct literal
# from "failed", per receipt_verdict.py's own comment) and "done" (in
# addition to "success"). Pre-fix outcome_signals.py only matched the exact
# literals "failed"/"success", so "failure" and "done" never produced a
# signal at all — on the production ledger, "failure" is the ONLY literal
# task_complete failures actually use; literal "failed" never occurs on
# task_complete receipts there.
# ---------------------------------------------------------------------------

def test_task_complete_failure_literal_is_failure():
    assert classify_event_outcome("task_complete", "failure") == "failure"


def test_task_complete_failed_literal_is_failure():
    assert classify_event_outcome("task_complete", "failed") == "failure"


def test_task_complete_done_is_success_not_unclassified():
    assert classify_event_outcome("task_complete", "done") == "success"


def test_task_complete_success_literal_is_success():
    assert classify_event_outcome("task_complete", "success") == "success"


def test_task_complete_blocked_is_failure():
    assert classify_event_outcome("task_complete", "blocked") == "failure"


def test_task_complete_contract_invalid_is_failure():
    assert classify_event_outcome("task_complete", "contract_invalid") == "failure"


def test_task_complete_unknown_status_is_unclassified():
    assert classify_event_outcome("task_complete", "unknown") is None


def test_task_complete_timeout_literal_status_stays_unclassified():
    """A literal status="timeout" on a task_complete receipt is a pre-existing,
    deliberate carve-out (kept as-is by this consolidation): "timeout" is not
    in FAILURE_STATUSES for completion events."""
    assert classify_event_outcome("task_complete", "timeout") is None


# ---------------------------------------------------------------------------
# task_timeout — status="no_confirmation" is a pending/awaiting-confirmation
# state (receipt_processor/rp_state.sh maps it to a "blocked" shadow state
# with a lease, never a failure; rp_pattern.sh routes it to a distinct
# "blocked" track branch, not the idle/failure paths). Pre-fix
# register_emit.py mapped EVERY task_timeout to dispatch_failed regardless of
# status, over-counting these pending dispatches as governed failures.
# ---------------------------------------------------------------------------

def test_task_timeout_no_confirmation_is_not_a_failure():
    assert classify_event_outcome("task_timeout", "no_confirmation") is None


def test_task_timeout_real_timeout_is_still_failure():
    assert classify_event_outcome("task_timeout", "timeout") == "failure"


def test_task_timeout_empty_status_is_still_failure():
    """No status at all on a task_timeout receipt is a fail-closed failure —
    only the specific no_confirmation literal is excluded."""
    assert classify_event_outcome("task_timeout", "") == "failure"


def test_task_timeout_is_governed_failure_helper():
    assert is_governed_failure("task_timeout", "timeout") is True
    assert is_governed_failure("task_timeout", "no_confirmation") is False


# ---------------------------------------------------------------------------
# Unconditional-failure event_types — status is irrelevant.
# ---------------------------------------------------------------------------

def test_task_failed_is_always_failure_regardless_of_status():
    assert classify_event_outcome("task_failed", "") == "failure"
    assert classify_event_outcome("task_failed", "anything") == "failure"


def test_report_contract_invalid_is_always_failure():
    assert classify_event_outcome("report_contract_invalid", "contract_invalid") == "failure"
    assert classify_event_outcome("report_contract_invalid", "") == "failure"


# ---------------------------------------------------------------------------
# Non-outcome event_types — no signal either way.
# ---------------------------------------------------------------------------

def test_non_outcome_event_type_is_unclassified():
    assert classify_event_outcome("dispatch_claimed", "info") is None
    assert classify_event_outcome("review_gate_request", "") is None
    assert classify_event_outcome("task_started", "") is None


def test_missing_event_type_and_status_is_unclassified():
    assert classify_event_outcome(None, None) is None
    assert classify_event_outcome("", "") is None


# ---------------------------------------------------------------------------
# Case/whitespace insensitivity — receipts store lowercase, but callers pass
# raw fields through str(...).lower() at different sites; the canonical
# function must not require the caller to have already normalized.
# ---------------------------------------------------------------------------

def test_classify_is_case_insensitive():
    assert classify_event_outcome("SUBPROCESS_COMPLETION", "DONE") == "success"
    assert classify_event_outcome("Task_Timeout", "No_Confirmation") is None


# ---------------------------------------------------------------------------
# Vocabulary sanity — the sets consumers import directly.
# ---------------------------------------------------------------------------

def test_completion_event_types_contains_all_three_forms():
    assert COMPLETION_EVENT_TYPES == frozenset(
        {"task_complete", "task_completed", "subprocess_completion"}
    )


def test_done_is_in_success_statuses():
    assert "done" in SUCCESS_STATUSES


def test_failure_and_failed_are_distinct_entries_in_failure_statuses():
    assert "failed" in FAILURE_STATUSES
    assert "failure" in FAILURE_STATUSES


# ---------------------------------------------------------------------------
# Single generated vocabulary — the five historical differences, resolved
# ---------------------------------------------------------------------------

def test_heartbeat_killed_is_a_failure_literal():
    assert "heartbeat_killed" in FAILURE_STATUSES
    assert classify_event_outcome("task_complete", "heartbeat_killed") == "failure"


def test_empty_status_is_no_longer_success():
    assert "" not in SUCCESS_STATUSES
    assert classify_event_outcome("task_complete", "") is None


def test_ignorable_statuses_carry_no_outcome_signal():
    for status in IGNORABLE_STATUSES:
        assert classify_event_outcome("task_complete", status) is None, status


# ---------------------------------------------------------------------------
# resolve_status_category — the strict write-side resolver
# ---------------------------------------------------------------------------

def test_resolve_status_category_maps_success_literals():
    for status in SUCCESS_STATUSES:
        assert resolve_status_category(status) == "success", status


def test_resolve_status_category_maps_failure_literals():
    for status in FAILURE_STATUSES:
        assert resolve_status_category(status) == "failure", status


def test_resolve_status_category_maps_ignorable_literals():
    for status in IGNORABLE_STATUSES:
        assert resolve_status_category(status) == "ignorable", status


def test_resolve_status_category_empty_is_no_signal():
    assert resolve_status_category("") == "no_signal"
    assert resolve_status_category(None) == "no_signal"
    assert resolve_status_category("   ") == "no_signal"


def test_resolve_status_category_unknown_raises():
    with pytest.raises(UnknownStatusError):
        resolve_status_category("bogus")
    with pytest.raises(UnknownStatusError):
        resolve_status_category("successful")  # near-miss, not a literal


def test_resolve_status_category_is_case_insensitive():
    assert resolve_status_category("FAILED") == "failure"
    assert resolve_status_category(" Done ") == "success"


def test_vocabulary_sets_are_disjoint_and_complete():
    assert not (FAILURE_STATUSES & SUCCESS_STATUSES)
    assert not (FAILURE_STATUSES & IGNORABLE_STATUSES)
    assert not (SUCCESS_STATUSES & IGNORABLE_STATUSES)
    # every vocabulary key lands in exactly one derived set
    from event_outcome_semantics import _STATUS_VOCABULARY
    assert set(_STATUS_VOCABULARY) == (
        FAILURE_STATUSES | SUCCESS_STATUSES | IGNORABLE_STATUSES
    )
