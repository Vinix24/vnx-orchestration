"""contract_invalid_ledger.py — visibility and non-silent-closure gating for
contract_invalid receipts (OI-1638).

Measured 05-09 (claudedocs/2026-09-05-golf2-contractpercentage-per-lane.md
§5a): envelope_govern.py's report-body-contract check works — it correctly
stamps 350+ receipts as ``status: "contract_invalid"`` in t0_receipts.ndjson
when ``validate_body()`` rejects a worker's report. But before this module,
every existing reader of that status (weekly_digest.py, learning_loop.py,
check_active_drain.py, receipt_classifier.py, event_outcome_semantics.py,
receipt_verdict.py, router_baseline.py, stop_conditions.py,
deliberation_panel.py — measured via ``grep -rn contract_invalid scripts/``,
NOT "only writers" as first assumed) treats it only as one more failure
bucket for a digest, a drain check, or a routing decision. None of them turn
a single contract_invalid receipt into a durable, per-dispatch consequence:
no open item, no restart, no block, no visible count anywhere a human looks
at session start. This module is the missing read side:

  - ``build_contract_invalid_summary``: the counters build_t0_state.py
    surfaces at every SessionStart (gevolg 1).
  - ``collect_contract_invalid_open`` / ``write_contract_invalid_open_ledger``:
    the single-file open-items ledger — one row per still-open dispatch, not
    one open item per receipt (gevolg 2, "een lijst, geen vloed").
  - ``is_deliverable_acceptable``: the gate a closer must call before
    treating a dispatch's deliverable as done (gevolg 3, "niet stil
    sluitbaar"). See this dispatch's report for the exact call site this
    module does not own.

Staleness windowing is delegated to the existing
``contract_invalid_window.is_stale_contract_invalid`` / the effective
timestamp it defines — never reimplemented here — and outcome
classification (is a given receipt a *governed success*?) is delegated to
``event_outcome_semantics.classify_event_outcome`` for the same reason.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from contract_invalid_window import contract_invalid_effective_timestamp  # noqa: E402
from event_outcome_semantics import classify_event_outcome  # noqa: E402

CONTRACT_INVALID_STATUS = "contract_invalid"
CONTRACT_INVALID_EVENT_TYPE = "report_contract_invalid"

OPEN_LEDGER_FILENAME = "contract_invalid_open.json"

_MIN_AWARE_DATETIME = datetime.min.replace(tzinfo=timezone.utc)


def _parse_ts(value: Optional[Any]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` and naive
    values (assumed UTC). Returns ``None`` on absence or malformed input —
    mirrors ``contract_invalid_window._parse_timestamp`` (not imported: that
    helper is private to its module) so every caller here gets an aware
    datetime or ``None``, never a naive/aware comparison crash.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_contract_invalid(record: Dict[str, Any]) -> bool:
    status = str(record.get("status") or "").strip().lower()
    event_type = str(record.get("event_type") or record.get("event") or "").strip().lower()
    return status == CONTRACT_INVALID_STATUS or event_type == CONTRACT_INVALID_EVENT_TYPE


def _is_governed_success(record: Dict[str, Any]) -> bool:
    event_type = record.get("event_type") or record.get("event")
    status = record.get("status")
    return classify_event_outcome(event_type, status) == "success"


def _read_receipts(receipts_path: Path) -> List[Dict[str, Any]]:
    """Read every well-formed JSON object line from an NDJSON receipts file.

    Absence, an unreadable file, and malformed individual lines all degrade
    to being skipped rather than raising — this is an advisory reader
    (surfacing at SessionStart / a gate check), not a write path.
    """
    if not receipts_path.exists():
        return []
    try:
        text = receipts_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _filter_project(records: List[Dict[str, Any]], project_id: Optional[str]) -> List[Dict[str, Any]]:
    """Scope records to ``project_id`` when given. A record without its own
    project_id passes through (backward compat with pre-tenant-stamping
    receipts) — mirrors the same tolerance in build_t0_state._build_queues.
    """
    pid = (project_id or "").strip()
    if not pid:
        return records
    out = []
    for r in records:
        r_pid = str(r.get("project_id") or "").strip()
        if not r_pid or r_pid == pid:
            out.append(r)
    return out


def _sort_key(record: Dict[str, Any]) -> datetime:
    return _parse_ts(contract_invalid_effective_timestamp(record)) or _MIN_AWARE_DATETIME


def _latest_receipt_per_dispatch(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Latest-by-effective-timestamp receipt for each dispatch_id.

    Records with no dispatch_id are dropped — not attributable to any single
    open/closed decision.
    """
    by_dispatch: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        did = str(rec.get("dispatch_id") or "").strip()
        if not did:
            continue
        by_dispatch.setdefault(did, []).append(rec)
    return {did: sorted(recs, key=_sort_key)[-1] for did, recs in by_dispatch.items()}


