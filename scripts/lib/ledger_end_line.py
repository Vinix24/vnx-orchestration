"""ADR-045 §2 (audit-spoor-kern K-2a): the one end line of a dispatch attempt.

A dispatch ends exactly once per ``(project_id, dispatch_id, attempt)``. Two
writers exist in the ledger's history: the legacy completion family
(``task_complete`` and its siblings) and, from K-1 on, ``dispatch_finished``
carrying a closed ``end_state``. A reader must see one answer whether the
ledger holds the new type, the legacy type, or both for the same attempt.

``end_line`` is that one answer. It never treats a start line or a report line
as an end: ``dispatch_started`` happens before the work and ``report_ingested``
only says the report arrived (ADR-045 §2, plan revisie 2 point 3). The outcome
of an end line is read through
``event_outcome_semantics.classify_event_outcome``; for a ``dispatch_finished``
the status is projected from its ``end_state`` with
``event_outcome_semantics.end_state_status``, never taken from a raw ``status``
field on the line. An absent, ``None``, malformed or unknown ``end_state``
therefore yields no outcome at all — never a success (fail-closed on evidence).

Everything here is deterministic: same records, same answer.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from event_outcome_semantics import (
    COMPLETION_EVENT_TYPES,
    classify_event_outcome,
    end_state_status,
)

DISPATCH_FINISHED_EVENT_TYPE = "dispatch_finished"

__all__ = [
    "DISPATCH_FINISHED_EVENT_TYPE",
    "end_line",
    "end_outcome",
    "end_status",
]


def _event_type(record: Dict[str, Any]) -> str:
    return str(record.get("event_type") or record.get("event") or "").strip().lower()


def _project_of(record: Dict[str, Any]) -> str:
    return str(record.get("project_id") or "").strip()


def _attempt_of(record: Dict[str, Any]) -> Optional[int]:
    """The attempt a record belongs to; absent reads as attempt 1.

    A present but malformed value (a bool, a non-integer, a zero, a negative)
    is no attempt at all: it is not quietly folded into attempt 1, because that
    would let a malformed line stand in for a real attempt's end.
    """
    value = record.get("attempt")
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _same_dispatch(record: Dict[str, Any], project_id: str, dispatch_id: str) -> bool:
    if str(record.get("dispatch_id") or "").strip() != dispatch_id:
        return False
    # A line without project_id is legacy history and stays readable; a line
    # that names a different project never counts for this one (ADR-007).
    record_project = _project_of(record)
    return not (project_id and record_project and record_project != project_id)


def end_line(
    records: Iterable[Dict[str, Any]],
    project_id: Optional[str],
    dispatch_id: Optional[str],
    attempt: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """The one end record of ``dispatch_id``'s attempt, or ``None``.

    ``dispatch_finished`` wins over a legacy completion record for the same
    attempt; when several records of the winning type exist, the last appended
    one is returned (the ledger is append-only). ``attempt=None`` selects the
    highest attempt present for the dispatch — computed over every record of
    that dispatch, not only the ends, so a newer attempt that has a start (or a
    report) but no end answers ``None`` instead of falling back to an older
    attempt's end. A ``report_ingested`` without an end line is exactly that:
    "report in, dispatch not ended", never an outcome.
    """
    did = str(dispatch_id or "").strip()
    if not did:
        return None
    pid = str(project_id or "").strip()

    matched = [
        record for record in records
        if isinstance(record, dict) and _same_dispatch(record, pid, did)
    ]
    if not matched:
        return None

    if attempt is None:
        attempts = [value for value in (_attempt_of(r) for r in matched) if value is not None]
        if not attempts:
            return None
        target = max(attempts)
    else:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            return None
        target = attempt

    ends = [
        record for record in matched
        if _attempt_of(record) == target and _event_type(record) in COMPLETION_EVENT_TYPES
    ]
    if not ends:
        return None
    finished = [r for r in ends if _event_type(r) == DISPATCH_FINISHED_EVENT_TYPE]
    return (finished or ends)[-1]


def end_status(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """The status an end record reports, projected per its own type.

    ``dispatch_finished`` projects from ``end_state`` (never its raw status
    field); a legacy completion record keeps its own ``status`` literal.
    ``None`` for a non-end record and for an absent/unknown ``end_state``.
    """
    if not isinstance(record, dict):
        return None
    event_type = _event_type(record)
    if event_type == DISPATCH_FINISHED_EVENT_TYPE:
        end_state = record.get("end_state")
        if not isinstance(end_state, str):
            return None  # absent, None or a non-string is malformed, never a status
        return end_state_status(end_state)
    if event_type in COMPLETION_EVENT_TYPES:
        return str(record.get("status") or "").strip().lower() or None
    return None


def end_outcome(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """``"success"``, ``"failure"`` or ``None`` for the end record's outcome.

    ``None`` is the fail-closed answer: a missing, malformed or unknown
    ``end_state`` on a ``dispatch_finished`` line, an unrecognized status on a
    legacy line, and a non-end record all carry no positive evidence.
    """
    if not isinstance(record, dict):
        return None
    event_type = _event_type(record)
    if event_type not in COMPLETION_EVENT_TYPES:
        return None
    return classify_event_outcome(event_type, end_status(record))
