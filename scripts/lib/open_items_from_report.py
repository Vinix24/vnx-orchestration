"""open_items_from_report.py — push a worker report's ## Open Items entries
into the open-items ledger (OI-1289).

Part of the same pipeline the CLAUDE.md report contract describes:
  report on disk -> receipt processor -> t0_receipts.ndjson (+ open_items.json)

This module does NOT scan unified_reports/ on its own — it is handed the
report text report_to_receipt_converter.py already read, at the point that
report's receipt lands (or is confirmed to already exist). No second report
scanner is introduced.

"Correctly formatted item" reuses validate_report.extract_open_items() — the
same shape the pre-submit format check already enforces
(``- [ ] [blocker|warn|info] Title``) — so this path and that check can never
define "an item" differently. An explicit "None" body and an introductory
prose line both fail that shape and are therefore never picked up; no
special-casing is needed here for either.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent

_OPEN_ITEMS_HEADING = "## Open Items"

# Conservative-default contract (OI-1289): an automatically-ingested item
# never becomes a blocker on its own — severity only rises above this
# default when the report itself is explicit (a real [blocker]/[warn] tag).
_VALID_SEVERITIES = ("blocker", "warn", "info")
_DEFAULT_SEVERITY = "info"


def _normalize_severity(raw_severity: str) -> str:
    """Map a raw severity token to the ledger's vocabulary.

    Falls back to the conservative default (_DEFAULT_SEVERITY) for anything
    outside the recognised set — this is the safety net that keeps a future
    upstream change from silently inflating the blocker count.
    """
    severity = (raw_severity or "").strip().lower()
    return severity if severity in _VALID_SEVERITIES else _DEFAULT_SEVERITY


def _dedup_key(dispatch_id: str, title: str) -> str:
    """Stable dedup key: reprocessing the same report must not duplicate items."""
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"report_oi:{dispatch_id}:{digest}"


def sync_open_items_from_report(
    text: str,
    *,
    dispatch_id: str,
    report_path: str,
    oim: Optional[Any] = None,
) -> List[Tuple[str, bool]]:
    """Register every formatted ``## Open Items`` entry in *text* to the ledger.

    Returns a list of (item_id, created) pairs, one per formatted item found.
    ``created=False`` means the item already existed (dedup_key match), not a
    fresh entry — reprocessing the same report is therefore idempotent.

    Returns ``[]`` when the section is absent, explicitly reports "None", or
    contains only prose (no line matches the formatted-item shape) — no
    ``open_items_manager`` import happens in that case, so a report with zero
    items never pays the STATE_DIR resolution cost.

    *oim* injects an already-loaded ``open_items_manager`` module — used by
    tests to point at an isolated STATE_DIR. Production callers omit it and
    get the real module via a lazy import.

    Never raises: a per-item registration failure (including the acceptance-
    criterion guard's ``ValueError``) is logged and skipped, never fatal to
    the caller's receipt-append flow.
    """
    sys.path.insert(0, str(_SCRIPTS_DIR))
    from report_body_contract import _extract_section  # noqa: PLC0415
    from validate_report import extract_open_items  # noqa: PLC0415

    section = _extract_section(text, _OPEN_ITEMS_HEADING)
    if not section:
        return []

    items = extract_open_items(section)
    if not items:
        return []

    if oim is None:
        import open_items_manager as oim  # noqa: PLC0415

    results: List[Tuple[str, bool]] = []
    for raw_severity, raw_title in items:
        title = raw_title.strip()
        if not title:
            continue
        severity = _normalize_severity(raw_severity)

        try:
            item_id, created = oim.add_item_programmatic(
                title=title,
                severity=severity,
                dispatch_id=dispatch_id,
                report_path=report_path,
                dedup_key=_dedup_key(dispatch_id, title),
                source="report_open_items",
            )
        except ValueError as exc:
            # Acceptance-criterion guard: title reads as a passed check-off,
            # not a problem. Skip it — one bad title must not crash the sync.
            logger.warning(
                "open_items_from_report: skipped item dispatch=%s title=%r: %s",
                dispatch_id, title, exc,
            )
            continue
        except Exception as exc:
            logger.warning(
                "open_items_from_report: registration failed dispatch=%s title=%r: %s",
                dispatch_id, title, exc,
            )
            continue

        results.append((item_id, created))

    return results
