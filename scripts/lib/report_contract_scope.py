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

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from dispatch_spec import _ID_RE  # noqa: E402
from phantom_guard import REVIEW_ROLES, REVIEW_TASK_CLASSES  # noqa: E402

# Dispatch-id prefixes for dispatch classes that never produce a unified build
# report. Matched case-insensitively against the START of dispatch_id.
_PANEL_PREFIX = "panel-"
_BENCH_PREFIXES = ("bench-", "smoke-")

# Status dirs a staged bundle can be in at classification time (stage_spec_bundle
# writes to pending/; promotion/reap machinery moves it to active/ or completed/).
_DISPATCH_STATUS_DIRS = ("pending", "active", "completed")

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


# ---------------------------------------------------------------------------
# Authoritative-record resolution (governance bypass fix)
#
# classify_non_report_dispatch() above is pure: it classifies whatever
# role/task_class/read_only/dispatch_id it is handed, with no opinion on
# WHERE those values came from. Both call sites historically handed it
# values pulled straight from the WORKER'S OWN report body — a real
# build-worker whose report is broken can therefore forge
# role: code-reviewer / task_class: research_structured / read_only: true
# in its frontmatter and self-exempt from report_contract_invalid. The
# functions below resolve the AUTHORITATIVE role/task_class for a
# dispatch_id from governed fabric sources the worker cannot write, and
# classify_report_dispatch() is the safe entry point call sites must use
# instead of calling classify_non_report_dispatch() directly with
# report-body values.
# ---------------------------------------------------------------------------

def _load_dispatch_spec_role(dispatch_id: str, data_dir: Path) -> Optional[Dict[str, Optional[str]]]:
    """Look up role/task_class from a staged ``dispatch-spec.json``.

    Searches ``dispatches/{pending,active,completed}/<dispatch_id>/dispatch-spec.json``
    — written by ``dispatch_bridge.stage_spec_bundle()`` at staging time, NEVER
    by the worker. Returns None when no spec bundle exists for this
    dispatch_id (reaped already, or never a governed dispatch to begin with).
    """
    for status in _DISPATCH_STATUS_DIRS:
        spec_path = data_dir / "dispatches" / status / dispatch_id / "dispatch-spec.json"
        if not spec_path.is_file():
            continue
        try:
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        return {"role": raw.get("role"), "task_class": raw.get("task_class")}
    return None


def _load_dispatch_register_role(dispatch_id: str, state_dir: Path) -> Optional[Dict[str, Optional[str]]]:
    """Fallback lookup: scan ``dispatch_register.ndjson`` for this dispatch_id's role.

    Checks both known register locations (legacy ``<state_dir>/dispatch_register.ndjson``
    and the ADR-005 transactional ``<state_dir>/../events/dispatch_register.ndjson``).
    Returns the role/task_class carried in the ``extra`` payload of the most
    recent matching record, or None when nothing usable is found. Used only
    when a ``dispatch-spec.json`` bundle has already been reaped.
    """
    candidates = (
        state_dir / "dispatch_register.ndjson",
        state_dir.parent / "events" / "dispatch_register.ndjson",
    )
    found: Optional[Dict[str, Optional[str]]] = None
    for register_path in candidates:
        if not register_path.is_file():
            continue
        try:
            with register_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or rec.get("dispatch_id") != dispatch_id:
                        continue
                    extra = rec.get("extra")
                    if not isinstance(extra, dict):
                        continue
                    if extra.get("role") or extra.get("task_class"):
                        found = {"role": extra.get("role"), "task_class": extra.get("task_class")}
        except OSError:
            continue
    return found


def resolve_dispatch_authority(
    dispatch_id: Optional[str],
    *,
    state_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> Optional[Dict[str, Optional[str]]]:
    """Resolve role/task_class for dispatch_id from GOVERNED sources only.

    Never reads the worker's own report. Tries the staged ``dispatch-spec.json``
    first (authoritative, written before the worker ever runs), then
    ``dispatch_register.ndjson`` as a fallback for a dispatch whose spec bundle
    has already been reaped. Returns None when neither source has a record for
    this dispatch_id — a genuinely ungoverned fabric dispatch (a panel seat or
    benchmark cell whose id was assigned by trusted fabric machinery, not a
    reviewed worker), where the dispatch_id-prefix signal remains valid.

    ``data_dir`` defaults to ``state_dir.parent`` (the standing convention
    elsewhere in this codebase — see ``dispatch_register.register_proposed_track_dispatch``
    and ``dispatch_cli._authority_from_spec_path``: state dir is always
    ``<data_dir>/state``).
    """
    if not dispatch_id or not _ID_RE.match(dispatch_id):
        return None
    if data_dir is None and state_dir is not None:
        data_dir = state_dir.parent
    if data_dir is not None:
        found = _load_dispatch_spec_role(dispatch_id, data_dir)
        if found is not None:
            return found
    if state_dir is not None:
        found = _load_dispatch_register_role(dispatch_id, state_dir)
        if found is not None:
            return found
    return None


def classify_report_dispatch(
    dispatch_id: Optional[str] = None,
    *,
    role: Optional[str] = None,
    task_class: Optional[str] = None,
    read_only: Optional[bool] = None,
    state_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> Optional[str]:
    """Governance-safe wrapper call sites must use instead of
    ``classify_non_report_dispatch()`` directly.

    A worker's own report body can claim ANY role/task_class/read_only/
    dispatch_id — those fields are not authoritative and must never, by
    themselves, grant a report_contract_invalid exemption (a broken
    build-worker report could otherwise self-exempt by forging its
    frontmatter). This resolves the dispatch's AUTHORITATIVE role/task_class
    first (staged ``dispatch-spec.json``, then ``dispatch_register.ndjson``).

    When an authoritative record exists, classification uses ONLY that
    record: the report-body ``role``/``task_class``/``read_only`` arguments
    are ignored outright, and the ``dispatch_id`` prefix signal is disabled
    (``dispatch_id=None`` passed through) — a governed dispatch's id prefix
    is coincidental, not a fabric-assigned classification.

    Only when NO authoritative record exists (a genuinely ungoverned fabric
    dispatch — panel seat, benchmark/smoke cell) do the body-supplied fields
    and the dispatch_id prefix apply, exactly as ``classify_non_report_dispatch``
    already does.
    """
    authority = resolve_dispatch_authority(dispatch_id, state_dir=state_dir, data_dir=data_dir)
    if authority is not None:
        return classify_non_report_dispatch(
            dispatch_id=None,
            role=authority.get("role"),
            task_class=authority.get("task_class"),
            read_only=None,
        )
    return classify_non_report_dispatch(
        dispatch_id=dispatch_id, role=role, task_class=task_class, read_only=read_only,
    )


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


def contract_invalid_effective_timestamp(record: Dict[str, Any]) -> Optional[Any]:
    """The timestamp to window a ``contract_invalid`` receipt's staleness on.

    Prefers ``ingested_at`` — stamped by ``append_receipt_payload()`` at
    receipt-write time, NEVER derived from the worker's own report body —
    over ``timestamp``/``recorded_at``, which ``report_to_receipt_converter.py``
    copies straight out of the merged report frontmatter/body. A worker whose
    report carries a forged old ``timestamp`` can otherwise vanish a fresh
    real failure from every counter that windows on it. Falls back to
    ``timestamp`` then ``recorded_at`` for receipts written before
    ``ingested_at`` existed — the frozen historical batches this window was
    built to exclude keep their old effective timestamp and stay excluded.
    """
    return record.get("ingested_at") or record.get("timestamp") or record.get("recorded_at")


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
