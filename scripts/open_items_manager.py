#!/usr/bin/env python3
"""
Open Items Manager - CLI for managing open items tracking
Maintains source of truth for blockers, warnings, and deferred items
"""

import json
import argparse
import fcntl
import os
import re
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Literal, Tuple
import sys

# Path configuration
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

_PATHS = ensure_env()
VNX_ROOT = Path(_PATHS["VNX_HOME"]).expanduser().resolve()
STATE_DIR = Path(_PATHS["VNX_STATE_DIR"]).expanduser().resolve()
LEGACY_STATE_DIR = (VNX_ROOT / "state").resolve()
OPEN_ITEMS_FILE = STATE_DIR / "open_items.json"
DIGEST_FILE = STATE_DIR / "open_items_digest.json"
MARKDOWN_FILE = STATE_DIR / "open_items.md"
AUDIT_LOG = STATE_DIR / "open_items_audit.jsonl"
LEGACY_OPEN_ITEMS_FILE = LEGACY_STATE_DIR / "open_items.json"
ROLLBACK_ENV_FLAG = "VNX_STATE_SIMPLIFICATION_ROLLBACK"

# Type definitions
SeverityLevel = Literal["blocker", "warn", "info"]
ItemStatus = Literal["open", "done", "deferred", "wontfix"]


def _env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rollback_mode_enabled() -> bool:
    rollback = _env_flag(ROLLBACK_ENV_FLAG)
    if rollback is None:
        rollback = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
    return bool(rollback)


def load_items() -> dict:
    """Load open items database"""
    source = OPEN_ITEMS_FILE
    if not source.exists() and _rollback_mode_enabled():
        source = LEGACY_OPEN_ITEMS_FILE
    if not source.exists():
        return {"schema_version": "1.0", "items": [], "next_id": 1}

    with open(source, 'r') as f:
        return json.load(f)

