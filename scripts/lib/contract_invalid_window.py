"""contract_invalid_window.py — staleness windowing for contract_invalid receipts.

report_contract_invalid / contract_invalid is the fleet's #1 governed-failure
signal, but a frozen historical batch (many receipts bulk-emitted with one
old timestamp — a one-time event, not organic churn) reads as "live" in any
counter that has no time bound, or whose bound is wider than the batch's age.

``is_stale_contract_invalid`` gives every counter a shared, configurable
cutoff (default 14 days, ``VNX_CONTRACT_INVALID_WINDOW_DAYS``) so a
contract_invalid receipt older than the window never counts as an active
failure signal, independent of whatever overall lookback window the caller
itself uses.

This module is deliberately scoped to windowing only — NOT classification.
Whether a given dispatch is exempt from the report-body contract in the
first place (panel seats, benchmark harness runs, review roles, ...) is a
separate, unrelated concern that belongs to a future receipt-v2 redesign.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

ENV_WINDOW_DAYS = "VNX_CONTRACT_INVALID_WINDOW_DAYS"
DEFAULT_WINDOW_DAYS = 14


def contract_invalid_window_days() -> int:
    """Configurable staleness threshold (days). Env override, default 14."""
    raw = os.environ.get(ENV_WINDOW_DAYS)
    if not raw:
        return DEFAULT_WINDOW_DAYS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


def _parse_timestamp(value: Optional[Any]) -> Optional[datetime]:
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


def contract_invalid_effective_timestamp(record: Dict[str, Any]) -> Optional[Any]:
    """The timestamp to window a contract_invalid receipt's staleness on.

    Prefers the processor-stamped ``ingested_at`` — set by
    ``append_receipt_payload()`` at receipt-write time, ALWAYS overwritten
    there and never derived from the worker's own report body — over
    ``timestamp``, which ``report_to_receipt_converter.py``/``report_parser.py``
    copy straight out of the merged report frontmatter/body. A worker whose
    report carries a forged old ``timestamp`` can otherwise vanish a fresh
    real failure from every counter that windows on it. Falls back to
    ``timestamp`` only for old v1 records written before ``ingested_at``
    existed — the frozen historical batches this window was built to
    exclude keep their old effective timestamp and stay excluded.
    """
    return record.get("ingested_at") or record.get("timestamp")


def is_stale_contract_invalid(
    record: Dict[str, Any],
    *,
    window_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    """True when a contract_invalid / report_contract_invalid record is older
    than the live-failure window (default 14 days) and must be excluded from
    a live failure count — a frozen historical batch, not ongoing churn.

    Windows exclusively on ``contract_invalid_effective_timestamp(record)``
    (processor ``ingested_at``, falling back to ``timestamp`` only when
    ``ingested_at`` is absent). A missing/unparseable effective timestamp is
    treated as NOT stale (fail-open: only a record we can positively date as
    old is excluded from live counts).
    """
    dt = _parse_timestamp(contract_invalid_effective_timestamp(record))
    if dt is None:
        return False
    days = window_days if window_days is not None else contract_invalid_window_days()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return dt < cutoff
