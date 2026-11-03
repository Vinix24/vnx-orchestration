"""ADR-045 (audit-spoor-kern K-8a): which line a worker report is booked as.

A report is not an end. Legacy: the report-derived receipt stays one
``task_complete`` / ``task_failed`` / ``report_contract_invalid`` line, byte for
byte. v3: one ``report_ingested`` carrying the same payload and claiming no
outcome (ADR-045 §2), while the finalizer's ``dispatch_finished`` is the end.

K-8a is the decision only: nothing calls :func:`decide` or :func:`report_line`
yet. K-8b wires ``report_to_receipt_converter`` and ``report_parser``, K-8c the
restore; every writer, every reader, the append layer and the schema stay
untouched here, so this delivery cannot change a receipt.

:func:`decide` is the ONE decision, in this order:

1. v3 as soon as ONE READABLE source says so — a ``dispatch_started`` of this
   dispatch with ``lifecycle_mode: v3``, or the row's
   ``metadata_json.lifecycle_mode == v3``. A v3 answer is irreversible (ADR-045
   §9) and never consults the current ``ledger_lifecycle`` setting.
2. Otherwise, if EITHER source is unreadable — the ledger, or the row (a store
   whose tenant cannot be addressed included) — the mode is undecidable and the
   decision REFUSES ``lifecycle_mode_undecidable``, naming both sources. An
   unreadable row can hide a v3 mode behind an empty ledger; an unreadable
   ledger can hide a v3 start behind a stale legacy row. Either mistake books a
   second end.
3. Otherwise (both read, neither says v3; an absent DB file or an absent row
   counts as read) the path is legacy, byte-identical to the pre-K-8 head.

The tenant is fixed before anything else is read per tenant: the given
``project_id``, else the one tenant this dispatch's start lines name; two
tenants refuse ``project_ambiguous`` (ADR-007). A v3 report then belongs to the
receipt's own attempt (which must have its own start line), else the only
started attempt, else the start latest at or before ``report_written_at`` (the
write time of the file the caller actually read, never the ingest time), else —
with no start line at all — the row's ``attempt_count``.

The end link comes ONLY from a ``dispatch_finished`` naming this exact
``(project_id, dispatch_id, attempt)``. The legacy completion family never
links: ``ledger_end_line.end_line`` is a reader of history and accepts those by
design, so the narrow lookup lives here instead. No such line: ``end_pending``
is ``True``.

Fail direction: when the mode cannot be decided, or a v3 dispatch has no usable
attempt or end link, :func:`report_line` raises :class:`ReportPathRefused` with
the dispatch id and a named reason. The writer then books NOTHING — neither
``task_complete`` nor a partial ``report_ingested`` — and the report stays for
the next scan.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coordination_db import db_path_from_state_dir
from dispatch_start import MODE_LEGACY, MODE_V3
from receipt_schema import (
    DISPATCH_FINISHED_EVENT_TYPE,
    DISPATCH_STARTED_EVENT_TYPE,
    REPORT_INGESTED_EVENT_TYPE,
)

LEDGER_FILENAME = "t0_receipts.ndjson"

ROW_STORED = "stored"
ROW_ABSENT = "absent"
ROW_UNREADABLE = "unreadable"

logger = logging.getLogger(__name__)

__all__ = [
    "LEDGER_FILENAME", "MODE_LEGACY", "MODE_V3", "LifecyclePath",
    "ReportPathRefused", "ROW_ABSENT", "ROW_STORED", "ROW_UNREADABLE",
    "append_destination_state_dir", "decide", "report_line",
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


def append_destination_state_dir() -> Path:
    """The state dir the receipt APPEND layer itself resolves, env-first.

    Reused from ``append_receipt_internals.idempotency._resolve_state_dir_env_first``
    (never copied): K-8b must hand :func:`decide` the destination the append
    will actually write to, or the mode is decided against a ledger the line
    never reaches.
    """
    from append_receipt_internals.idempotency import (  # noqa: PLC0415
        _resolve_state_dir_env_first,
    )

    return _resolve_state_dir_env_first()


def _require_state_dir(state_dir: object) -> Path:
    """The explicit store, or a TypeError: ``None`` is never "legacy" (finding 5)."""
    if state_dir is None:
        raise TypeError(
            "state_dir is required: the store the receipt is appended to decides the "
            "lifecycle mode, so None is not legacy")
    try:
        return Path(state_dir)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(
            f"state_dir must be path-like, got {type(state_dir).__name__}") from exc


def _positive_int(value: Any) -> Optional[int]:
    """``value`` when it is a positive integer; ``None`` for anything else.

    A bool, float, numeric string or zero is not an attempt: folding it into
    "1" would let a malformed row stand in for a real attempt's key.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _event_type(record: Mapping[str, Any]) -> str:
    return str(record.get("event_type") or record.get("event") or "").strip()


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
    A store whose tenant cannot be addressed is *unreadable*, never legacy: the
    row behind it could be v3.
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


