"""ADR-045 §7 (audit-spoor-kern K-7): the verdict of one dispatch attempt.

``attempt_verdict`` is the one reader the active readers ask whether a dispatch
is done: ``legacy`` (no ``v3`` start line — the caller keeps its old path),
``ended`` (the highest attempt has its own end line), or a ``v3`` start without
an end that is ``live``, of ``unknown`` liveness, or ``dead_no_end``. The end
line is selected on the exact key ``dispatch_lifecycle.v3_in_flight`` ends on
and the liveness is that module's, so the reader and the sweep agree (ADR-045
§2); an unreadable ledger raises ``OSError``, never "done".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dispatch_lifecycle import (LIVENESS_DEAD, LIVENESS_LIVE, LIVENESS_UNKNOWN,
                                InFlightAttempt, liveness)
from ledger_end_line import DISPATCH_FINISHED_EVENT_TYPE, end_status
from receipt_schema import DISPATCH_STARTED_EVENT_TYPE

LEDGER_FILENAME = "t0_receipts.ndjson"
MODE_V3 = "v3"
KIND_LEGACY, KIND_ENDED, KIND_LIVE, KIND_UNKNOWN, KIND_DEAD_NO_END = (
    "legacy", "ended", "live", "unknown", "dead_no_end")
ATTEMPT_KINDS = frozenset(
    {KIND_LEGACY, KIND_ENDED, KIND_LIVE, KIND_UNKNOWN, KIND_DEAD_NO_END})


@dataclass(frozen=True)
class AttemptVerdict:
    """One attempt as the ledger reads it: ``kind`` from ``ATTEMPT_KINDS``; the
    end line for ``ended`` (``end_status`` None when the end_state is malformed),
    the start line, attempt and liveness for a ``v3`` start without an end."""

    kind: str
    project_id: str
    dispatch_id: str
    attempt: Optional[int] = None
    end_line: Optional[Dict[str, Any]] = None
    end_status: Optional[str] = None
    end_state: Optional[str] = None
    start_line: Optional[Dict[str, Any]] = None
    liveness: Optional[str] = None

    @property
    def is_legacy(self) -> bool:
        return self.kind == KIND_LEGACY


def read_ledger(state_dir: str | Path) -> List[Dict[str, Any]]:
    """Every complete ledger record of this store, in file order, through
    ``open_outcomes.iter_complete_lines``; unreadable raises ``OSError``."""
    from open_outcomes import iter_complete_lines  # local: open_outcomes imports this module

    return list(iter_complete_lines(Path(state_dir) / LEDGER_FILENAME))


def _v3_starts(records: Iterable[Any], project_id: str,
               dispatch_id: str) -> Dict[int, Dict[str, Any]]:
    """This key's ``v3`` start lines by attempt, first line per attempt
    (ADR-045 §9: the mode is fixed at hand-over, never re-read from the setting)."""
    starts: Dict[int, Dict[str, Any]] = {}
    for line in records:
        attempt = line.get("attempt") if isinstance(line, dict) else None
        if (isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1
                or line.get("project_id") != project_id
                or line.get("dispatch_id") != dispatch_id
                or str(line.get("event_type") or "").strip() != DISPATCH_STARTED_EVENT_TYPE
                or str(line.get("lifecycle_mode") or "").strip() != MODE_V3):
            continue
        starts.setdefault(attempt, line)
    return starts


def _own_end_line(records: Iterable[Any], project_id: str, dispatch_id: str,
                  attempt: int) -> Optional[Dict[str, Any]]:
    """This attempt's own end line, first match: a ``dispatch_finished`` whose
    ``project_id``, ``dispatch_id`` and integer ``attempt`` are exactly its key.
    Nothing is defaulted (``ledger_end_line.end_line`` reads a missing attempt
    as 1 and a missing project as any project) — that is the key
    ``dispatch_lifecycle.v3_in_flight`` ends on."""
    for line in records:
        if (isinstance(line, dict)
                and str(line.get("event_type") or "").strip() == DISPATCH_FINISHED_EVENT_TYPE
                and line.get("project_id") == project_id
                and line.get("dispatch_id") == dispatch_id
                and isinstance(line.get("attempt"), int)
                and not isinstance(line.get("attempt"), bool)
                and line.get("attempt") == attempt):
            return line
    return None


def attempt_verdict(state_dir: str | Path, project_id: str, dispatch_id: str, *,
                    records: Optional[Iterable[Any]] = None) -> AttemptVerdict:
    """``records`` is a ledger the caller already read (read once, ask many).
    The ``v3`` branches are the end line and, without one, the liveness of the
    start; no ``v3`` start line means ``legacy`` — the caller's old path."""
    owned: List[Any] = list(records) if records is not None else read_ledger(state_dir)
    starts = _v3_starts(owned, project_id, dispatch_id)
    if not starts:
        return AttemptVerdict(KIND_LEGACY, project_id, dispatch_id)
    attempt = max(starts)
    # A v3 attempt ends only on its own ``dispatch_finished``: the same exact
    # key ``v3_in_flight`` ends on (ADR-045 §2), first match. A line missing a
    # key field, naming another project or attempt, or carrying a malformed
    # attempt (a string, a bool) is no end, so it falls through to the liveness
    # branch exactly as no end line would.
    line = _own_end_line(owned, project_id, dispatch_id, attempt)
    if line is not None:
        state = line.get("end_state")
        return AttemptVerdict(KIND_ENDED, project_id, dispatch_id, attempt=attempt,
                              end_line=line, end_status=end_status(line),
                              end_state=(state.strip().lower() or None)
                              if isinstance(state, str) else None)
    measured, _signal = liveness(state_dir, InFlightAttempt(project_id, dispatch_id,
                                                            attempt, starts[attempt]))
    # a signal outside the three is unknown, never dead: unknown never abandons
    kind = {LIVENESS_LIVE: KIND_LIVE, LIVENESS_DEAD: KIND_DEAD_NO_END,
            LIVENESS_UNKNOWN: KIND_UNKNOWN}.get(measured, KIND_UNKNOWN)
    return AttemptVerdict(kind, project_id, dispatch_id, attempt=attempt,
                          start_line=starts[attempt], liveness=measured)
