#!/usr/bin/env python3
"""cleanup_orphan_gates.py — Resolve gate requests older than 24h with no matching result.

Orphan gate requests are requests/ files that have no corresponding results/ file.
This script marks them as "abandoned" and logs the cleanup to governance_audit.ndjson.

Usage:
    python3 scripts/cleanup_orphan_gates.py --dry-run   # preview only
    python3 scripts/cleanup_orphan_gates.py             # write result files + audit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VNX_DATA_DIR = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))
_GATE_REQUESTS_DIR = _VNX_DATA_DIR / "state" / "review_gates" / "requests"
_GATE_RESULTS_DIR = _VNX_DATA_DIR / "state" / "review_gates" / "results"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_age_hours(path: Path) -> float:
    """Return age of file in hours."""
    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except OSError:
        return 0.0


def _read_request(path: Path) -> dict:
    """Parse request file. Return empty dict on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_orphans(max_age_hours: float = 24.0) -> list[dict]:
    """Find gate request files with no corresponding result, older than max_age_hours.

    Returns a list of dicts with: request_path, gate_name, stem, age_hours.
    """
    if not _GATE_REQUESTS_DIR.exists():
        return []

    orphans = []
    for req_path in sorted(_GATE_REQUESTS_DIR.glob("*.json")):
        result_path = _GATE_RESULTS_DIR / req_path.name
        if result_path.exists():
            continue  # already has a result

        age = _file_age_hours(req_path)
        if age < max_age_hours:
            continue  # too recent

        stem = req_path.stem  # e.g. "pr-57-gemini_review"
        parts = stem.split("-", 2)
        gate_name = parts[2] if len(parts) == 3 else stem

        orphans.append({
            "request_path": req_path,
            "result_path": result_path,
            "gate_name": gate_name,
            "stem": stem,
            "age_hours": round(age, 1),
            "request_data": _read_request(req_path),
        })

    return orphans


def _write_abandoned_result(orphan: dict, dry_run: bool) -> bool:
    """Write an abandoned result file for an orphan gate request.

    Returns True on success (or dry-run), False on write failure.
    """
    result = {
        "timestamp": _now_utc(),
        "gate": orphan["gate_name"],
        "status": "abandoned",
        "reason": (
            f"Gate request had no result after {orphan['age_hours']:.1f}h. "
            "Marked abandoned by cleanup_orphan_gates.py."
        ),
        "source_request": orphan["stem"],
        "original_request": orphan["request_data"],
    }

    if dry_run:
        return True

    try:
        orphan["result_path"].parent.mkdir(parents=True, exist_ok=True)
        orphan["result_path"].write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        print(f"  [ERROR] Failed to write result for {orphan['stem']}: {exc}", file=sys.stderr)
        return False


def _extract_pr_number_from_stem(stem: str) -> "int | None":
    """Extract PR number from gate request stem like 'pr-57-gemini_review'."""
    import re
    m = re.match(r"^pr-(\d+)-", stem)
    return int(m.group(1)) if m else None


def _log_to_audit(orphans: list[dict]) -> None:
    """Append cleanup event to governance_audit.ndjson."""
    try:
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
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
    except Exception as exc:
        print(f"  [WARN] Could not write to governance_audit: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve orphan gate requests with no result (>24h old).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview orphans without writing result files.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Minimum age in hours to consider a gate request orphaned (default: 24).",
    )
    parser.add_argument(
        "--requests-dir",
        default=None,
        help="Override gate requests directory path.",
    )
    parser.add_argument(
        "--results-dir",
        default=None,
        help="Override gate results directory path.",
    )
    args = parser.parse_args(argv)

    global _GATE_REQUESTS_DIR, _GATE_RESULTS_DIR
    if args.requests_dir:
        _GATE_REQUESTS_DIR = Path(args.requests_dir)
    if args.results_dir:
        _GATE_RESULTS_DIR = Path(args.results_dir)

    orphans = _find_orphans(max_age_hours=args.max_age_hours)

    if not orphans:
        print(f"No orphan gate requests found (threshold: {args.max_age_hours:.0f}h).")
        return 0

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode_label}Found {len(orphans)} orphan gate request(s):\n")

    resolved = 0
    failed = 0
    for orphan in orphans:
        status = "would write" if args.dry_run else "writing"
        print(
            f"  {orphan['stem']}  age={orphan['age_hours']:.1f}h  "
            f"gate={orphan['gate_name']}  → {status} abandoned result"
        )
        ok = _write_abandoned_result(orphan, dry_run=args.dry_run)
        if ok:
            resolved += 1
        else:
            failed += 1

    if not args.dry_run and resolved > 0:
        _log_to_audit(orphans[:resolved])
        print(f"\nResolved {resolved} orphan(s). Logged to governance_audit.ndjson.")

    if failed:
        print(f"\n[WARN] {failed} write failure(s) — check stderr.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
