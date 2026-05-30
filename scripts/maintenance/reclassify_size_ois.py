#!/usr/bin/env python3
"""reclassify_size_ois.py — Backfill: downgrade size-blocker OIs to warn.

Size findings (file_size_blocking, function_size_blocking) were emitted as
severity="blocker" before the policy change in quality_advisory.py. These are
advisory/tech-debt findings, not correctness blockers. This script reclassifies
all open size-blocker OIs to severity="warn" with an audit-trail entry.

Idempotent — re-running is safe; already-reclassified items (severity != "blocker")
are skipped.

Usage:
    python3 scripts/maintenance/reclassify_size_ois.py          # dry-run (default)
    python3 scripts/maintenance/reclassify_size_ois.py --apply  # write changes
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    _PATHS = ensure_env()
    STATE_DIR = Path(_PATHS["VNX_STATE_DIR"]).expanduser().resolve()
except Exception:
    # Fallback: derive from repo root (used in tests / offline runs)
    STATE_DIR = REPO_ROOT / ".vnx-data" / "state"

OPEN_ITEMS_FILE = STATE_DIR / "open_items.json"
AUDIT_LOG = STATE_DIR / "open_items_audit.jsonl"

# Match dedup keys produced by qa:{check_id}:{file}:{symbol}
DEDUP_KEY_RX = re.compile(r"^qa:(file_size_blocking|function_size_blocking):")

# Match titles from the old emitted messages
TITLE_SIZE_RX = re.compile(
    r"(File exceeds blocking threshold|Function exceeds blocking threshold"
    r"|File is large|Function is large)",
    re.IGNORECASE,
)


def _is_size_blocker(item: dict) -> bool:
    if item.get("status") != "open":
        return False
    if item.get("severity") != "blocker":
        return False
    dedup = item.get("dedup_key", "")
    if DEDUP_KEY_RX.match(dedup):
        return True
    title = item.get("title", "")
    return bool(TITLE_SIZE_RX.search(title))


def load_items() -> dict:
    if not OPEN_ITEMS_FILE.exists():
        return {"schema_version": "1.0", "items": [], "next_id": 1}
    return json.loads(OPEN_ITEMS_FILE.read_text(encoding="utf-8"))


def save_items(data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = OPEN_ITEMS_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp_path, OPEN_ITEMS_FILE)


def append_audit(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry) + "\n"
    with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is dry-run; no files are modified without this flag)",
    )
    args = parser.parse_args()

    if not OPEN_ITEMS_FILE.exists():
        print(f"Open-items store not found: {OPEN_ITEMS_FILE}")
        print("Nothing to reclassify.")
        return 0

    data = load_items()
    items = data.get("items", data) if isinstance(data, dict) else data
    if isinstance(data, list):
        # Bare list format (legacy)
        items = data
        data = {"items": items}

    candidates = [it for it in items if _is_size_blocker(it)]

    print(f"Open-items store: {OPEN_ITEMS_FILE}")
    print(f"Total items: {len(items)}")
    print(f"Size-blocker OIs to reclassify: {len(candidates)}\n")

    if not candidates:
        print("No size-blocker OIs found. Store is already clean.")
        return 0

    for it in candidates:
        tag = f"{it['id']} | {it.get('dedup_key', '')[:60] or it.get('title', '')[:60]}"
        print(f"  {'[DRY-RUN] would reclassify' if not args.apply else 'Reclassifying'}: {tag}")

    if not args.apply:
        print(f"\nDRY RUN — would reclassify {len(candidates)} OI(s). Re-run with --apply to write.")
        return 0

    # --- apply ---
    lock_path = STATE_DIR / "open_items.lock"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            # Re-load inside the lock so we don't race with concurrent writers
            data = load_items()
            items = data.get("items", []) if isinstance(data, dict) else data
            reclassified = 0
            now = datetime.now().isoformat()
            for item in items:
                if not _is_size_blocker(item):
                    continue
                item["severity"] = "warn"
                item["updated_at"] = now
                audit_entry = {
                    "timestamp": now,
                    "actor": "maintenance/reclassify_size_ois",
                    "action": "reclassify",
                    "item_id": item["id"],
                    "from_severity": "blocker",
                    "to_severity": "warn",
                    "reason": "size-finding reclassified advisory per quality_advisory policy change",
                }
                append_audit(audit_entry)
                reclassified += 1
            if isinstance(data, dict):
                data["items"] = items
            else:
                data = {"items": items}
            save_items(data)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    print(f"\nReclassified {reclassified}/{len(candidates)} size-blocker OIs → severity=warn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
