"""ADR-045 (audit-spoor-kern K-8a): which line a worker report is booked as.

Legacy keeps the report-derived receipt byte for byte; v3 books one
``report_ingested`` that claims no outcome (ADR-045 §2), while the
``dispatch_finished`` is the end. Nothing calls this module yet (K-8b wires the
writers, K-8c the restore).

The decision, fail-closed at every gap: v3 when ONE READABLE source says so (a
this-dispatch ``dispatch_started`` with ``lifecycle_mode: v3``, or the row's
``metadata_json.lifecycle_mode``); otherwise EITHER unreadable source (ledger or
row, an unaddressable tenant included) refuses ``lifecycle_mode_undecidable``;
otherwise legacy (an absent DB file or row counts as read). The tenant is fixed
before the row is read; the attempt is the receipt's own (with its own start
line), else the only started one, else the latest at or before the caller's
write time, else the row's ``attempt_count``; the end link is only an exact-key
``dispatch_finished``, and no such line means ``end_pending``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coordination_db import db_path_from_state_dir
from dispatch_start import MODE_LEGACY, MODE_V3
from receipt_schema import DISPATCH_FINISHED_EVENT_TYPE, DISPATCH_STARTED_EVENT_TYPE, REPORT_INGESTED_EVENT_TYPE

LEDGER_FILENAME = "t0_receipts.ndjson"
ROW_STORED, ROW_ABSENT, ROW_UNREADABLE = "stored", "absent", "unreadable"
logger = logging.getLogger(__name__)

__all__ = ["LEDGER_FILENAME", "MODE_LEGACY", "MODE_V3", "LifecyclePath", "ReportPathRefused",
           "ROW_ABSENT", "ROW_STORED", "ROW_UNREADABLE", "append_destination_state_dir", "decide", "report_line"]


class ReportPathRefused(RuntimeError):
    """No line may be booked: ``code`` is the named reason, the dispatch id the key."""

    def __init__(self, dispatch_id: str, code: str, detail: str):
        self.dispatch_id, self.code, self.detail = dispatch_id, code, detail
        super().__init__(f"report path refused for dispatch {dispatch_id!r}: {code}: {detail}")


@dataclass(frozen=True)
class LifecyclePath:
    """One answer: exactly one of ``end_pending`` / ``end_outcome_id`` on a v3 path."""

    mode: str
    project_id: Optional[str] = None
    attempt: Optional[int] = None
    end_outcome_id: Optional[str] = None
    end_pending: bool = False

    @property
    def is_v3(self) -> bool:
        return self.mode == MODE_V3


_Row = namedtuple("_Row", ("status", "metadata", "attempt", "detail"), defaults=(None, None, ""))


def append_destination_state_dir() -> Path:
    """The destination the receipt APPEND layer resolves, env-first; reused, not copied."""
    from append_receipt_internals.idempotency import _resolve_state_dir_env_first  # noqa: PLC0415

    return _resolve_state_dir_env_first()


def _require_state_dir(state_dir: object) -> Path:
    """The explicit store, or a TypeError: ``None`` is never "legacy" (finding 5)."""
    if state_dir is None:
        raise TypeError("state_dir is required: the store the receipt is appended to "
                        "decides the lifecycle mode, so None is not legacy")
    return Path(state_dir)  # type: ignore[arg-type]


def _positive_int(value: Any) -> Optional[int]:
    """``value`` when it is a positive integer; a bool, float, string or zero is not an attempt."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _read_ledger(state_dir: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Every parseable record, or ``(None, reason)``; a missing ledger is readable and empty."""
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


