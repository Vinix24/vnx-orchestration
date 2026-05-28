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
import datetime
import fcntl
import json
import re
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

_PR_LABEL_RE = re.compile(r"\bPR-([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", re.IGNORECASE)


def _extract_pr_id_from_subject(subject: str) -> Optional[str]:
    """Extract internal PR-N label from commit subject or PR title."""
    m = _PR_LABEL_RE.search(subject)
    return f"PR-{m.group(1).upper()}" if m else None


def _lookup_dispatch_id_for_pr(
    pr_number: int,
    branch: str,
    register_events: List[Dict[str, Any]],
) -> str:
    """Find dispatch_id from dispatch_register events by pr_number or branch-slug match.

    Priority: pr_number exact match > branch-slug heuristic > '' (not found).
    """
    # 1. Exact pr_number match
    for ev in reversed(register_events):
        if ev.get("pr_number") == pr_number and ev.get("dispatch_id"):
            return str(ev["dispatch_id"])

    # 2. Branch-slug heuristic: tokenise branch and match against dispatch_id strings
    if branch:
        branch_raw = branch.lower().replace("/", "-").replace("_", "-")
        tokens = [t for t in re.split(r"[-]+", branch_raw) if len(t) >= 4]
        if tokens:
            for ev in reversed(register_events):
                did = str(ev.get("dispatch_id") or "")
                if not did:
                    continue
                did_lower = did.lower()
                matches = sum(1 for tok in tokens if tok in did_lower)
                if matches >= max(1, len(tokens) // 2):
                    return did

    return ""


def _resolve_receipts_path() -> Path:
    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _resolve_events_pr_merged_path() -> Path:
    """Return path for ADR-005 events ledger: .vnx-data/events/pr_merged.ndjson."""
    paths = ensure_env()
    events_dir = Path(paths["VNX_STATE_DIR"]).parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return events_dir / "pr_merged.ndjson"


def _append_locked(path: Path, record: Dict[str, Any]) -> None:
    """Append a JSON record to an NDJSON file under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_receipted_pr_numbers(
    receipts_path: Path,
    events_path: Optional[Path] = None,
) -> Set[int]:
    """Return set of pr_number values already receipted as pr_merged.

    Scans both t0_receipts.ndjson and events/pr_merged.ndjson (ADR-005 ledger)
    so idempotency works regardless of which path prior runs wrote to.

    events_path: explicit override. When None, derived as receipts_path/../events/pr_merged.ndjson
    (no env-var lookup, so test isolation is preserved).
    """
    result: Set[int] = set()
    paths_to_scan: List[Path] = [receipts_path]

    # Derive events path from receipts_path structure (state/ → events/pr_merged.ndjson)
    if events_path is None:
        candidate = receipts_path.parent.parent / "events" / "pr_merged.ndjson"
        if candidate.resolve() != receipts_path.resolve():
            events_path = candidate

    if events_path is not None and events_path not in paths_to_scan:
        paths_to_scan.append(events_path)

    for path in paths_to_scan:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
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
        "--json", "number,title,headRefName,mergedAt,baseRefName,mergeCommit",
    ]
    # gh does not support --search date filtering directly, handled post-fetch
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


def _load_dispatch_register_events() -> List[Dict[str, Any]]:
    """Load dispatch_register.ndjson events. Best-effort — returns [] on failure."""
    try:
        from dispatch_register import read_events
        return read_events()
    except Exception:
        return []


def _emit_backfill_receipt(
    pr: Dict[str, Any],
    *,
    pr_id: str = "",
    dispatch_id: str = "",
    events_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Emit a pr_merged receipt for a single PR to events/pr_merged.ndjson (ADR-005).

    Dual-scheme: pr_number (GitHub numeric) + pr_id (internal PR-N/PR-LABEL).
    pr_id_resolution='unmatched' when no internal label could be derived.
    Uses the merge commit timestamp for accurate historical placement.
    """
    pr_number = int(pr["number"])
    merged_at = pr.get("mergedAt", "")

    # Use merge commit timestamp if available for accurate historical placement
    merge_commit = pr.get("mergeCommit") or {}
    commit_ts = str(merge_commit.get("committedDate") or merge_commit.get("authoredDate") or merged_at)

    receipt: Dict[str, Any] = {
        "timestamp": commit_ts or merged_at or datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "event_type": "pr_merged",
        "status": "success",
        "terminal": "T0",
        "source": "backfill_pr_merged_receipts",
        "pr_number": pr_number,
        "conclusion": "merged",
        "merge_method": "unknown",
        "pr_title": pr.get("title", ""),
        "branch": pr.get("headRefName", ""),
        "merged_at": merged_at,
        "backfilled": True,
    }
    if pr_id:
        receipt["pr_id"] = pr_id
    else:
        receipt["pr_id_resolution"] = "unmatched"
    if dispatch_id:
        receipt["dispatch_id"] = dispatch_id

    target_path = events_path if events_path is not None else _resolve_events_pr_merged_path()
    _append_locked(target_path, receipt)
    return receipt


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
    events_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the backfill and return a summary dict.

    Writes new pr_merged receipts to events/pr_merged.ndjson (ADR-005 ledger).
    The receipts_file parameter is kept for backwards compatibility but is only
    used to seed the already-receipted set (idempotency check).
    events_path overrides the default events/pr_merged.ndjson destination.
    """
    receipts_path = Path(receipts_file) if receipts_file else _resolve_receipts_path()
    resolved_events_path = events_path if events_path is not None else _resolve_events_pr_merged_path()
    already_receipted = _load_receipted_pr_numbers(receipts_path, events_path=resolved_events_path)

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

    # Load dispatch_register events once for dispatch_id lookup
    register_events = _load_dispatch_register_events()

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

        # Derive dual-scheme fields
        subject = pr.get("title", "") or pr.get("headRefName", "")
        pr_id = _extract_pr_id_from_subject(subject)
        dispatch_id = _lookup_dispatch_id_for_pr(pr_number, pr.get("headRefName", ""), register_events)
        detail["pr_id"] = pr_id or ""
        detail["dispatch_id"] = dispatch_id

        if dry_run:
            detail["action"] = "would_backfill"
        else:
            try:
                receipt = _emit_backfill_receipt(
                    pr,
                    pr_id=pr_id or "",
                    dispatch_id=dispatch_id,
                    events_path=events_path,
                )
                detail["action"] = "backfilled"
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
