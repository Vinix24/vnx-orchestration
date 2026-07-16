"""report_contract_scope.py — scope guard for the report-body-contract validator.

Two problems, one root: `report_contract_invalid` is measured as the fleet's #1
"governed failure" signal, but the counter conflates two different things.

1. Scope: the report-body contract (`report_body_contract.validate_body`) checks
   for headings (`## Changes`, `## Verification`, ...) that only a delivery
   worker who actually changed files would write. Panel/deliberation seats and
   benchmark/smoke harness runs never produce a diff by design — scoring their
   reports against the contract manufactures noise for dispatch classes the
   contract was never meant to police. `classify_non_report_dispatch` follows
   the existing phantom_guard.py exemption taxonomy (REVIEW_ROLES,
   task_class="research_structured", read_only=True) exactly, then extends it
   with the dispatch_id-prefix classes observed in the fleet data (panel-*
   deliberation seats, bench-*/smoke-* harness runs) that carry neither a role
   nor a task_class in their report body.

2. Staleness: a frozen historical batch (many `contract_invalid` receipts all
   sharing one old timestamp — a one-time bulk event, not organic churn) reads
   as "live" in any counter that has no time bound, or whose bound is wider
   than the batch's age. `is_stale_contract_invalid` gives every counter a
   shared, configurable cutoff (default 14 days, `VNX_CONTRACT_INVALID_WINDOW_DAYS`)
   so a `contract_invalid` receipt older than the window never counts as an
   active failure signal, regardless of what overall lookback window the
   caller itself uses.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from phantom_guard import REVIEW_ROLES, REVIEW_TASK_CLASSES  # noqa: E402

# Dispatch-id prefixes for dispatch classes that never produce a unified build
# report. Matched case-insensitively against the START of dispatch_id.
_PANEL_PREFIX = "panel-"
_BENCH_PREFIXES = ("bench-", "smoke-")

ENV_WINDOW_DAYS = "VNX_CONTRACT_INVALID_WINDOW_DAYS"
DEFAULT_WINDOW_DAYS = 14


def truthy(value: Optional[Any]) -> bool:
    """Coerce a frontmatter/body-field value ("true", "1", True, ...) to bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def classify_non_report_dispatch(
    *,
    dispatch_id: Optional[str] = None,
    role: Optional[str] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> Optional[str]:
    """Return the exemption reason (`report_class`) when this dispatch is NOT
    expected to produce a unified build report, or None when it IS — i.e. the
    report-body contract applies and a violation is a real `report_contract_invalid`.

    Mirrors phantom_guard.phantom_guard's read_only/role/task_class exemption
    order exactly, then extends it with dispatch_id-prefix classes for the
    non-report shapes that carry neither a role nor a task_class in their
    report body (panel deliberation seats, benchmark/smoke harness runs).
    """
    if read_only:
        return "read_only"
    if role is not None and role.strip().lower() in REVIEW_ROLES:
        return "review_role"
    if task_class is not None and task_class.strip().lower() in REVIEW_TASK_CLASSES:
        return "research_structured"
    did = (dispatch_id or "").strip().lower()
    if did.startswith(_PANEL_PREFIX):
        return "panel_seat"
    if did.startswith(_BENCH_PREFIXES):
        return "benchmark"
    return None


def is_report_producing(
    *,
    dispatch_id: Optional[str] = None,
    role: Optional[str] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> bool:
    """True when the report-body contract applies to this dispatch."""
    return classify_non_report_dispatch(
        dispatch_id=dispatch_id, role=role, task_class=task_class, read_only=read_only,
    ) is None


def contract_invalid_window_days() -> int:
    """Configurable staleness threshold (days). Env override, default 14."""
    raw = os.environ.get(ENV_WINDOW_DAYS)
    if not raw:
        return DEFAULT_WINDOW_DAYS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
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


def is_stale_contract_invalid(
    timestamp: Optional[str],
    *,
    window_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    """True when a contract_invalid / report_contract_invalid receipt is older
    than the live-failure window (default 14 days) and must be excluded from a
    live failure count — a frozen historical batch, not ongoing churn.

    A missing/unparseable timestamp is treated as NOT stale (fail-open: only a
    receipt we can positively date as old is excluded from live counts).
    """
    dt = _parse_timestamp(timestamp)
    if dt is None:
        return False
    days = window_days if window_days is not None else contract_invalid_window_days()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    return dt < cutoff