def build_contract_invalid_summary(
    receipts_path: Path,
    *,
    now: Optional[datetime] = None,
    window_hours: int = 24,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Counters for SessionStart visibility (OI-1638 gevolg 1).

    Counts every contract_invalid RECORD in the ledger — total, and the
    subset within ``window_hours`` of ``now`` — broken down per provider.
    Deliberately not deduplicated to one-per-dispatch: a dispatch that failed
    the contract twice is two distinct governed-failure events, and the
    write side (envelope_govern.py) appends one receipt per attempt. Use
    ``collect_contract_invalid_open`` for the per-dispatch open/closed view.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    records = _filter_project(_read_receipts(receipts_path), project_id)

    total = 0
    last_window = 0
    by_provider: Dict[str, int] = {}
    by_provider_window: Dict[str, int] = {}
    for rec in records:
        if not _is_contract_invalid(rec):
            continue
        provider = str(rec.get("provider") or "unknown").strip().lower() or "unknown"
        total += 1
        by_provider[provider] = by_provider.get(provider, 0) + 1
        ts = _parse_ts(contract_invalid_effective_timestamp(rec))
        if ts is not None and ts >= cutoff:
            last_window += 1
            by_provider_window[provider] = by_provider_window.get(provider, 0) + 1

    return {
        "total": total,
        "last_24h": last_window,
        "window_hours": window_hours,
        "by_provider": by_provider,
        "by_provider_last_24h": by_provider_window,
    }


def collect_contract_invalid_open(
    receipts_path: Path,
    *,
    project_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Open contract_invalid dispatches — one entry per dispatch, not per
    receipt (OI-1638 gevolg 2, "een lijst, geen vloed").

    A dispatch is OPEN when the LATEST receipt on record for its dispatch_id
    is itself a contract_invalid receipt — i.e. no later attempt superseded
    it with a governed success. A dispatch whose latest receipt is a
    governed success is resolved and excluded entirely (not carried forward
    as a closed row — the caller wants the open backlog, not a growing
    history).
    """
    records = _filter_project(_read_receipts(receipts_path), project_id)
    latest = _latest_receipt_per_dispatch(records)

    open_items: List[Dict[str, Any]] = []
    for dispatch_id, rec in latest.items():
        if not _is_contract_invalid(rec):
            continue
        open_items.append({
            "dispatch_id": dispatch_id,
            "provider": str(rec.get("provider") or "unknown"),
            "timestamp": contract_invalid_effective_timestamp(rec),
            "report_path": rec.get("report_path"),
            "resolved": False,
        })
    open_items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return open_items


def write_contract_invalid_open_ledger(
    receipts_path: Path,
    output_path: Path,
    *,
    project_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write the single-file open-items ledger atomically.

    Atomic write (tmp + os.replace) — this file is read at SessionStart
    while a dispatch may concurrently be appending to the ledger it is
    derived from; a bare ``open(path, 'w')`` could hand a reader a
    truncated/partial file mid-write.
    """
    open_items = collect_contract_invalid_open(receipts_path, project_id=project_id)
    payload = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "open_count": len(open_items),
        "items": open_items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, output_path)
    return payload


def is_deliverable_acceptable(
    dispatch_id: str,
    receipts_path: Path,
    *,
    project_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """False while the LATEST receipt on record for ``dispatch_id`` is
    contract_invalid (OI-1638 gevolg 3) — a dispatch must not be silently
    closed on the strength of a report that never satisfied the report-body
    contract, even if an earlier attempt for the same dispatch_id succeeded.

    A dispatch with no receipt on record at all is NOT this function's
    concern (fail-open, True): that is an absence, and per the fleet's own
    precedent (OI-1624, "a gate outage is absence, never a rejection") an
    absence must not read as a rejection. Callers needing "did this dispatch
    even run" own that separate check themselves.
    """
    did = (dispatch_id or "").strip()
    if not did:
        return False, "empty dispatch_id"

    records = _filter_project(_read_receipts(receipts_path), project_id)
    matching = [r for r in records if str(r.get("dispatch_id") or "").strip() == did]
    if not matching:
        return True, "no receipt on record for dispatch_id (absence, not a rejection)"

    latest = sorted(matching, key=_sort_key)[-1]
    if _is_contract_invalid(latest):
        return False, (
            f"latest receipt for {did} is contract_invalid "
            f"(report_path={latest.get('report_path')!r})"
        )
    return True, "latest receipt is not contract_invalid"


__all__ = [
    "CONTRACT_INVALID_STATUS",
    "CONTRACT_INVALID_EVENT_TYPE",
    "OPEN_LEDGER_FILENAME",
    "build_contract_invalid_summary",
    "collect_contract_invalid_open",
    "write_contract_invalid_open_ledger",
    "is_deliverable_acceptable",
]