def _names_dispatch(record: Mapping[str, Any], dispatch_id: str) -> bool:
    return str(record.get("dispatch_id") or "").strip() == dispatch_id


def _for_tenant(record: Mapping[str, Any], project_id: Optional[str]) -> bool:
    """A line without ``project_id`` is legacy history and stays readable; a line
    naming another project never counts for this one (ADR-007)."""
    record_project = str(record.get("project_id") or "").strip()
    return not (project_id and record_project and record_project != project_id)


def _resolve_tenant(dispatch_id: str, project_id: Optional[str],
                    starts: List[Dict[str, Any]]) -> Optional[str]:
    """The one tenant: the caller's, else the only one the start lines name.

    Two different tenants on one dispatch's start lines are refused
    (``project_ambiguous``): picking either would attribute the report — and
    its end link — to a tenant that may belong to another store's dispatch.
    """
    if project_id:
        return project_id
    found = {str(line.get("project_id") or "").strip() for line in starts}
    found.discard("")
    if len(found) > 1:
        raise ReportPathRefused(dispatch_id, "project_ambiguous",
            "the dispatch_started lines of this dispatch name more than one tenant "
            f"({', '.join(sorted(found))}), so the report cannot be attributed to one "
            "of them")
    return next(iter(found), None)


def _to_utc(value: Any) -> Optional[datetime]:
    """*value* as an aware UTC datetime, or ``None`` — an unparseable time is no evidence."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _start_time(line: Mapping[str, Any]) -> Any:
    """When this attempt started: the line's ``timestamp`` (``started_at`` fallback)."""
    return line.get("timestamp") or line.get("started_at")


def _resolve_attempt(dispatch_id: str, starts: List[Dict[str, Any]], row: _Row,
                     attempt: Any, report_written_at: Any) -> int:
    """The attempt this report belongs to; refuses rather than guessing one.

    The receipt's own positive attempt (which must have a start line), else the
    only started attempt, else the start latest at or before the report's write
    time, else — with no start line at all — the row's ``attempt_count``. A
    start line without a positive attempt is no attempt; an unparseable start
    time stays ``None`` and never wins the "latest start" comparison.
    """
    supplied = _positive_int(attempt)
    if attempt is not None and supplied is None:
        raise ReportPathRefused(dispatch_id, "attempt_unknown",
            f"the receipt carries attempt {attempt!r}, which is not a positive integer")
    started = [(value, _to_utc(_start_time(line))) for line in starts
               if (value := _positive_int(line.get("attempt"))) is not None]
    if supplied is not None:
        if all(value != supplied for value, _ in started):
            raise ReportPathRefused(dispatch_id, "attempt_unknown",
                f"the receipt carries attempt {supplied}, but no dispatch_started starts "
                f"it (started attempts: {sorted({value for value, _ in started}) or 'none'})")
        return supplied
    distinct = {value for value, _ in started}
    if len(distinct) == 1:
        return next(iter(distinct))
    if not distinct:
        if row.attempt is None:
            raise ReportPathRefused(dispatch_id, "attempt_unknown",
                "this dispatch is v3, but no dispatch_started has a usable attempt and "
                f"the row has no readable attempt_count (row: {row.status}"
                f"{'; ' + row.detail if row.detail else ''})")
        return row.attempt
    when = _to_utc(report_written_at)
    if when is None:
        raise ReportPathRefused(dispatch_id, "attempt_ambiguous",
            f"attempts {sorted(distinct)} have started and the report's own write "
            "time is unknown, so the report cannot be attributed to one of them")
    chosen: Optional[Tuple[datetime, int]] = None
    for value, at in started:
        if at is not None and at <= when and (chosen is None or (at, value) >= chosen):
            chosen = (at, value)
    if chosen is None:
        raise ReportPathRefused(dispatch_id, "attempt_ambiguous",
            f"the report's write time ({when.isoformat()}) is earlier than every "
            f"dispatch_started of attempts {sorted(distinct)}")
    return chosen[1]


