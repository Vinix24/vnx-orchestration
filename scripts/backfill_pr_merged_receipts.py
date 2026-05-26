#!/usr/bin/env python3
"""Backfill pr_merged receipts for GitHub PRs that have been merged without an audit trail.

Queries GitHub for all merged PRs in the current repository, compares against
t0_receipts.ndjson, and emits a pr_merged receipt for any PR that is missing one.

Usage:
    python3 scripts/backfill_pr_merged_receipts.py          # live backfill
    python3 scripts/backfill_pr_merged_receipts.py --dry-run  # preview, no writes
    python3 scripts/backfill_pr_merged_receipts.py --limit 50  # cap at 50 PRs
    python3 scripts/backfill_pr_merged_receipts.py --since 2026-01-01  # only newer PRs
    python3 scripts/backfill_pr_merged_receipts.py --json   # structured output

Idempotent: already-receipted PR numbers are skipped.

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt

EXIT_OK = 0
EXIT_ERROR = 1


def _resolve_receipts_path() -> Path:
    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _load_receipted_pr_numbers(receipts_path: Path) -> Set[int]:
    """Return set of pr_number values already in t0_receipts.ndjson with event_type=pr_merged."""
    if not receipts_path.exists():
        return set()
    result: Set[int] = set()
    for line in receipts_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = rec.get("event_type") or rec.get("event") or ""
        if event == "pr_merged":
            pn = rec.get("pr_number")
            if pn is not None:
                try:
                    result.add(int(pn))
                except (TypeError, ValueError):
                    pass
    return result


def _gh_list_merged_prs(limit: int, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return list of merged PRs from GitHub via gh CLI."""
    args = [
        "gh", "pr", "list",
        "--state", "merged",
        "--limit", str(max(limit, 1)),
        "--json", "number,title,headRefName,mergedAt,baseRefName",
    ]
    if since:
        # gh does not support --search date filtering directly, handled post-fetch
        pass
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            print(f"ERROR: gh pr list failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        return json.loads(result.stdout or "[]")
    except Exception as exc:
        print(f"ERROR: gh pr list raised: {exc}", file=sys.stderr)
        return []


def _emit_backfill_receipt(
    pr: Dict[str, Any],
    *,
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Emit a pr_merged receipt for a single PR (backfill mode)."""
    pr_number = int(pr["number"])
    kwargs: Dict[str, Any] = {
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": "unknown",
        "pr_title": pr.get("title", ""),
        "branch": pr.get("headRefName", ""),
        "merged_at": pr.get("mergedAt", ""),
        "backfilled": True,
    }
    return emit_governance_receipt(
        "pr_merged",
        status="success",
        terminal="T0",
        source="backfill_pr_merged_receipts",
        receipts_file=receipts_file,
        **kwargs,
    )


def _emit_register_event(pr_number: int) -> bool:
    """Write pr_merged event to dispatch_register.ndjson. Best-effort."""
    try:
        from dispatch_register import append_event
        return append_event(
            "pr_merged",
            pr_number=pr_number,
            terminal="T0",
            extra={"backfilled": True, "conclusion": "merged"},
        )
    except Exception:
        return False


def backfill(
    *,
    limit: int = 500,
    since: Optional[str] = None,
    dry_run: bool = False,
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the backfill and return a summary dict."""
    receipts_path = Path(receipts_file) if receipts_file else _resolve_receipts_path()
    already_receipted = _load_receipted_pr_numbers(receipts_path)

    merged_prs = _gh_list_merged_prs(limit=limit, since=since)

    # Filter by --since date when provided (gh does not support this flag natively)
    if since:
        filtered = []
        for pr in merged_prs:
            merged_at = pr.get("mergedAt", "")
            if merged_at and merged_at[:10] >= since[:10]:
                filtered.append(pr)
        merged_prs = filtered

    missing: List[Dict[str, Any]] = [
        pr for pr in merged_prs
        if pr.get("number") is not None and int(pr["number"]) not in already_receipted
    ]

    summary: Dict[str, Any] = {
        "total_merged_prs": len(merged_prs),
        "already_receipted": len(already_receipted),
        "missing_receipt_count": len(missing),
        "backfilled": 0,
        "errors": 0,
        "dry_run": dry_run,
        "details": [],
    }

    for pr in missing:
        pr_number = int(pr["number"])
        detail: Dict[str, Any] = {
            "pr_number": pr_number,
            "pr_title": pr.get("title", ""),
            "merged_at": pr.get("mergedAt", ""),
        }

        if dry_run:
            detail["action"] = "would_backfill"
        else:
            try:
                receipt = _emit_backfill_receipt(pr, receipts_file=str(receipts_path))
                detail["action"] = "backfilled"
                detail["receipt_status"] = receipt.get("append_status", "unknown")
                detail["register_ok"] = _emit_register_event(pr_number)
                summary["backfilled"] += 1
            except Exception as exc:
                detail["action"] = "error"
                detail["error"] = str(exc)
                summary["errors"] += 1

        summary["details"].append(detail)

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill pr_merged receipts for already-merged PRs",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--limit", type=int, default=500, help="Max merged PRs to fetch (default 500)")
    parser.add_argument("--since", default=None, help="Only PRs merged on/after this date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    args = parser.parse_args(argv)

    summary = backfill(
        limit=args.limit,
        since=args.since,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(summary, indent=2))
        return EXIT_OK

    dry_label = " [dry-run]" if args.dry_run else ""
    print(f"Backfill pr_merged receipts{dry_label}")
    print(f"  merged PRs found    : {summary['total_merged_prs']}")
    print(f"  already receipted   : {summary['already_receipted']}")
    print(f"  missing receipt     : {summary['missing_receipt_count']}")
    if args.dry_run:
        print(f"  would backfill      : {summary['missing_receipt_count']}")
    else:
        print(f"  backfilled          : {summary['backfilled']}")
        if summary["errors"]:
            print(f"  errors              : {summary['errors']}")

    if summary["details"] and not args.json:
        print()
        for d in summary["details"][:20]:
            action = d.get("action", "?")
            print(f"  PR #{d['pr_number']:>4}  {action:<14}  {d.get('pr_title','')[:60]}")
        if len(summary["details"]) > 20:
            print(f"  ... and {len(summary['details']) - 20} more")

    return EXIT_OK if summary["errors"] == 0 else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
