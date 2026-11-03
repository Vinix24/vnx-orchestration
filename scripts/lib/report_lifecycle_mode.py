"""ADR-045 (audit-spoor-kern K-8): which line a worker report is booked as.

A report is not an end. Legacy: the report-derived receipt stays one
``task_complete`` / ``task_failed`` / ``report_contract_invalid`` line, byte for
byte. v3: one ``report_ingested`` carrying the same payload and claiming no
outcome (ADR-045 §2), while the finalizer's ``dispatch_finished`` is the end.

Both writers ask this module: ``report_to_receipt_converter`` (scan, public
builder, ``restore_stranded_reports``) and ``report_parser``.

:func:`decide` is the ONE decision: v3 when the dispatch row's
``metadata_json.lifecycle_mode`` says so, or when the ledger holds a
``dispatch_started`` of the dispatch with ``lifecycle_mode: v3``; legacy
otherwise. A v3 answer is irreversible (ADR-045 §9): the mode is fixed at the
hand-over and the current stand of the ``ledger_lifecycle`` setting is never
consulted. The latest start line wins over the row for the attempt; the row's
``attempt_count`` is the fallback.

Fail direction: when the mode cannot be decided (row and ledger both unreadable)
or a v3 dispatch has no usable attempt or end link, :func:`report_line` raises
:class:`ReportPathRefused` with the dispatch id and a named reason. The writer
then books NOTHING — neither ``task_complete`` nor a partial
``report_ingested`` — and the report stays for the next scan.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coordination_db import db_path_from_state_dir
from dispatch_start import MODE_LEGACY, MODE_V3
from ledger_end_line import end_line
from receipt_schema import DISPATCH_STARTED_EVENT_TYPE, REPORT_INGESTED_EVENT_TYPE

LEDGER_FILENAME = "t0_receipts.ndjson"

ROW_STORED = "stored"
ROW_ABSENT = "absent"
ROW_UNREADABLE = "unreadable"

logger = logging.getLogger(__name__)

__all__ = [
    "LEDGER_FILENAME", "MODE_LEGACY", "MODE_V3", "LifecyclePath",
    "ReportPathRefused", "ROW_ABSENT", "ROW_STORED", "ROW_UNREADABLE",
    "decide", "report_line", "resolve_project_id",
]


class ReportPathRefused(RuntimeError):
    """No line may be booked: ``code`` is the named reason, the dispatch id the key."""

    def __init__(self, dispatch_id: str, code: str, detail: str):
        self.dispatch_id, self.code, self.detail = dispatch_id, code, detail
        super().__init__(f"report path refused for dispatch {dispatch_id!r}: {code}: {detail}")


@dataclass(frozen=True)
class LifecyclePath:
    """A usable answer for one ``(project_id, dispatch_id)``.

    Exactly one of ``end_pending`` / ``end_outcome_id`` is set on a v3 path,
    neither on a legacy one.
    """

    mode: str
    project_id: Optional[str] = None
    attempt: Optional[int] = None
    end_outcome_id: Optional[str] = None
    end_pending: bool = False

    @property
    def is_v3(self) -> bool:
        return self.mode == MODE_V3


@dataclass(frozen=True)
class _Row:
    status: str
    metadata: Optional[Dict[str, Any]] = None
    attempt: Optional[int] = None
    detail: str = ""


def _positive_int(value: Any) -> Optional[int]:
    """``value`` when it is a positive integer; ``None`` for anything else.

    A bool, float, numeric string or zero is not an attempt: folding it into
    "1" would let a malformed row stand in for a real attempt's key.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def resolve_project_id(state_dir: object) -> Optional[str]:
    """The tenant at *state_dir*, or ``None`` — the write tier's own resolver
    (ADR-007), never the ``vnx-dev`` guess."""
    if state_dir is None:
        return None
    try:
        # Function-local: importing the QI write tier at module scope would pull
        # the migration stack into every reader of this module.
        from project_scope import resolve_stamp_project_id
    except Exception:  # vnx-silent-except: no resolver means no tenant, never a guessed one
        return None
    try:
        return resolve_stamp_project_id(
            db_path=str(db_path_from_state_dir(Path(state_dir))))
    except Exception:  # vnx-silent-except: an unresolvable tenant is None, the caller decides
        return None