def _narrow_end(records: List[Dict[str, Any]], project_id: str, dispatch_id: str,
                attempt: int) -> Optional[Dict[str, Any]]:
    """The last ``dispatch_finished`` with this EXACT (project, dispatch, attempt).

    Deliberately not ``ledger_end_line.end_line``: that reader accepts the
    legacy completion family and reads a missing attempt as attempt 1 by design,
    so a ``task_complete`` (even ``delivery_refused``) would become a permanent
    end link for a running v3 attempt. Here only the exact key links; a line
    without ``project_id`` or ``attempt`` never does.
    """
    found: Optional[Dict[str, Any]] = None
    for record in records:
        if not isinstance(record, dict) or _event_type(record) != DISPATCH_FINISHED_EVENT_TYPE:
            continue
        if str(record.get("project_id") or "").strip() != project_id:
            continue
        if str(record.get("dispatch_id") or "").strip() != dispatch_id:
            continue
        if _positive_int(record.get("attempt")) != attempt:
            continue
        found = record
    return found


def _mode_refusal_detail(records: Optional[List[Dict[str, Any]]],
                         ledger_error: Optional[str], row: _Row) -> str:
    ledger_state = "readable" if records is not None else f"unreadable ({ledger_error})"
    row_state = row.status + (f" ({row.detail})" if row.detail else "")
    return (f"one of the two lifecycle sources is unreadable (ledger: {ledger_state}; "
            f"row: {row_state}), so a v3 dispatch could be missed and end twice")


