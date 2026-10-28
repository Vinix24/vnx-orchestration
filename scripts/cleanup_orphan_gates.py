#!/usr/bin/env python3
"""cleanup_orphan_gates.py — resolve gate requests older than 24h with no matching result.

An orphan gate request is a ``review_gates/requests/`` record with no
corresponding ``results/`` record. Ending it is a gate outcome, so it goes
through the one gate writer, ``gate_outcome.commit_gate_outcome``: under the
slot's lock that writes one result record, books its one
``review_gate_result`` receipt and ends the request. Nothing here writes a gate
record itself (ADR-046 §2), and ``tests/test_oi1472_residual_guard_writers.py``
lists this module in its writer guard.

The orphan becomes ``unavailable`` — there is no ``abandoned`` outcome
(ADR-046 §1) — with the reason ``abandoned_orphan_request``, who ended it, the
age in hours and the source request stem. The request ends ``unavailable``
through ``gate_outcome.REQUEST_STATUS_OF``, so ``execute_gate`` short-circuits
on it and a real retry is a new request.

The store is resolved through ``project_root.resolve_data_dir`` (ADR-026,
issue #225): ``$VNX_DATA_DIR`` needs ``VNX_DATA_DIR_EXPLICIT=1``; never a
repo-relative ``.vnx-data`` fallback.

Usage: ``python3 scripts/cleanup_orphan_gates.py [--dry-run]``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from gate_outcome import (  # noqa: E402
    GateOutcome, GateRequest, RequestWriteFailed, commit_gate_outcome,
)
from project_root import resolve_data_dir  # noqa: E402

ABANDONED_ORPHAN_REQUEST = "abandoned_orphan_request"
ABANDONED_BY = "cleanup_orphan_gates"

# ``main`` points these at the CLI overrides; otherwise the resolver decides.
_REQUESTS_DIR: Optional[Path] = None
_RESULTS_DIR: Optional[Path] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_dirs() -> Tuple[Path, Path]:
    """The store's pair from the central resolver — never a repo-relative fallback."""
    review_gates = resolve_data_dir(__file__) / "state" / "review_gates"
    return review_gates / "requests", review_gates / "results"


def _current_dirs() -> Tuple[Path, Path]:
    """The live pair: ``main``'s CLI override first, else the resolver's."""
    if _REQUESTS_DIR is None or _RESULTS_DIR is None:
        return _gate_dirs()
    return _REQUESTS_DIR, _RESULTS_DIR


def _file_age_hours(path: Path) -> float:
    """Return age of file in hours."""
    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except OSError:
        return 0.0


