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
# "subprocess_completion", dispatch_govern.ensure_receipt — status "done" for
# an authored non-failing report, "failed" otherwise), and the legacy
# report_to_receipt_converter / codex-variant emit sites ("failure" is a
# distinct literal from "failed", not a typo — different call sites use each).
#
# "timeout" is deliberately NOT a failure status here: a completion-family
# receipt carrying literal status="timeout" is a separate, pre-existing
# carve-out (kept as-is by this consolidation, not part of the OI-1148 fix).
# ---------------------------------------------------------------------------
FAILURE_STATUSES = frozenset({"failed", "failure", "error", "blocked", "contract_invalid"})
SUCCESS_STATUSES = frozenset({"success", "completed", "complete", "ok", "done", ""})

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


__all__ = [
    "FAILURE_STATUSES",
    "SUCCESS_STATUSES",
    "COMPLETION_EVENT_TYPES",
    "classify_event_outcome",
    "is_governed_failure",
]
