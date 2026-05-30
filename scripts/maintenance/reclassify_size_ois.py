#!/usr/bin/env python3
"""reclassify_size_ois.py — Backfill: downgrade size-blocker OIs to warn.

Size findings (file size > threshold, function size > threshold) were
previously emitted as severity="blocking" and stored as severity="blocker".
Policy change (dispatch 20260530-102058-hyg-policy): size findings are
ADVISORY — they must be severity="warn", not "blocker".

This script finds every open OI with severity=="blocker" that carries a size
signature (via dedup_key prefix or title pattern) and reclassifies it to
severity="warn". Idempotent — running twice is safe.

Usage:
    python3 scripts/maintenance/reclassify_size_ois.py          # dry-run
    python3 scripts/maintenance/reclassify_size_ois.py --apply  # apply
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = REPO_ROOT / ".vnx-data" / "state"
OPEN_ITEMS_FILE = STATE_DIR / "open_items.json"
AUDIT_LOG = STATE_DIR / "open_items_audit.jsonl"

RECLASSIFY_REASON = (
    "size-finding reclassified advisory per quality_advisory policy change"
)
AUDIT_ACTOR = "reclassify_size_ois"


def _is_size_oi(item: dict) -> bool:
    """Return True if this OI is a size-based finding."""
    dedup = item.get("dedup_key", "")
    if dedup.startswith(("qa:function_size_", "qa:file_size_")):
        return True
    title = item.get("title", "")
    if "exceeds blocking threshold" in title:
        return True
    title_lower = title.lower()
    if "exceeds threshold" in title_lower and (
        title_lower.startswith("function ") or title_lower.startswith("file ")
    ):
        return True
    return False


def _load(store_path: Path) -> dict:
    if not store_path.exists():
        return {"schema_version": "1.0", "items": [], "next_id": 1}
    return json.loads(store_path.read_text(encoding="utf-8"))


def _save(store_path: Path, data: dict) -> None:
    data["last_updated"] = datetime.now().isoformat()
    tmp = store_path.with_suffix(".json.tmp")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, store_path)


def _write_audit(audit_path: Path, entries: list) -> None:
    if not entries:
        return
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps(e) + "\n" for e in entries)
    with open(audit_path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(lines)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def reclassify(
    store_path: Path,
    audit_path: Path,
    apply: bool = False,
) -> dict:
    """Core reclassification logic.

    Returns {"found": N, "reclassified": N}.
    Thread-safe: acquires fcntl.LOCK_EX on a sidecar lock file.
    """
    lock_path = store_path.with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data = _load(store_path)
            targets = [
                item for item in data.get("items", [])
                if item.get("status") == "open"
                and item.get("severity") == "blocker"
                and _is_size_oi(item)
            ]

            if not apply:
                return {"found": len(targets), "reclassified": 0}

            now = datetime.now().isoformat()
            audit_entries = []
            for item in targets:
                item["severity"] = "warn"
                item["updated_at"] = now
                audit_entries.append({
                    "timestamp": now,
                    "actor": AUDIT_ACTOR,
                    "action": "reclassify",
                    "item_id": item["id"],
                    "from_severity": "blocker",
                    "to_severity": "warn",
                    "reason": RECLASSIFY_REASON,
                })

            _save(store_path, data)
            _write_audit(audit_path, audit_entries)
            return {"found": len(targets), "reclassified": len(targets)}
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually reclassify OIs (default is dry-run)",
    )
    args = parser.parse_args()

    result = reclassify(OPEN_ITEMS_FILE, AUDIT_LOG, apply=args.apply)
    found = result["found"]
    reclassified = result["reclassified"]

    print(f"Open size-blocker OIs found: {found}")

    if found == 0:
        print("Nothing to do.")
        return 0

    if not args.apply:
        print(f"DRY RUN — would reclassify {found} OI(s) blocker→warn.")
        print("Re-run with --apply to write changes.")
    else:
        print(f"Reclassified {reclassified}/{found}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