def _read_request(path: Path) -> Dict[str, Any]:
    """Parse request file. Return empty dict on failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_pr_number_from_stem(stem: str) -> Optional[int]:
    """Extract PR number from gate request stem like 'pr-57-gemini_review'."""
    match = re.match(r"^pr-(\d+)-", stem)
    return int(match.group(1)) if match else None


def _slot_identity(stem: str, request_data: Dict[str, Any]) -> Tuple[str, Optional[int], str]:
    """The ``(gate, pr_number, pr_id)`` that reproduce this request file's own name.

    The filename is authoritative: the request must be ENDED at the path it was
    found at, and ``persist_request`` re-derives that path from these values. A
    payload ``pr_id`` on a ``pr-<N>-<gate>.json`` request names another slot.
    """
    if stem.endswith("-contract"):
        base = stem[: -len("-contract")]
        pr_slug, sep, gate = base.rpartition("-")
        if sep and gate:
            return gate, None, pr_slug
    number, sep, gate = stem[3:].partition("-") if stem.startswith("pr-") else ("", "", "")
    if sep and gate and number.isdigit():
        return gate, int(number), ""
    # A non-standard stem: fall back to what the payload names.
    gate = request_data.get("gate")
    gate_name = gate.strip() if isinstance(gate, str) and gate.strip() else stem
    pr_number = request_data.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        pr_number = _extract_pr_number_from_stem(stem)
    pr_id = request_data.get("pr_id")
    return gate_name, pr_number, pr_id.strip() if isinstance(pr_id, str) else ""


def _find_orphans(max_age_hours: float = 24.0) -> List[Dict[str, Any]]:
    """Orphans: request files with no result, older than ``max_age_hours`` (dicts)."""
    requests_dir, results_dir = _current_dirs()
    if not requests_dir.exists():
        return []

    orphans: List[Dict[str, Any]] = []
    for req_path in sorted(requests_dir.glob("*.json")):
        result_path = results_dir / req_path.name
        if result_path.exists():
            continue  # already has a result
        age = _file_age_hours(req_path)
        if age < max_age_hours:
            continue  # too recent

        stem = req_path.stem
        request_data = _read_request(req_path)
        gate_name, pr_number, pr_id = _slot_identity(stem, request_data)
        orphans.append({
            "request_path": req_path,
            "result_path": result_path,
            "gate_name": gate_name,
            "pr_number": pr_number,
            "pr_id": pr_id,
            "stem": stem,
            "age_hours": round(age, 1),
            "request_data": request_data,
        })

    return orphans


def _write_abandoned_result(
    orphan: Dict[str, Any], dry_run: bool, requests_dir: Optional[Path] = None,
) -> bool:
    """Book the orphan's ``unavailable`` outcome through the one gate writer.

    True when resolved (written, dry run, or already resolved by another
    writer); False only on a write failure. A refusal is not an error here.
    """
    if dry_run:
        return True

    request_data = orphan.get("request_data") if isinstance(orphan.get("request_data"), dict) else {}
    if "pr_number" in orphan or "pr_id" in orphan:
        pr_number, pr_id = orphan.get("pr_number"), orphan.get("pr_id") or ""
    else:
        _gate, pr_number, pr_id = _slot_identity(orphan["stem"], request_data)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gate": orphan["gate_name"],
        "pr_id": pr_id,
        "pr_number": pr_number,
        "status": "unavailable",
        "reason": ABANDONED_ORPHAN_REQUEST,
        "abandoned_by": ABANDONED_BY,
        "age_hours": orphan["age_hours"],
        "source_request": orphan["stem"],
        "original_request": request_data,
    }
    # ``pr_id`` stays what the filename names: a filled-in ``str(pr_number)``
    # makes ``persist_request`` end a contract slot, not this orphan's own file.
    outcome = GateOutcome(
        gate=orphan["gate_name"], status="unavailable", pr_id=pr_id,
        pr_number=pr_number, record=record,
    )
    # A slot that already holds a record is not ours to end twice: the writer
    # refuses the record, and ending the request is that slot's writer's job.
    request = None
    if not orphan["result_path"].exists():
        request = GateRequest(
            requests_dir=requests_dir or _current_dirs()[0],
            payload=dict(request_data),
            fields={"reason": ABANDONED_ORPHAN_REQUEST, "abandoned_by": ABANDONED_BY},
        )
    try:
        _payload, written = commit_gate_outcome(
            outcome,
            results_dir=orphan["result_path"].parent,
            result_path=orphan["result_path"],
            request=request,
            book_undecided_receipt=True,
        )
    except (OSError, RequestWriteFailed) as exc:
        print(f"  [ERROR] Failed to write result for {orphan['stem']}: {exc}", file=sys.stderr)
        return False

    if not written:
        print(f"  {orphan['stem']}  already resolved: {orphan['result_path'].name} exists")
    return True


def _log_to_audit(orphans: List[Dict[str, Any]]) -> None:
    """Append cleanup event to governance_audit.ndjson."""
    try:
        from governance_audit import log_enforcement  # noqa: PLC0415

        for orphan in orphans:
            pr_number = _extract_pr_number_from_stem(orphan["stem"])
            dispatch_id = orphan.get("request_data", {}).get("dispatch_id") or None
            context: dict = {
                "stem": orphan["stem"],
                "age_hours": orphan["age_hours"],
                "gate_name": orphan["gate_name"],
            }
            if pr_number is not None:
                context["pr_number"] = pr_number
            if dispatch_id is not None:
                context["dispatch_id"] = dispatch_id
            log_enforcement(
                check_name="orphan_gate_cleanup",
                level=1,
                result=True,
                context=context,
                message=(
                    f"Orphan gate {orphan['stem']} abandoned after "
                    f"{orphan['age_hours']:.1f}h with no result"
                ),
                dispatch_id=dispatch_id,
            )
    except Exception as exc:  # vnx-broad-except: audit write is best-effort; the cleanup already landed
        print(f"  [WARN] Could not write to governance_audit: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve orphan gate requests with no result (>24h old).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Minimum age in hours to consider a request orphaned (default: 24).")
    parser.add_argument("--requests-dir", default=None, help="Override gate requests directory path.")
    parser.add_argument("--results-dir", default=None, help="Override gate results directory path.")
    args = parser.parse_args(argv)

    global _REQUESTS_DIR, _RESULTS_DIR
    _REQUESTS_DIR, _RESULTS_DIR = _gate_dirs()
    if args.requests_dir:
        _REQUESTS_DIR = Path(args.requests_dir)
    if args.results_dir:
        _RESULTS_DIR = Path(args.results_dir)

    orphans = _find_orphans(max_age_hours=args.max_age_hours)

    if not orphans:
        print(f"No orphan gate requests found (threshold: {args.max_age_hours:.0f}h).")
        return 0

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode_label}Found {len(orphans)} orphan gate request(s):\n")

    resolved_orphans: List[Dict[str, Any]] = []
    failed = 0
    for orphan in orphans:
        status = "would book" if args.dry_run else "booking"
        print(
            f"  {orphan['stem']}  age={orphan['age_hours']:.1f}h  "
            f"gate={orphan['gate_name']}  → {status} unavailable outcome"
        )
        ok = _write_abandoned_result(orphan, dry_run=args.dry_run)
        if ok:
            resolved_orphans.append(orphan)
        else:
            failed += 1

    if not args.dry_run and resolved_orphans:
        _log_to_audit(resolved_orphans)
        print(f"\nResolved {len(resolved_orphans)} orphan(s). Logged to governance_audit.ndjson.")

    if failed:
        print(f"\n[WARN] {failed} write failure(s) — check stderr.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
