"""ADR-045 §7 (audit-spoor-kern K-7): the verdict of one dispatch attempt.

``attempt_verdict`` is the one reader the active readers ask whether a dispatch
is done: ``legacy`` (no ``v3`` start line — the caller keeps its old path),
``ended`` (the highest attempt has its own ``dispatch_finished``), or a ``v3``
start without an end that is ``live``, of ``unknown`` liveness, or
``dead_no_end``. The end line is ``ledger_end_line``'s and the liveness
``dispatch_lifecycle``'s, so neither is implemented twice; an unreadable ledger
raises ``OSError``, never "done".

``read_ledger`` keeps the raw text of every interior line that did not parse, so
a damaged line about THIS dispatch makes the verdict unreadable
(``NdjsonCorruptionError``) instead of silently dropping that dispatch to
``legacy``. A broken line that names another dispatch blocks only that one
(K-act r2).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dispatch_lifecycle import (LIVENESS_DEAD, LIVENESS_LIVE, LIVENESS_UNKNOWN,
                                InFlightAttempt, liveness)
from ledger_end_line import DISPATCH_FINISHED_EVENT_TYPE, end_line, end_status
from ndjson_io import TORN_TAIL_CLOSED, NdjsonCorruptionError, scan_ndjson_lines
from receipt_schema import DISPATCH_STARTED_EVENT_TYPE

logger = logging.getLogger(__name__)

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


class LedgerRecords(list):
    """The parsed ledger records, plus the raw text of every interior line that
    did not parse. Callers that only iterate see a plain list (it IS one)."""

    def __init__(self, records: Iterable[Any] = (),
                 corrupt_lines: Iterable[Tuple[int, str]] = ()) -> None:
        super().__init__(records)
        # (line number, raw text) for each non-torn, non-closed line that failed to parse
        self.corrupt_lines: Tuple[Tuple[int, str], ...] = tuple(corrupt_lines)


def read_ledger(state_dir: str | Path, *, path: Optional[str | Path] = None) -> LedgerRecords:
    """Every complete ledger record of this store, in file order, read with the
    one torn-tail rule (``ndjson_io.scan_ndjson_lines``).

    ``path`` overrides the default ``<state_dir>/t0_receipts.ndjson``. A torn
    final fragment and ``TORN_TAIL_CLOSED`` debris are skipped; an interior line
    that did not parse is skipped as a record but its raw text is kept in
    ``corrupt_lines`` so a reader scoped to one dispatch can refuse it. An
    unreadable store raises ``OSError``.
    """
    ledger_path = Path(path) if path is not None else Path(state_dir) / LEDGER_FILENAME
    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError:
        return LedgerRecords()
    records: List[Any] = []
    corrupt: List[Tuple[int, str]] = []
    for number, raw, record, error, torn in scan_ndjson_lines(data):
        if error is None:
            if isinstance(record, dict):
                records.append(record)
        elif raw.endswith(TORN_TAIL_CLOSED):
            logger.warning("ledger_attempt_state: closed torn fragment on line %d of %s",
                           number, ledger_path)
        elif torn:
            logger.debug("ledger_attempt_state: skipping torn tail line %d in %s",
                         number, ledger_path)
        else:
            logger.warning("ledger_attempt_state: skipping malformed NDJSON line %d in %s",
                           number, ledger_path)
            corrupt.append((number, raw.decode("utf-8", errors="replace")))
    return LedgerRecords(records, corrupt)


def _refuse_corruption(records: Iterable[Any], project_id: str, dispatch_id: str,
                       state_dir: str | Path) -> None:
    """Raise when a line that did not parse carries THIS dispatch id.

    A broken line is unreadable evidence about the dispatch whose text it holds;
    a broken line about another dispatch says nothing about this one. Lists that
    did not come from ``read_ledger`` carry no raw text and are left alone.
    """
    did = str(dispatch_id or "").strip()
    if not did:
        return
    for number, raw in getattr(records, "corrupt_lines", ()):
        if did in raw:
            raise NdjsonCorruptionError(
                Path(state_dir) / LEDGER_FILENAME, number,
                f"a line carrying dispatch_id {did!r} did not parse")


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


def attempt_verdict(state_dir: str | Path, project_id: str, dispatch_id: str, *,
                    records: Optional[Iterable[Any]] = None) -> AttemptVerdict:
    """``records`` is a ledger the caller already read (read once, ask many).

    A ``v3`` attempt ends only on the ``dispatch_finished`` line of its highest
    attempt (``max(starts)``); a legacy completion record, or an end line of a
    lower attempt, is not an end and falls through to liveness exactly as "no
    end" does. No ``v3`` start line means ``legacy`` — the caller's old path.
    A line that did not parse and carries this dispatch id raises
    ``NdjsonCorruptionError``: the dispatch is unreadable, never ``legacy``.
    """
    if records is not None:
        _refuse_corruption(records, project_id, dispatch_id, state_dir)
        owned: List[Any] = list(records)
    else:
        records_read = read_ledger(state_dir)
        _refuse_corruption(records_read, project_id, dispatch_id, state_dir)
        owned = list(records_read)
    starts = _v3_starts(owned, project_id, dispatch_id)
    if not starts:
        return AttemptVerdict(KIND_LEGACY, project_id, dispatch_id)
    attempt = max(starts)
    line = end_line(owned, project_id, dispatch_id, attempt=attempt)
    if line is not None and (str(line.get("event_type") or "").strip().lower()
                             == DISPATCH_FINISHED_EVENT_TYPE):
        finished_attempt, state = line.get("attempt"), line.get("end_state")
        if (isinstance(finished_attempt, bool) or not isinstance(finished_attempt, int)
                or finished_attempt < 1):
            finished_attempt = None
        state = (state.strip().lower() or None) if isinstance(state, str) else None
        return AttemptVerdict(KIND_ENDED, project_id, dispatch_id, attempt=finished_attempt,
                              end_line=line, end_status=end_status(line), end_state=state)
    measured, _signal = liveness(state_dir, InFlightAttempt(project_id, dispatch_id,
                                                            attempt, starts[attempt]))
    # a signal outside the three is unknown, never dead: unknown never abandons
    kind = {LIVENESS_LIVE: KIND_LIVE, LIVENESS_DEAD: KIND_DEAD_NO_END,
            LIVENESS_UNKNOWN: KIND_UNKNOWN}.get(measured, KIND_UNKNOWN)
    return AttemptVerdict(kind, project_id, dispatch_id, attempt=attempt,
                          start_line=starts[attempt], liveness=measured)
