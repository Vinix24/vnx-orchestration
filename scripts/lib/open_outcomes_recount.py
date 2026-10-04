#!/usr/bin/env python3
"""open_outcomes_recount.py: a fresh reading of the test evidence next to the stored one.

The ledger is append-only, so the ``verification`` a writer-B receipt carries
is the reading of the reader as it was the day the receipt was written. A
receipt from before a reader repair keeps its old reading for ever, and
``receipt_outcome`` decides on it. ``open-outcomes --recount`` shows, per open
``receipt_outcome`` item, what the same report reads as today and the decision
that reading would give. It is read-only: no ledger line, no decision, no
state file. Only ``receipt_query.py decide`` closes an item.

The report is read by the reader both write paths use
(``envelope_govern_support._verification_from_report``); there is no second
parser here. The receipts are filtered on ``project_id`` first (ADR-007), so
a dispatch id another project reuses never points at that project's report.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from envelope_govern_support import _verification_from_report
from receipt_outcome import _dispatch_id, _is_writer_b, _verdict_status, noise_reason
from receipt_verdict import compute_verdict

NO_REPORT = "no_report"
REPORTS_DIRNAME = "unified_reports"
REFUSED_OUTSIDE = "outside_reports_dir"


def last_report_receipts(
    receipts: Iterable[Dict[str, Any]], project_id: str
) -> Dict[str, Dict[str, Any]]:
    """The last writer-B receipt per dispatch id, over this project's lines only."""
    last: Dict[str, Dict[str, Any]] = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or noise_reason(receipt, project_id) is not None:
            continue
        if _is_writer_b(receipt):
            last[_dispatch_id(receipt)] = receipt
    return last


def report_path_for(receipt: Optional[Dict[str, Any]], dispatch_id: str, data_dir: Path) -> Path:
    """The report named by the receipt, else ``<data_dir>/unified_reports/<id>.md``."""
    reports = Path(data_dir) / REPORTS_DIRNAME
    named = str((receipt or {}).get("report_file") or (receipt or {}).get("report_path") or "").strip()
    if not named:
        return reports / f"{dispatch_id}.md"
    path = Path(named).expanduser()
    return path if path.is_absolute() else reports / path


def _inside_reports_dir(path: Path, data_dir: Path) -> bool:
    """True when ``path``, symlinks followed, lies inside the project's report folder."""
    try:
        path.resolve().relative_to((Path(data_dir) / REPORTS_DIRNAME).resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _stored_reading(receipt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    verification = (receipt or {}).get("verification") or {}
    return {key: verification.get(key) for key in ("method", "tests_run", "tests_failed")}


def _fresh_decision(receipt: Dict[str, Any], status: Any, fresh: Dict[str, Any]) -> str:
    judged = dict(receipt, status=status)
    judged["status"] = _verdict_status(judged)
    judged["verification"] = fresh
    return compute_verdict(judged)["decision"]


def recount_item(
    item: Dict[str, Any], report_receipt: Optional[Dict[str, Any]], data_dir: Path
) -> Dict[str, Any]:
    """The ``recount`` object of one open item."""
    path = report_path_for(report_receipt, item["dispatch_id"], data_dir)
    stored = _stored_reading(report_receipt)
    if not _inside_reports_dir(path, data_dir):
        return {"stored": stored, "report": str(path), "method": None, "tests_run": None,
                "tests_failed": None, "fresh_decision": NO_REPORT, "refused": REFUSED_OUTSIDE}
    if not path.is_file():
        return {"stored": stored, "report": str(path), "method": None, "tests_run": None,
                "tests_failed": None, "fresh_decision": NO_REPORT, "refused": None}
    fresh = _verification_from_report(path)
    return {
        "stored": stored,
        "report": str(path),
        "refused": None,
        "method": fresh.get("method"),
        "tests_run": fresh.get("tests_run"),
        "tests_failed": fresh.get("tests_failed"),
        "fresh_decision": _fresh_decision(report_receipt or {}, item.get("status"), fresh),
    }


def recount_summary(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The count per (stored decision, fresh decision) pair, most frequent first."""
    pairs = Counter((i["outcome"], i["recount"]["fresh_decision"]) for i in items)
    return [{"stored_decision": stored, "fresh_decision": fresh, "count": count}
            for (stored, fresh), count in sorted(pairs.items(), key=lambda p: (-p[1], p[0]))]


def add_recount(
    result: Dict[str, Any], receipts: List[Dict[str, Any]], *, project_id: str, data_dir: Path
) -> Dict[str, Any]:
    """``result`` of ``build_open_outcomes`` with a ``recount`` on every
    ``receipt_outcome`` item and a ``recount_summary`` over them. The input is
    not changed."""
    reports = last_report_receipts(receipts, project_id)
    items = []
    for item in result["items"]:
        if item["kind"] == "receipt_outcome":
            item = dict(item, recount=recount_item(item, reports.get(item["dispatch_id"]), data_dir))
        items.append(item)
    counted = [i for i in items if "recount" in i]
    return dict(result, items=items, recount_summary=recount_summary(counted))