def _read_row(state_dir: Path, project_id: Optional[str], dispatch_id: str) -> "_Row":
    """``dispatches`` for this key, read-only; an unaddressable tenant is unreadable."""
    db_path = Path(db_path_from_state_dir(state_dir))
    if not db_path.is_file():
        return _Row(ROW_ABSENT, detail=f"{db_path} does not exist")
    if not project_id:
        return _Row(ROW_UNREADABLE, detail="the tenant of the store could not be resolved")
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT metadata_json, attempt_count FROM dispatches "
                               "WHERE dispatch_id = ? AND project_id = ?", (dispatch_id, project_id)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # vnx-silent-except: the caller turns it into a refusal or a no-v3 answer
        return _Row(ROW_UNREADABLE, detail=f"cannot read {db_path}: {exc}")
    if row is None:
        return _Row(ROW_ABSENT, detail="no row for this (project_id, dispatch_id)")
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError) as exc:
        return _Row(ROW_UNREADABLE, detail=f"metadata_json is not JSON: {exc}")
    if not isinstance(metadata, dict):
        return _Row(ROW_UNREADABLE, detail=f"metadata_json is {type(metadata).__name__}, not an object")
    return _Row(ROW_STORED, metadata=metadata, attempt=_positive_int(row["attempt_count"]))


def _for_tenant(record: Mapping[str, Any], project_id: Optional[str]) -> bool:
    """A line without ``project_id`` is legacy history; another project never counts (ADR-007)."""
    record_project = str(record.get("project_id") or "").strip()
    return not (project_id and record_project and record_project != project_id)


def _resolve_tenant(dispatch_id: str, project_id: Optional[str],
                    starts: List[Dict[str, Any]]) -> Optional[str]:
    """The given tenant, else the only one the start lines name; two refuse (finding 3)."""
    if project_id:
        return project_id
    found = {str(line.get("project_id") or "").strip() for line in starts}
    found.discard("")
    if len(found) > 1:
        raise ReportPathRefused(dispatch_id, "project_ambiguous",
            f"the dispatch_started lines name more than one tenant ({', '.join(sorted(found))})")
    return next(iter(found), None)


def _to_utc(value: Any) -> Optional[datetime]:
    """*value* as an aware UTC datetime; an unparseable time is no evidence."""
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
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _resolve_attempt(dispatch_id: str, starts: List[Dict[str, Any]], row: "_Row",
                     attempt: Any, report_written_at: Any) -> int:
    """The attempt this report belongs to; refuses rather than guessing one (finding 2/4)."""
    supplied = _positive_int(attempt)
    if attempt is not None and supplied is None:
        raise ReportPathRefused(dispatch_id, "attempt_unknown",
                                f"the receipt carries attempt {attempt!r}, not a positive integer")
    started = [(value, _to_utc(line.get("timestamp") or line.get("started_at"))) for line in starts
               if (value := _positive_int(line.get("attempt"))) is not None]
    if supplied is not None:
        if all(value != supplied for value, _ in started):
            raise ReportPathRefused(dispatch_id, "attempt_unknown",
                f"the receipt carries attempt {supplied}, but no dispatch_started starts it")
        return supplied
    distinct = {value for value, _ in started}
    if len(distinct) == 1:
        return next(iter(distinct))
    if not distinct:
        if row.attempt is None:
            raise ReportPathRefused(dispatch_id, "attempt_unknown",
                "no dispatch_started has a usable attempt and the row has no readable attempt_count "
                f"(row: {row.status}{'; ' + row.detail if row.detail else ''})")
        return row.attempt
    when = _to_utc(report_written_at)
    if when is None:
        raise ReportPathRefused(dispatch_id, "attempt_ambiguous",
            f"attempts {sorted(distinct)} have started and the report's write time is unknown")
    chosen = max(((at, value) for value, at in started if at is not None and at <= when), default=None)
    if chosen is None:
        raise ReportPathRefused(dispatch_id, "attempt_ambiguous",
            f"the report's write time ({when.isoformat()}) is earlier than every start")
    return chosen[1]


def _narrow_end(records: List[Dict[str, Any]], project_id: str, dispatch_id: str,
                attempt: int) -> Optional[Dict[str, Any]]:
    found: Optional[Dict[str, Any]] = None
    for record in records:
        if (isinstance(record, dict) and str(record.get("event_type") or record.get("event") or "").strip() == DISPATCH_FINISHED_EVENT_TYPE
                and str(record.get("project_id") or "").strip() == project_id
                and str(record.get("dispatch_id") or "").strip() == dispatch_id
                and _positive_int(record.get("attempt")) == attempt):
            found = record
    return found