def decide(*, project_id: Optional[str], dispatch_id: str, state_dir: object,
           attempt: Any = None,
           report_written_at: Any = None) -> LifecyclePath:
    """The one path decision for ``(project_id, dispatch_id)``.

    One readable source saying v3 wins; otherwise an unreadable source refuses
    (``lifecycle_mode_undecidable``); only two readable non-v3 sources stay
    legacy. ``attempt`` / ``report_written_at`` only matter for a v3 answer.
    ``state_dir`` is required: ``None`` raises ``TypeError``, never legacy.
    """
    did = str(dispatch_id or "").strip()
    if not did:
        raise ReportPathRefused(did, "missing_dispatch_id",
                      "the receipt carries no dispatch_id, so no path can be chosen")
    state_path = _require_state_dir(state_dir)
    given_tenant = str(project_id or "").strip() or None
    records, ledger_error = _read_ledger(state_path)
    starts_any = ([] if records is None else
                  [record for record in records
                   if isinstance(record, dict)
                   and _names_dispatch(record, did)
                   and _event_type(record) == DISPATCH_STARTED_EVENT_TYPE])
    tenant = _resolve_tenant(did, given_tenant, starts_any)
    starts = [record for record in starts_any if _for_tenant(record, tenant)]
    row = _read_row(state_path, tenant, did)
    v3_started = any(str(line.get("lifecycle_mode") or "").strip() == MODE_V3
                     for line in starts)
    v3_row = (row.status == ROW_STORED
              and str((row.metadata or {}).get("lifecycle_mode") or "").strip() == MODE_V3)
    if not v3_started and not v3_row:
        if records is None or row.status == ROW_UNREADABLE:
            raise ReportPathRefused(did, "lifecycle_mode_undecidable",
                                    _mode_refusal_detail(records, ledger_error, row))
        return LifecyclePath(MODE_LEGACY, project_id=tenant)
    # A v3 key needs its attempt and end link from the ledger; an unreadable ledger
    # cannot be papered over with ``end_pending`` (that claims "not ended" without evidence).
    if records is None:
        raise ReportPathRefused(did, "ledger_unreadable",
                      f"this dispatch is v3, but its ledger could not be read "
                      f"({ledger_error}), so neither its attempt nor its end link "
                      "can be established")
    if not tenant:
        raise ReportPathRefused(did, "project_unknown",
                      "this dispatch is v3, but no tenant could be resolved for the "
                      "store and no dispatch_started names one")
    chosen = _resolve_attempt(did, starts, row, attempt, report_written_at)
    end = _narrow_end(records, tenant, did, chosen)
    if end is None:
        return LifecyclePath(MODE_V3, project_id=tenant, attempt=chosen, end_pending=True)
    outcome_id = str(end.get("outcome_id") or "").strip()
    if not outcome_id:
        raise ReportPathRefused(did, "end_outcome_id_missing",
                      f"the end line of attempt {chosen} carries no outcome_id, so the "
                      "report line cannot link to it and must not claim end_pending either")
    return LifecyclePath(MODE_V3, project_id=tenant, attempt=chosen,
                         end_outcome_id=outcome_id)


def _report_written_at(receipt: Mapping[str, Any], report_written_at: Any) -> Any:
    """The report's own write time: the caller's value, else the canonical mtime.

    The caller passes the write time of the file it ACTUALLY read; the receipt's
    own ``report_path`` (the canonical form the converter resolves, which can be
    a different file) is only the fallback. A scan can run long after the worker
    finished, so "now" says nothing about the attempt; a missing path yields
    ``None`` (a several-attempt decision refuses).
    """
    if report_written_at is not None:
        return report_written_at
    path = receipt.get("report_path")
    try:
        return os.stat(str(path or "")).st_mtime
    except OSError:
        return None


def report_line(receipt: Dict[str, Any], *, state_dir: object,
                report_written_at: Any = None) -> Dict[str, Any]:
    """The line to book: unchanged for legacy, one ``report_ingested`` for v3.

    The v3 line keeps every payload field the task-shaped receipt carries (model,
    provider, role, report path, verification, findings, status, ...), adds
    ``project_id``, ``attempt`` and exactly one of ``end_outcome_id`` /
    ``end_pending``, and is never rewritten later (ADR-045 §2): a second
    delivery dedups on the outcome identity. The tenant comes from the receipt,
    else from this dispatch's start lines (rule 3); the receipt's own ``attempt``
    decides which attempt the line belongs to; ``report_written_at`` only breaks
    a tie between several started attempts. Raises :class:`ReportPathRefused`
    when the path cannot be decided; the caller books nothing and stays
    retryable.
    """
    dispatch_id = str(receipt.get("dispatch_id") or "").strip()
    project_id = str(receipt.get("project_id") or "").strip() or None
    path = decide(project_id=project_id, dispatch_id=dispatch_id, state_dir=state_dir,
                  attempt=receipt.get("attempt"),
                  report_written_at=_report_written_at(receipt, report_written_at))
    if not path.is_v3:
        return receipt
    line = dict(receipt)
    line.pop("end_outcome_id", None)
    line.pop("end_pending", None)
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
