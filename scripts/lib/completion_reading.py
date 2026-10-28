"""Reading a dispatch's completion off one receipt line (ADR-045 §2, K-2c).

Two shapes carry a completion, and the analysis readers read both:

  legacy -- ``event_type`` in ``COMPLETION_EVENT_TYPES``: the line's own
            ``status`` field carries the outcome.
  v3     -- ``dispatch_finished`` (K-1/ADR-045): ``end_state`` is the ledger
            fact and ``status`` only its projection. The outcome is read from
            ``end_state``, never from the status field: an ``end_state`` that is
            absent, ``None`` or outside the closed set carries no outcome at
            all -- it is never a success.

``dispatch_started`` and ``report_ingested`` are lifecycle lines: never a
completion, never a success, never a cost line of their own, so every helper
here answers ``False``/``None`` for them. Line-level counterpart of K-2a's
``ledger_end_line``: these readers walk the ledger themselves.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, FrozenSet, Mapping, Optional

# Importable as ``completion_reading`` (scripts/lib on sys.path) and as
# ``scripts.lib.completion_reading`` (repo-root package form): event_outcome_semantics
# is imported by name, so this directory must be on the path in both shapes.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from event_outcome_semantics import (  # noqa: E402
    COMPLETION_EVENT_TYPES,
    classify_event_outcome,
    end_state_status,
)

# The v3 end line of one attempt: read as another literal ("success") it would
# count a failed end line as a success, the defect ADR-045 §4 closes.
DISPATCH_FINISHED_EVENT_TYPE = "dispatch_finished"

# ADR-045 §2: the two lifecycle lines that are never an outcome. Kept identical
# to ``receipt_outcome.NON_OUTCOME_EVENT_TYPES`` (K-1) by a test.
NON_OUTCOME_EVENT_TYPES = frozenset({"dispatch_started", "report_ingested"})

# What a reader filters the ledger by: the canonical set, never a local copy.
COMPLETION_TYPES: FrozenSet[str] = COMPLETION_EVENT_TYPES


def event_type_of(record: Mapping[str, Any]) -> str:
    """The event type of a ledger line, the v2 ``event`` spelling included."""
    return str(record.get("event_type") or record.get("event") or "").strip().lower()


def is_v3_end_line(record: Mapping[str, Any]) -> bool:
    """True for a ``dispatch_finished`` line — the only line whose status is a
    projection rather than a value the writer chose for a reader."""
    return event_type_of(record) == DISPATCH_FINISHED_EVENT_TYPE


def is_completion(record: Mapping[str, Any]) -> bool:
    """True for a line that reports a dispatch's completion, in either shape."""
    return event_type_of(record) in COMPLETION_EVENT_TYPES


def is_non_outcome_lifecycle(record: Mapping[str, Any]) -> bool:
    """True for a start line or a report line — never an outcome (ADR-045 §2)."""
    return event_type_of(record) in NON_OUTCOME_EVENT_TYPES


def completion_status(record: Mapping[str, Any]) -> Optional[str]:
    """The status a completion line's outcome is read under, or None.

    A ``dispatch_finished`` line answers with the projection of its ``end_state``
    (ADR-045 §4) -- the ledger fact, never the projected status field. Every
    other completion type answers with its own ``status``, as it always has.
    """
    if is_v3_end_line(record):
        end_state = record.get("end_state")
        # A non-string end_state (a number, a list) is malformed: it has no
        # projection and is no evidence, so it answers None rather than raising
        # inside whichever reader met the line.
        return end_state_status(end_state) if isinstance(end_state, str) else None
    status = record.get("status")
    return None if status is None else str(status)


def completion_outcome(record: Mapping[str, Any]) -> Optional[str]:
    """``"success"``/``"failure"`` for a completion line, None when it has none.

    ``classify_event_outcome`` decides over the status ``completion_status``
    reads: a v3 end line by its ``end_state``, a legacy completion by its
    status literal. A non-completion line has no outcome.
    """
    event_type = event_type_of(record)
    if event_type not in COMPLETION_EVENT_TYPES:
        return None
    return classify_event_outcome(event_type, completion_status(record))


def completion_matches_outcome(
    record: Mapping[str, Any],
    outcome: str,
    legacy_statuses: frozenset[str] | set[str],
) -> bool:
    """True when a completion line resolves to ``outcome``.

    The legacy branch is the literal comparison a reader has always made
    (``legacy_statuses``), so legacy lines read the same numbers as before,
    ``blocked`` included. A v3 end line is judged on its ``end_state``.
    """
    if not is_completion(record):
        return False
    if is_v3_end_line(record):
        return completion_outcome(record) == outcome
    return str(record.get("status") or "").strip().lower() in legacy_statuses


__all__ = (
    "COMPLETION_TYPES", "DISPATCH_FINISHED_EVENT_TYPE", "NON_OUTCOME_EVENT_TYPES",
    "completion_matches_outcome", "completion_outcome", "completion_status",
    "event_type_of", "is_completion", "is_non_outcome_lifecycle", "is_v3_end_line",
)