def decide(*, project_id: Optional[str], dispatch_id: str, state_dir: object,
           attempt: Any = None, report_written_at: Any = None) -> LifecyclePath:
    """The one path decision; ``state_dir=None`` raises TypeError, never legacy (finding 5)."""
    did = str(dispatch_id or "").strip()
    if not did:
        raise ReportPathRefused(did, "missing_dispatch_id", "the receipt carries no dispatch_id")
    state_path = _require_state_dir(state_dir)
    records, ledger_error = _read_ledger(state_path)
    starts_any = [] if records is None else [r for r in records if isinstance(r, dict)
        and str(r.get("dispatch_id") or "").strip() == did
        and str(r.get("event_type") or r.get("event") or "").strip() == DISPATCH_STARTED_EVENT_TYPE]
    tenant = _resolve_tenant(did, str(project_id or "").strip() or None, starts_any)
    starts = [record for record in starts_any if _for_tenant(record, tenant)]
    row = _read_row(state_path, tenant, did)
    v3_started = any(str(line.get("lifecycle_mode") or "").strip() == MODE_V3 for line in starts)
    v3_row = row.status == ROW_STORED and str((row.metadata or {}).get("lifecycle_mode") or "").strip() == MODE_V3
    if not (v3_started or v3_row):
        if records is None or row.status == ROW_UNREADABLE:
            ledger_state = "readable" if records is not None else f"unreadable ({ledger_error})"
            row_state = row.status + (f" ({row.detail})" if row.detail else "")
            raise ReportPathRefused(did, "lifecycle_mode_undecidable",
                f"one lifecycle source is unreadable (ledger: {ledger_state}; row: {row_state})")
        return LifecyclePath(MODE_LEGACY, project_id=tenant)
    # An unreadable ledger cannot be papered over with end_pending: that claims "not ended"
    # without evidence of the attempt or of a missing end.
    if records is None:
        raise ReportPathRefused(did, "ledger_unreadable", f"this dispatch is v3, but its ledger is unreadable ({ledger_error})")
    if not tenant:
        raise ReportPathRefused(did, "project_unknown", "this dispatch is v3, but no tenant could be resolved")
    chosen = _resolve_attempt(did, starts, row, attempt, report_written_at)
    end = _narrow_end(records, tenant, did, chosen)
    if end is None:
        return LifecyclePath(MODE_V3, project_id=tenant, attempt=chosen, end_pending=True)
    outcome_id = str(end.get("outcome_id") or "").strip()
    if not outcome_id:
        raise ReportPathRefused(did, "end_outcome_id_missing",
            f"the end line of attempt {chosen} carries no outcome_id, so the report line cannot link")
    return LifecyclePath(MODE_V3, project_id=tenant, attempt=chosen, end_outcome_id=outcome_id)


def _report_written_at(receipt: Mapping[str, Any], report_written_at: Any) -> Any:
    if report_written_at is not None:
        return report_written_at
    try:
        return os.stat(str(receipt.get("report_path") or "")).st_mtime
    except OSError:
        return None


def report_line(receipt: Dict[str, Any], *, state_dir: object,
                report_written_at: Any = None) -> Dict[str, Any]:
    """The line to book: the same object for legacy, one ``report_ingested`` copy for v3."""
    dispatch_id = str(receipt.get("dispatch_id") or "").strip()
    path = decide(project_id=str(receipt.get("project_id") or "").strip() or None,
                  dispatch_id=dispatch_id, state_dir=state_dir, attempt=receipt.get("attempt"),
                  report_written_at=_report_written_at(receipt, report_written_at))
    if not path.is_v3:
        return receipt
    line = dict(receipt)
    line.pop("end_outcome_id", None)
    line.pop("end_pending", None)
    line["event_type"] = REPORT_INGESTED_EVENT_TYPE
    if "event" in line:
        line["event"] = REPORT_INGESTED_EVENT_TYPE
    line["project_id"], line["attempt"] = path.project_id, path.attempt
    if path.end_pending:
        line["end_pending"] = True
    else:
        line["end_outcome_id"] = path.end_outcome_id
    logger.info("report_lifecycle_mode: %s attempt=%s booked as %s", path.project_id,
                path.attempt, REPORT_INGESTED_EVENT_TYPE)
    return line
