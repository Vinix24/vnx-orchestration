#!/usr/bin/env python3
"""Migrate governance_audit.ndjson from VNX_EVENTS_DIR to VNX_STATE_DIR.

Idempotent: safe to run multiple times. Deduplication key: timestamp + context_hash.
If VNX_STATE_DIR file already exists, appends only entries not already present.
After a successful merge, removes events/ file.

Usage:
    python3 scripts/migrate_governance_audit_path.py [--data-dir PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_ndjson(path: Path) -> list[dict]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _dedup_key(entry: dict) -> str:
    ts = entry.get("timestamp", "")
    ch = entry.get("context_hash") or ""
    et = entry.get("event_type", "")
    return f"{ts}|{ch}|{et}"


def migrate(data_dir: Path, dry_run: bool = False) -> dict:
    src = data_dir / "events" / "governance_audit.ndjson"
    dst = data_dir / "state" / "governance_audit.ndjson"

    result = {
        "src_exists": src.exists(),
        "dst_exists": dst.exists(),
        "src_entries": 0,
        "dst_existing": 0,
        "appended": 0,
        "skipped_dupes": 0,
        "src_removed": False,
        "dry_run": dry_run,
    }

    if not src.exists():
        print(f"[migrate] Source not found: {src} — nothing to migrate.")
        return result

    src_entries = _parse_ndjson(src)
    result["src_entries"] = len(src_entries)

    dst_entries: list[dict] = []
    if dst.exists():
        dst_entries = _parse_ndjson(dst)
    result["dst_existing"] = len(dst_entries)

    existing_keys = {_dedup_key(e) for e in dst_entries}
    to_append = [e for e in src_entries if _dedup_key(e) not in existing_keys]
    result["appended"] = len(to_append)
    result["skipped_dupes"] = len(src_entries) - len(to_append)

    if dry_run:
        print(
            f"[migrate] DRY RUN — would append {result['appended']} entries "
            f"(skip {result['skipped_dupes']} dupes) to {dst}"
        )
        return result

    if to_append:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "a", encoding="utf-8") as fh:
            for entry in to_append:
                fh.write(json.dumps(entry) + "\n")

    src.unlink()
    result["src_removed"] = True

    print(
        f"[migrate] Migrated {result['appended']} entries to {dst} "
        f"(skipped {result['skipped_dupes']} dupes). Source removed."
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None, help="Path to .vnx-data (default: auto-detect)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing anything")
    args = parser.parse_args(argv)

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))

    result = migrate(data_dir, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