def _read_ledger(state_dir: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Every parseable ledger record, or ``(None, reason)`` when unreadable.

    A missing ledger is a readable empty one; a torn line is skipped (it is not
    evidence of a completed record); an I/O failure is *unreadable*.
    """
    path = state_dir / LEDGER_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    records: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, None


def _read_row(state_dir: Path, project_id: Optional[str], dispatch_id: str) -> _Row:
    """``dispatches`` for this key: stored, absent, or unreadable.

    Never through ``get_connection``: that helper creates the state dir and
    flips the journal mode, and a reader must not write to the store it measures.
    """
    db_path = Path(db_path_from_state_dir(state_dir))
    if not db_path.is_file():
        return _Row(ROW_ABSENT, detail=f"{db_path} does not exist")
    if not project_id:
        return _Row(ROW_UNREADABLE, detail="the tenant of the store could not be "
                                           "resolved, so the row cannot be addressed")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT metadata_json, attempt_count FROM dispatches "
                "WHERE dispatch_id = ? AND project_id = ?",
                (dispatch_id, project_id)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # vnx-silent-except: caller turns it into a refusal or a no-v3 answer
        return _Row(ROW_UNREADABLE, detail=f"cannot read {db_path}: {exc}")
    if row is None:
        return _Row(ROW_ABSENT, detail="no row for this (project_id, dispatch_id)")
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError) as exc:
        return _Row(ROW_UNREADABLE, detail=f"metadata_json is not JSON: {exc}")
    if not isinstance(metadata, dict):
        return _Row(ROW_UNREADABLE,
                    detail=f"metadata_json is {type(metadata).__name__}, not an object")
    return _Row(ROW_STORED, metadata=metadata, attempt=_positive_int(row["attempt_count"]))


def _for_dispatch(record: Mapping[str, Any], project_id: Optional[str],
                  dispatch_id: str) -> bool:
    """The exact key: a line without ``project_id`` is legacy history and stays
    readable; a line naming another project never counts for this one (ADR-007)."""
    if str(record.get("dispatch_id") or "").strip() != dispatch_id:
        return False
    record_project = str(record.get("project_id") or "").strip()
    return not (project_id and record_project and record_project != project_id)


def decide(*, project_id: Optional[str], dispatch_id: str, state_dir: object) -> LifecyclePath:
    """The one path decision for ``(project_id, dispatch_id)``.

    v3 when the row or a ``dispatch_started`` of this dispatch says so, legacy
    otherwise. Refuses when both stores are unreadable, when a v3 dispatch has
    no usable attempt, no resolvable tenant, or an unreadable ledger.
    ``state_dir=None`` means "no store to consult" — legacy, as before K-8.
    """
    did = str(dispatch_id or "").strip()
    if not did:
        raise ReportPathRefused(did, "missing_dispatch_id",
                      "the receipt carries no dispatch_id, so no path can be chosen")
    if state_dir is None:
        return LifecyclePath(MODE_LEGACY, project_id=project_id)

    state_path = Path(state_dir)
    records, ledger_error = _read_ledger(state_path)
    row = _read_row(state_path, project_id, did)

    starts = [record for record in (records or [])
              if _for_dispatch(record, project_id, did)
              and str(record.get("event_type") or "").strip() == DISPATCH_STARTED_EVENT_TYPE]
    v3_starts = [line for line in starts
                 if str(line.get("lifecycle_mode") or "").strip() == MODE_V3]
    if not v3_starts and str((row.metadata or {}).get("lifecycle_mode") or "").strip() != MODE_V3:
        if records is None and row.status == ROW_UNREADABLE:
            raise ReportPathRefused(did, "lifecycle_mode_undecidable",
                          f"neither the ledger ({ledger_error}) nor the dispatch row "
                          f"({row.detail}) could be read, so a v3 dispatch could be missed")
        return LifecyclePath(MODE_LEGACY, project_id=project_id)

    # A v3 key needs its attempt and end link from the ledger; an unreadable
    # ledger cannot be papered over with ``end_pending`` (that would claim "not
    # ended" without evidence).
    if records is None:
        raise ReportPathRefused(did, "ledger_unreadable",
                      f"this dispatch is v3, but its ledger could not be read "
                      f"({ledger_error}), so neither its attempt nor its end link "
                      "can be established")
    resolved_project = (project_id
                        or next((str(line.get("project_id") or "").strip()
                                 for line in reversed(starts)
                                 if str(line.get("project_id") or "").strip()), "")
                        or None)
    if not resolved_project:
        raise ReportPathRefused(did, "project_unknown",
                      "this dispatch is v3, but no tenant could be resolved for the "
                      "store and no dispatch_started names one")
    attempts = [value for value in (_positive_int(line.get("attempt")) for line in starts)
                if value is not None]
    attempt = max(attempts) if attempts else row.attempt
    if attempt is None:
        raise ReportPathRefused(did, "attempt_unknown",
                      "this dispatch is v3, but neither a dispatch_started with a usable "
                      f"attempt nor a readable attempt_count exists (row: {row.status}"
                      f"{'; ' + row.detail if row.detail else ''})")

    end = end_line(records, resolved_project, did, attempt)
    if end is None:
        return LifecyclePath(MODE_V3, project_id=resolved_project, attempt=attempt,
                             end_pending=True)
    outcome_id = str(end.get("outcome_id") or "").strip()
    if not outcome_id:
        raise ReportPathRefused(did, "end_outcome_id_missing",
                      f"the end line of attempt {attempt} carries no outcome_id, so the "
                      "report line cannot link to it and must not claim end_pending either")
    return LifecyclePath(MODE_V3, project_id=resolved_project, attempt=attempt,
                         end_outcome_id=outcome_id)


def report_line(receipt: Dict[str, Any], *, state_dir: object) -> Dict[str, Any]:
    """The line to book: unchanged for legacy, one ``report_ingested`` for v3.

    The v3 line keeps every payload field the task-shaped receipt carries (model,
    provider, role, report path, verification, findings, status, ...), adds
    ``project_id``, ``attempt`` and exactly one of ``end_outcome_id`` /
    ``end_pending``, and is never rewritten later (ADR-045 §2): a second
    delivery dedups on the outcome identity. Raises :class:`ReportPathRefused`
    when the path cannot be decided; the caller books nothing and stays retryable.
    """
    dispatch_id = str(receipt.get("dispatch_id") or "").strip()
    project_id = (str(receipt.get("project_id") or "").strip()
                  or resolve_project_id(state_dir))
    path = decide(project_id=project_id, dispatch_id=dispatch_id, state_dir=state_dir)
    if not path.is_v3:
        return receipt
    line = dict(receipt)
    line["event_type"] = REPORT_INGESTED_EVENT_TYPE
    if "event" in line:
        line["event"] = REPORT_INGESTED_EVENT_TYPE
    line["project_id"] = path.project_id
    line["attempt"] = path.attempt
    if path.end_pending:
        line["end_pending"] = True
    else:
        line["end_outcome_id"] = path.end_outcome_id
    logger.info(
        "report_lifecycle_mode: dispatch=%s project=%s attempt=%s booked as %s (%s)",
        dispatch_id, path.project_id, path.attempt, REPORT_INGESTED_EVENT_TYPE,
        "end_pending" if path.end_pending else f"end_outcome_id={path.end_outcome_id}",
    )
    return line
