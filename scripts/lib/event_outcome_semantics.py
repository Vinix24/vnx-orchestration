"""Canonical event_type + status -> governed-outcome semantics (OI-1148).

The receipt ledger (``t0_receipts.ndjson``) carries several distinct
``event_type`` values, and the ``status`` field's vocabulary — and its
meaning — differs per ``event_type``. Before this module, that per-event_type
semantic was re-decided independently in at least three places
(``append_receipt_internals/register_emit.py``,
``append_receipt_internals/payload.py``, ``outcome_signals.py``), each with
its own hand-copied ``FAILURE_STATUSES``/``SUCCESS_STATUSES`` set and its own
guess at which event_types even carry outcome semantics. The sets drifted
(e.g. one had ``"done"`` in its success set, another didn't — silently
dropping every ``subprocess_completion`` success receipt from the register),
and a status-blind branch (``task_timeout`` always == failure) ignored that
``status == "no_confirmation"`` is a *pending, awaiting-confirmation* state,
not a terminal failure (see ``receipt_processor/rp_state.sh`` and
``rp_pattern.sh``, and ``cqs_calculator.STATUS_MAP`` which maps
``no_confirmation``/``timeout`` to their own excluded ``"timeout"`` category,
never ``"failure"``).

This module is the single source of truth: every reader that needs to know
whether a given ``(event_type, status)`` pair represents a governed failure,
a governed success, or neither, calls ``classify_event_outcome()`` here.

Scope note (OI-1148): this module governs the READ-side classification only.
It does not correct historically-written receipts — a receipt written under
the old (pre-fix) synthesized-status logic keeps whatever status it was
written with; only how that status is *interpreted* changes here.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Status vocab for "completion" events (COMPLETION_EVENT_TYPES below).
#
# The union of every status literal seen across the write paths that emit
# these event_types: ReceiptV2 (event_type forced to "task_complete",
# governance_emit.py), SynthesizedLaneReceipt (event_type
# "subprocess_completion", dispatch_govern.ensure_receipt, status "done" for
# an authored non-failing report, "failed" otherwise), and the legacy
# report_to_receipt_converter / codex-variant emit sites ("failure" is a
# distinct literal from "failed", not a typo — different call sites use each).
#
# _STATUS_VOCABULARY is the single generated source. FAILURE_STATUSES,
# SUCCESS_STATUSES, and IGNORABLE_STATUSES below are DERIVED from it (never
# hand-copied), and the write-side converter resolves a report's declared
# status against it via resolve_status_category(). A new status literal is
# added here exactly once, with its category; there is no second list to keep
# in sync, and scripts/check_status_vocab_drift.py fails CI on any module that
# hard-codes its own copy.
#
# The five historical differences vs the converter's old hand-copied sets are
# resolved here per value:
#   - "ok"               -> success (a report declaring ok is a success claim)
#   - "" (empty)         -> REMOVED from success: absence is not a success
#                           claim (resolve_status_category returns "no_signal")
#   - "blocked"          -> failure (a blocked completion is a governed failure)
#   - "contract_invalid" -> failure (already canonical; the converter missed it)
#   - "heartbeat_killed" -> failure (the converter's defensive literal; a
#                           heartbeat-kill report is failure-shaped by design)
#
# "timeout" is deliberately NOT a failure status here: a completion-family
# receipt carrying literal status="timeout" is a separate, pre-existing
# carve-out (kept as-is by this consolidation, not part of the OI-1148 fix).
# It is still listed as ignorable so the write-side refuses nothing the
# read-side already tolerated.
# ---------------------------------------------------------------------------
_STATUS_VOCABULARY = {
    # governed failure literals
    "failed": "failure",
    "failure": "failure",
    "error": "failure",
    "blocked": "failure",
    "contract_invalid": "failure",
    "heartbeat_killed": "failure",
    # governed success literals
    "success": "success",
    "completed": "success",
    "complete": "success",
    "ok": "success",
    "done": "success",
    # explicitly ignorable literals (pending/neutral, no outcome signal)
    "timeout": "ignorable",
    "no_confirmation": "ignorable",
    "unknown": "ignorable",
    "in_progress": "ignorable",
    "guard_error": "ignorable",
    "no_ready_pr": "ignorable",
    "not_configured": "ignorable",
    "not_executable": "ignorable",
    "requested": "ignorable",
}

FAILURE_STATUSES = frozenset(
    status for status, category in _STATUS_VOCABULARY.items() if category == "failure"
)
SUCCESS_STATUSES = frozenset(
    status for status, category in _STATUS_VOCABULARY.items() if category == "success"
)
IGNORABLE_STATUSES = frozenset(
    status for status, category in _STATUS_VOCABULARY.items() if category == "ignorable"
)

# event_types whose own status field uses the FAILURE_STATUSES/SUCCESS_STATUSES
# vocab above to signal outcome.
COMPLETION_EVENT_TYPES = frozenset({"task_complete", "task_completed", "subprocess_completion"})

# event_types that are unconditionally a governed failure, regardless of
# whatever the status field carries.
_ALWAYS_FAILURE_EVENT_TYPES = frozenset({"task_failed", "report_contract_invalid"})

# task_timeout is a governed failure UNLESS status is one of these — a
# "pending, awaiting confirmation" signal, not a terminal outcome. See
# receipt_processor/rp_state.sh::update_receipt_shadow_state (maps this
# combination to a "blocked" shadow state with a lease, not a failure) and
# rp_pattern.sh::update_track_progress (same combination -> track "blocked",
# never routed through the idle/failure branches).
_TIMEOUT_PENDING_STATUSES = frozenset({"no_confirmation"})


def classify_event_outcome(event_type: Optional[str], status: Optional[str]) -> Optional[str]:
    """Classify a receipt's governed outcome from its (event_type, status).

    Returns:
        "failure" -- this (event_type, status) pair is a governed failure.
        "success" -- this pair is a governed success / non-failure completion.
        None      -- this pair carries no outcome signal at all (either the
                     event_type isn't an outcome-bearing event, or the status
                     literal isn't in the recognized vocabulary for it).
    """
    et = (event_type or "").strip().lower()
    st = (status or "").strip().lower()

    if et in _ALWAYS_FAILURE_EVENT_TYPES:
        return "failure"

    if et == "task_timeout":
        return None if st in _TIMEOUT_PENDING_STATUSES else "failure"

    if et in COMPLETION_EVENT_TYPES:
        if st in FAILURE_STATUSES:
            return "failure"
        if st in SUCCESS_STATUSES:
            return "success"
        return None

    return None


def is_governed_failure(event_type: Optional[str], status: Optional[str]) -> bool:
    """True only when classify_event_outcome resolves to "failure"."""
    return classify_event_outcome(event_type, status) == "failure"


class UnknownStatusError(ValueError):
    """A report declared a status literal outside the canonical vocabulary.

    Raised by resolve_status_category() so the write-side converter refuses a
    report instead of silently treating an unknown literal as a non-terminal
    status (which would let a typo or a new, unvetted literal slip through as
    a quiet success/no-signal receipt).
    """


def resolve_status_category(status: Optional[str]) -> str:
    """Resolve a normalized status literal to its canonical category (strict).

    The write-side (report_to_receipt_converter) uses this to refuse a report
    whose declared status is not in the canonical vocabulary. The read-side
    (classify_event_outcome) stays tolerant by design; only the write path is
    fail-closed, because the write path is where a new literal enters the
    ledger.

    Returns one of:
      "failure"   -- the status is a governed failure literal.
      "success"   -- the status is a governed success literal.
      "ignorable" -- the status is in the vocabulary but carries no outcome
                     signal (pending/neutral literals the ledger tolerates).
      "no_signal" -- the status is empty/absent (NOT a success claim).

    Raises UnknownStatusError for any other literal.
    """
    st = (status or "").strip().lower()
    if not st:
        return "no_signal"
    category = _STATUS_VOCABULARY.get(st)
    if category is None:
        raise UnknownStatusError(f"unknown status literal: {status!r}")
    return category


__all__ = [
    "FAILURE_STATUSES",
    "SUCCESS_STATUSES",
    "IGNORABLE_STATUSES",
    "COMPLETION_EVENT_TYPES",
    "UnknownStatusError",
    "classify_event_outcome",
    "is_governed_failure",
    "resolve_status_category",
]