def save_items(data: dict):
    """Save open items database"""
    data["last_updated"] = datetime.now().isoformat()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(OPEN_ITEMS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def audit_log_entry(action: str, **kwargs):
    """Write audit log entry"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "actor": "T0",  # In practice, could be from env var
        "action": action,
        **kwargs
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')

def generate_item_id(data: dict) -> str:
    """Generate next item ID"""
    item_id = f"OI-{data['next_id']:03d}"
    data['next_id'] += 1
    return item_id

def _find_by_dedup_key(data: dict, key: str) -> Optional[dict]:
    """Scan all items for matching dedup_key (any status).

    Replay safety: a duplicate receipt must not recreate findings that were
    previously closed. Matching against any status (open/done/deferred/wontfix)
    keeps replays idempotent. Genuine regressions of a closed finding can be
    surfaced by manually reopening the existing item.
    """
    for item in data.get("items", []):
        if item.get("dedup_key") == key:
            return item
    return None


# Backwards-compatible alias kept for any external callers that imported the
# previous open-only helper. New code should call _find_by_dedup_key.
_find_open_by_dedup_key = _find_by_dedup_key


def add_item_programmatic(
    *,
    title: str,
    severity: SeverityLevel,
    dispatch_id: str,
    report_path: str = "",
    pr_id: str = "",
    details: str = "",
    dedup_key: str = "",
    source: str = "quality_advisory",
) -> Tuple[str, bool]:
    """Thread-safe programmatic API for adding open items with deduplication.

    Uses fcntl.flock on a dedicated lock file for concurrent terminal safety.

    Returns:
        (item_id, created): item_id is existing or new, created is False if deduplicated.
    """
    lock_path = STATE_DIR / "open_items.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            data = load_items()

            # Dedup check: if key matches an existing item (any status), skip.
            # Closed items are included so duplicate/replayed receipts cannot
            # recreate findings that were already closed.
            if dedup_key:
                existing = _find_by_dedup_key(data, dedup_key)
                if existing is not None:
                    return (existing["id"], False)

            item_id = generate_item_id(data)

            new_item = {
                "id": item_id,
                "status": "open",
                "severity": severity,
                "title": title,
                "details": details,
                "origin_dispatch_id": dispatch_id,
                "origin_report_path": report_path,
                "pr_id": pr_id,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "closed_reason": None,
                "source": source,
            }
            if dedup_key:
                new_item["dedup_key"] = dedup_key

            data["items"].append(new_item)
            save_items(data)

            audit_log_entry(
                "add",
                item_id=item_id,
                severity=severity,
                dispatch_id=dispatch_id,
                pr_id=pr_id,
                source=source,
                dedup_key=dedup_key,
            )

            generate_digest()

            return (item_id, True)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def add_item(args):
    """Add new open item"""
    data = load_items()

    item_id = generate_item_id(data)

    new_item = {
        "id": item_id,
        "status": "open",
        "severity": args.severity,
        "title": args.title,
        "details": args.details or "",
        "origin_dispatch_id": args.dispatch,
        "origin_report_path": args.report,
        "pr_id": args.pr,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "closed_reason": None
    }

    data["items"].append(new_item)
    save_items(data)

    audit_log_entry(
        "add",
        item_id=item_id,
        severity=args.severity,
        dispatch_id=args.dispatch,
        pr_id=args.pr
    )

    print(f"✅ Added {item_id}: {args.title}")
    print(f"   Severity: {args.severity}, PR: {args.pr or 'none'}")

    generate_digest()

def close_item(args):
    """Close an open item with specified status"""
    data = load_items()

    item = None
    for i in data["items"]:
        if i["id"] == args.item_id:
            item = i
            break

    if not item:
        print(f"❌ Item {args.item_id} not found")
        return 1

    if item["status"] != "open":
        print(f"⚠️  Item {args.item_id} already {item['status']}")
        return 1

    # Update item
    old_status = item["status"]
    item["status"] = args.status
    item["closed_reason"] = args.reason
    item["closed_by_dispatch_id"] = getattr(args, 'dispatch_id', None)
    item["closed_at"] = datetime.now().isoformat()
    item["updated_at"] = datetime.now().isoformat()

    save_items(data)

    audit_log_entry(
        "close",
        item_id=args.item_id,
        from_status=old_status,
        to_status=args.status,
        reason=args.reason,
        dispatch_id=item.get("origin_dispatch_id"),
        pr_id=item.get("pr_id")
    )

    _log_oi_close_decision(
        oi_id=args.item_id,
        status=args.status,
        reason=args.reason,
        dispatch_id=item.get("origin_dispatch_id"),
    )

    print(f"✅ Closed {args.item_id} as {args.status}")
    print(f"   Reason: {args.reason}")

    generate_digest()


def _log_oi_close_decision(*, oi_id: str, status: str, reason: str,
                           dispatch_id: Optional[str]) -> None:
    """Best-effort decision-log fan-out for OI closures.

    Failure here must never block close_item — the audit log is the
    primary source of truth, the decision log is a governance overlay.
    """
    try:
        from t0_decision_log import log_decision
    except Exception:
        return
    log_decision(
        decision_type="oi_closed",
        oi_id=oi_id,
        status=status,
        reasoning=reason or "",
        dispatch_id=dispatch_id,
    )

def list_items(args):
    """List open items with optional filtering"""
    data = load_items()

    # Filter by status if specified
    items = data["items"]
    if args.status:
        items = [i for i in items if i["status"] == args.status]

    if not items:
        print(f"No items with status: {args.status or 'any'}")
        return

    # Group by status
    by_status = {}
    for item in items:
        status = item["status"]
        if status not in by_status:
            by_status[status] = []
        by_status[status].append(item)

    # Display
    for status in ["open", "done", "deferred", "wontfix"]:
        if status not in by_status:
            continue

        print(f"\n{status.upper()} ({len(by_status[status])} items):")
        print("-" * 60)

        # Sort by severity for open items
        status_items = by_status[status]
        if status == "open":
            severity_order = {"blocker": 0, "warn": 1, "info": 2}
            status_items.sort(key=lambda x: severity_order.get(x["severity"], 999))

        for item in status_items:
            severity_icon = {
                "blocker": "🔴",
                "warn": "🟡",
                "info": "🔵"
            }.get(item["severity"], "⚪")

            print(f"{severity_icon} {item['id']}: {item['title']}")
            if item.get("pr_id"):
                print(f"     PR: {item['pr_id']}")
            if item["status"] != "open" and item.get("closed_reason"):
                print(f"     Closed: {item['closed_reason']}")

def attach_evidence(args):
    """Attach evidence from a report to all open items for a PR (does NOT close them)."""
    data = load_items()

    pr_id = args.pr
    report_path = args.report or ""
    dispatch_id = args.dispatch or ""

    matched = 0
    for item in data["items"]:
        if item["status"] != "open":
            continue
        if item.get("pr_id") != pr_id:
            continue

        # Append evidence entry
        if "evidence" not in item:
            item["evidence"] = []
        item["evidence"].append({
            "report_path": report_path,
            "dispatch_id": dispatch_id,
            "attached_at": datetime.now().isoformat()
        })
        item["updated_at"] = datetime.now().isoformat()
        matched += 1

    if matched == 0:
        print(f"ℹ️  No open items found for {pr_id}")
        return 0

    save_items(data)

    audit_log_entry(
        "attach_evidence",
        pr_id=pr_id,
        report_path=report_path,
        dispatch_id=dispatch_id,
        items_matched=matched
    )

    print(f"📎 Attached evidence to {matched} open items for {pr_id}")
    print(f"   Report: {report_path}")
    print(f"   T0 must review and close items manually")

    generate_digest()
    return 0


def generate_digest():
    """Generate digest and markdown view"""
    data = load_items()

    # Calculate summary
    summary = {
        "open_count": 0,
        "blocker_count": 0,
        "warn_count": 0,
        "info_count": 0,
        "done_count": 0,
        "deferred_count": 0,
        "wontfix_count": 0
    }

    top_blockers = []
    top_warnings = []
    recent_closures = []

    for item in data["items"]:
        if item["status"] == "open":
            summary["open_count"] += 1
            if item["severity"] == "blocker":
                summary["blocker_count"] += 1
                top_blockers.append({
                    "id": item["id"],
                    "title": item["title"],
                    "pr_id": item.get("pr_id")
                })
            elif item["severity"] == "warn":
                summary["warn_count"] += 1
                top_warnings.append({
                    "id": item["id"],
                    "title": item["title"],
                    "pr_id": item.get("pr_id")
                })
            elif item["severity"] == "info":
                summary["info_count"] += 1
        elif item["status"] == "done":
            summary["done_count"] += 1
            recent_closures.append(item)
        elif item["status"] == "deferred":
            summary["deferred_count"] += 1
        elif item["status"] == "wontfix":
            summary["wontfix_count"] += 1

    # Limit top items (token efficiency: show only top 2)
    top_blockers = top_blockers[:3]
    top_warnings = top_warnings[:2]
    recent_closures = sorted(recent_closures, key=lambda x: x["updated_at"], reverse=True)[:5]

    # Save digest
    open_items = [
        {
            "id": item["id"],
            "severity": item["severity"],
            "title": item["title"],
            "pr_id": item.get("pr_id"),
        }
        for item in data["items"]
        if item["status"] == "open"
    ]

    digest = {
        "summary": summary,
        "top_blockers": top_blockers,
        "top_warnings": top_warnings,
        "open_items": open_items,
        "recent_closures": [
            {
                "id": i["id"],
                "title": i["title"],
                "closed_reason": i.get("closed_reason"),
                "closed_at": i.get("updated_at"),
            }
            for i in recent_closures
        ],
        "last_updated": data.get("last_updated"),
        "digest_generated": datetime.now().isoformat()
    }

    with open(DIGEST_FILE, 'w') as f:
        json.dump(digest, f, indent=2)

    # Generate markdown
    generate_markdown(data, digest)

    print(f"📊 Digest updated: {summary['open_count']} open ({summary['blocker_count']} blockers)")

def generate_markdown(data: dict, digest: dict):
    """Generate human-readable markdown view"""
    lines = []

    lines.append("# Open Items Tracker")
    lines.append("")
    lines.append("⚠️ **DO NOT EDIT** - This file is auto-generated from `open_items.json`")
    lines.append("")

    # Summary
    s = digest["summary"]
    lines.append("## Summary")
    lines.append(f"- **Open**: {s['open_count']} items ({s['blocker_count']} blockers, {s['warn_count']} warnings, {s['info_count']} info)")
    lines.append(f"- **Closed**: {s['done_count']} done, {s['deferred_count']} deferred, {s['wontfix_count']} wontfix")
    lines.append(f"- **Last Updated**: {digest['digest_generated'][:19]}")
    lines.append("")

    # Active items
    lines.append("## Active Items")
    lines.append("")

    open_items = [i for i in data["items"] if i["status"] == "open"]
    if not open_items:
        lines.append("*No open items*")
    else:
        # Group by severity
        for severity in ["blocker", "warn", "info"]:
            severity_items = [i for i in open_items if i["severity"] == severity]
            if severity_items:
                lines.append(f"### {severity.upper()}S")
                for item in severity_items:
                    pr = f" (PR: {item['pr_id']})" if item.get('pr_id') else ""
                    lines.append(f"- **{item['id']}**: {item['title']}{pr}")
                    if item.get("details"):
                        lines.append(f"  - {item['details']}")
                lines.append("")

    lines.append("")

    # Recently closed
    lines.append("## Recently Closed")
    lines.append("")

    if not digest["recent_closures"]:
        lines.append("*No recently closed items*")
    else:
        for item in digest["recent_closures"]:
            lines.append(f"- **{item['id']}**: {item['title']}")
            if item.get("closed_reason"):
                lines.append(f"  - Reason: {item['closed_reason']}")

    lines.append("")
    lines.append("---")
    lines.append("Generated automatically by `open_items_manager.py`")

    with open(MARKDOWN_FILE, 'w') as f:
        f.write('\n'.join(lines))

# ---------------------------------------------------------------------------
# OI Auto-Close: rescan patterns
# ---------------------------------------------------------------------------

FILE_SIZE_PATTERN = re.compile(
    r'file\s+(.+?)\s+exceeds\s+(\d+)\s*L?(?:ines)?',
    re.IGNORECASE,
)

FUNC_SIZE_PATTERN = re.compile(
    r'function\s+(\S+?)(?:\s+in\s+(.+?))?\s+exceeds\s+(\d+)\s*L?(?:ines)?',
    re.IGNORECASE,
)


def check_violation(item: dict) -> Optional[dict]:
    """Check if an OI's underlying violation still exists.

    Returns None if the item doesn't match a rescannable pattern.
    Returns {"resolved": bool, "reason": str} otherwise.
    """
    title = item.get("title", "")

    # Category 1: file size
    m = FILE_SIZE_PATTERN.search(title)
    if m:
        return _check_file_size(m.group(1).strip(), int(m.group(2)))

    # Category 2: function size
    m = FUNC_SIZE_PATTERN.search(title)
    if m:
        func_name = m.group(1).strip()
        file_path = (m.group(2) or "").strip()
        threshold = int(m.group(3))
        return _check_function_size(func_name, file_path, threshold)

    return None


def _check_file_size(file_path: str, threshold: int) -> dict:
    """Check if a file still exceeds the line threshold."""
    resolved_path = (VNX_ROOT / file_path) if not os.path.isabs(file_path) else Path(file_path)
    if not resolved_path.exists():
        return {"resolved": True, "reason": "auto-resolved: file no longer exists"}

    try:
        line_count = sum(1 for _ in resolved_path.open())
    except OSError:
        return {"resolved": False, "reason": "unable to read file"}

    if line_count <= threshold:
        return {"resolved": True, "reason": f"auto-resolved: actual {line_count}L, threshold {threshold}L"}
    return {"resolved": False, "reason": f"still exceeds: actual {line_count}L, threshold {threshold}L"}


def _check_function_size(func_name: str, file_path: str, threshold: int) -> dict:
    """Check if a function still exceeds the line threshold."""
    if not file_path:
        return {"resolved": False, "reason": "no file path in OI title, cannot verify"}

    resolved_path = (VNX_ROOT / file_path) if not os.path.isabs(file_path) else Path(file_path)
    if not resolved_path.exists():
        return {"resolved": True, "reason": "auto-resolved: file no longer exists"}

    try:
        lines = resolved_path.read_text().splitlines()
    except OSError:
        return {"resolved": False, "reason": "unable to read file"}

    func_start = None
    indent = None
    for i, line in enumerate(lines):
        if re.match(rf'^(\s*)def\s+{re.escape(func_name)}\s*\(', line):
            func_start = i
            indent = len(line) - len(line.lstrip())
            break
        if re.match(rf'^(\s*){re.escape(func_name)}\s*\(\)', line):
            func_start = i
            indent = len(line) - len(line.lstrip())
            break

    if func_start is None:
        return {"resolved": True, "reason": f"auto-resolved: function {func_name} no longer exists in {file_path}"}

    func_end = len(lines)
    for i in range(func_start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith('#'):
            continue
        current_indent = len(lines[i]) - len(stripped)
        if current_indent <= indent and (stripped.startswith('def ') or stripped.startswith('class ')):
            func_end = i
            break

    func_length = func_end - func_start
    if func_length <= threshold:
        return {"resolved": True, "reason": f"auto-resolved: function {func_name} actual {func_length}L, threshold {threshold}L"}
    return {"resolved": False, "reason": f"still exceeds: function {func_name} actual {func_length}L, threshold {threshold}L"}


def rescan_items(args):
    """Rescan open items and auto-close resolved violations."""
    data = load_items()
    dry_run = getattr(args, 'dry_run', False)
    closed = []

    for item in data["items"]:
        if item["status"] != "open":
            continue

        result = check_violation(item)
        if result is None:
            continue
        if result["resolved"]:
            if not dry_run:
                item["status"] = "done"
                item["closed_reason"] = result["reason"]
                item["closed_at"] = datetime.now().isoformat()
                item["updated_at"] = datetime.now().isoformat()
                item["closed_by"] = "auto-rescan"
            closed.append({
                "id": item["id"],
                "title": item["title"],
                "reason": result["reason"],
            })

    if not dry_run and closed:
        save_items(data)
        for c in closed:
            audit_log_entry(
                "auto_close",
                item_id=c["id"],
                reason=c["reason"],
                source="rescan",
            )

    prefix = "[DRY RUN] " if dry_run else ""
    verb = "would be " if dry_run else ""
    print(f"{prefix}Rescan complete: {len(closed)} items {verb}closed")
    for c in closed:
        print(f"  {c['id']}: {c['reason']}")

    if not dry_run:
        generate_digest()


def count_items_closed_by_dispatch(dispatch_id: str) -> int:
    """Count items where closed_by_dispatch_id matches."""
    data = load_items()
    return sum(
        1 for item in data["items"]
        if item.get("closed_by_dispatch_id") == dispatch_id
    )


def main():
    if _rollback_mode_enabled():
        print(
            "[CUTOVER] WARNING: rollback mode enabled "
            f"({ROLLBACK_ENV_FLAG}=1). Legacy open-items fallback reads are active."
        )

    parser = argparse.ArgumentParser(description="Open Items Manager")
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # List command
    list_parser = subparsers.add_parser('list', help='List open items')
    list_parser.add_argument('--status', choices=['open', 'done', 'deferred', 'wontfix'],
                            help='Filter by status')

    # Add command
    add_parser = subparsers.add_parser('add', help='Add new open item')
    add_parser.add_argument('--dispatch', required=True, help='Origin dispatch ID')
    add_parser.add_argument('--title', required=True, help='Item title')
    add_parser.add_argument('--severity', choices=['blocker', 'warn', 'info'],
                           default='info', help='Severity level')
    add_parser.add_argument('--pr', help='Associated PR ID')
    add_parser.add_argument('--report', help='Origin report path')
    add_parser.add_argument('--details', help='Additional details')

    # Close command
    close_parser = subparsers.add_parser('close', help='Close item as done')
    close_parser.add_argument('item_id', help='Item ID to close')
    close_parser.add_argument('--status', default='done',
                             choices=['done'], help='Close status')
    close_parser.add_argument('--reason', required=True, help='Closure reason')
    close_parser.add_argument('--dispatch-id', dest='dispatch_id', default=None, help='Dispatch ID that resolved this item')

    # Defer command
    defer_parser = subparsers.add_parser('defer', help='Defer item')
    defer_parser.add_argument('item_id', help='Item ID to defer')
    defer_parser.add_argument('--reason', required=True, help='Deferral reason')
    defer_parser.add_argument('--dispatch-id', dest='dispatch_id', default=None, help='Dispatch ID that deferred this item')

    # Wontfix command
    wontfix_parser = subparsers.add_parser('wontfix', help='Mark as wontfix')
    wontfix_parser.add_argument('item_id', help='Item ID to mark wontfix')
    wontfix_parser.add_argument('--reason', required=True, help='Wontfix reason')
    wontfix_parser.add_argument('--dispatch-id', dest='dispatch_id', default=None, help='Dispatch ID that marked this wontfix')

    # Attach evidence command
    evidence_parser = subparsers.add_parser('attach-evidence', help='Attach report evidence to PR open items')
    evidence_parser.add_argument('--pr', required=True, help='PR ID to attach evidence to')
    evidence_parser.add_argument('--report', help='Path to the completion report')
    evidence_parser.add_argument('--dispatch', help='Dispatch ID that generated the report')

    # Digest command
    digest_parser = subparsers.add_parser('digest', help='Generate digest')
    digest_parser.add_argument('--last', type=int, default=20,
                              help='Include last N closed items')
    digest_parser.add_argument('--rescan', action='store_true',
                              help='Run auto-close rescan before generating digest')

    # Rescan command
    rescan_parser = subparsers.add_parser('rescan', help='Rescan open items and auto-close resolved violations')
    rescan_parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                              help='Show what would be closed without closing')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Route commands
    if args.command == 'list':
        list_items(args)
    elif args.command == 'add':
        add_item(args)
    elif args.command == 'close':
        close_item(args)
    elif args.command == 'defer':
        args.status = 'deferred'
        close_item(args)
    elif args.command == 'wontfix':
        args.status = 'wontfix'
        close_item(args)
    elif args.command == 'attach-evidence':
        attach_evidence(args)
    elif args.command == 'digest':
        if getattr(args, 'rescan', False):
            rescan_items(args)
        generate_digest()
    elif args.command == 'rescan':
        rescan_items(args)

    return 0

if __name__ == "__main__":
    sys.exit(main())
