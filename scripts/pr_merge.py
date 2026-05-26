#!/usr/bin/env python3
"""VNX PR merge: merge a PR and emit pr_merged receipt + dispatch register event.

T0 calls this instead of raw ``gh pr merge`` so every merge is captured in the
audit trail (t0_receipts.ndjson + dispatch_register.ndjson).  Without this,
FPY/rework-rate/history have no linkage between merged PRs and receipts.

Usage:
    python3 scripts/pr_merge.py --pr 123
    python3 scripts/pr_merge.py --pr 123 --dispatch-id 20260526-gov2-something
    python3 scripts/pr_merge.py --pr 123 --squash          # default merge strategy
    python3 scripts/pr_merge.py --pr 123 --rebase
    python3 scripts/pr_merge.py --pr 123 --merge
    python3 scripts/pr_merge.py --pr 123 --dry-run         # no merge, no write

Receipt written to t0_receipts.ndjson:
    event_type  : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    conclusion  : "merged"
    merge_method: "squash" | "merge" | "rebase"
    pr_title    : <from gh api>
    branch      : <from gh api>

Register event written to dispatch_register.ndjson:
    event       : "pr_merged"
    pr_number   : <int>
    dispatch_id : <str, optional>
    terminal    : "T0"

BILLING SAFETY: No Anthropic SDK. No direct API calls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt

EXIT_OK = 0
EXIT_ERROR = 1


def _gh(args: list[str], *, check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a gh command and return the CompletedProcess."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _query_pr(pr_number: int) -> Optional[Dict[str, Any]]:
    """Return PR metadata from GitHub, or None on failure."""
    result = _gh([
        "pr", "view", str(pr_number),
        "--json", "number,title,state,headRefName,baseRefName,mergedAt,mergeCommit",
    ])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _do_merge(pr_number: int, method: str) -> tuple[bool, str]:
    """Execute gh pr merge and return (success, error_message)."""
    method_flag = f"--{method}"
    result = _gh([
        "pr", "merge", str(pr_number),
        method_flag, "--auto",
    ])
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "gh pr merge failed").strip()
        return False, msg
    return True, ""


def _emit_receipt(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
    pr_title: str,
    branch: str,
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Write pr_merged receipt to t0_receipts.ndjson."""
    kwargs: Dict[str, Any] = {
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": merge_method,
        "pr_title": pr_title,
        "branch": branch,
    }
    if dispatch_id:
        kwargs["dispatch_id"] = dispatch_id
    return emit_governance_receipt(
        "pr_merged",
        status="success",
        terminal="T0",
        source="pr_merge",
        receipts_file=receipts_file,
        **kwargs,
    )


def _emit_register_event(
    *,
    pr_number: int,
    dispatch_id: str,
    merge_method: str,
) -> bool:
    """Write pr_merged event to dispatch_register.ndjson. Best-effort, never raises."""
    try:
        from dispatch_register import append_event
        return append_event(
            "pr_merged",
            pr_number=pr_number,
            dispatch_id=dispatch_id or "",
            terminal="T0",
            extra={"merge_method": merge_method, "conclusion": "merged"},
        )
    except Exception:
        return False


def merge_pr(
    pr_number: int,
    *,
    dispatch_id: str = "",
    merge_method: str = "squash",
    dry_run: bool = False,
    receipts_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge a PR and emit audit trail.

    Returns a dict with keys: success, pr_number, dispatch_id, merge_method,
    pr_title, branch, receipt_status, register_ok, error.
    """
    result: Dict[str, Any] = {
        "success": False,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "merge_method": merge_method,
        "pr_title": "",
        "branch": "",
        "receipt_status": None,
        "register_ok": False,
        "error": "",
        "dry_run": dry_run,
    }

    # Query PR metadata before merge (needed for receipt)
    pr_data = _query_pr(pr_number)
    if pr_data:
        result["pr_title"] = pr_data.get("title", "")
        result["branch"] = pr_data.get("headRefName", "")

    if dry_run:
        result["success"] = True
        result["error"] = "dry_run: no merge executed"
        print(f"[dry-run] Would merge PR #{pr_number} via {merge_method}")
        if dispatch_id:
            print(f"[dry-run] dispatch_id: {dispatch_id}")
        return result

    # Execute the merge
    ok, err = _do_merge(pr_number, merge_method)
    if not ok:
        result["error"] = err
        print(f"ERROR: gh pr merge failed for #{pr_number}: {err}", file=sys.stderr)
        return result

    result["success"] = True

    # Emit receipt to t0_receipts.ndjson
    try:
        receipt = _emit_receipt(
            pr_number=pr_number,
            dispatch_id=dispatch_id,
            merge_method=merge_method,
            pr_title=result["pr_title"],
            branch=result["branch"],
            receipts_file=receipts_file,
        )
        result["receipt_status"] = receipt.get("append_status", "unknown")
    except Exception as exc:
        result["receipt_status"] = f"error: {exc}"
        print(f"WARN: receipt emit failed for PR #{pr_number}: {exc}", file=sys.stderr)

    # Emit event to dispatch_register.ndjson (best-effort)
    result["register_ok"] = _emit_register_event(
        pr_number=pr_number,
        dispatch_id=dispatch_id,
        merge_method=merge_method,
    )

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge a PR and emit pr_merged receipt + register event",
    )
    parser.add_argument("--pr", type=int, required=True, help="GitHub PR number")
    parser.add_argument(
        "--dispatch-id", default="",
        help="Dispatch-ID to link this merge to a receipt chain",
    )
    merge_group = parser.add_mutually_exclusive_group()
    merge_group.add_argument("--squash", dest="merge_method", action="store_const", const="squash", default=None)
    merge_group.add_argument("--rebase", dest="merge_method", action="store_const", const="rebase")
    merge_group.add_argument("--merge", dest="merge_method", action="store_const", const="merge")
    parser.add_argument("--dry-run", action="store_true", help="Skip merge and receipt write")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args(argv)

    method = args.merge_method or "squash"

    result = merge_pr(
        pr_number=args.pr,
        dispatch_id=args.dispatch_id or "",
        merge_method=method,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["success"]:
        dry_label = " (dry-run)" if args.dry_run else ""
        print(f"OK: PR #{args.pr} merged{dry_label} via {method}")
        if result.get("receipt_status") and not args.dry_run:
            print(f"    receipt: {result['receipt_status']}")
            print(f"    register: {'ok' if result['register_ok'] else 'warn-not-written'}")
    else:
        print(f"ERROR: {result['error']}", file=sys.stderr)

    return EXIT_OK if result["success"] else EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
