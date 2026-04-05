#!/usr/bin/env python3
"""
terminal_state_check.py — Check terminal lock state for dispatcher.

Usage: terminal_state_check.py <state_file> <terminal_id> <dispatch_id>
Outputs: ALLOW:<reason> or BLOCK:<reason>
Exits:   0 always (output encodes the result)

Extracted from dispatcher_v8_minimal.sh terminal_lock_allows_dispatch (lines 303-354).
"""

import json
import sys
from datetime import datetime, timezone


def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def main():
    if len(sys.argv) != 4:
        print("BLOCK:terminal_state_unreadable")
        sys.exit(0)

    state_file, terminal_id, dispatch_id = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        with open(state_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        print("BLOCK:terminal_state_unreadable")
        sys.exit(0)

    record = ((payload.get("terminals") or {}).get(terminal_id) or {})
    if not isinstance(record, dict) or not record:
        print("ALLOW:no_record")
        sys.exit(0)

    now = datetime.now(timezone.utc)
    status = str(record.get("status") or "").strip().lower()
    claimed_by = str(record.get("claimed_by") or "").strip()
    lease_expires_at = parse_iso(record.get("lease_expires_at"))
    last_activity = parse_iso(record.get("last_activity"))

    claim_active = bool(claimed_by) and (lease_expires_at is None or lease_expires_at > now)
    if claim_active and claimed_by != dispatch_id:
        print(f"BLOCK:active_claim:{claimed_by}")
        sys.exit(0)

    # Only block by claimed status when the claim is still active.
    # Expired claims should not prevent new dispatches.
    if status in {"working", "blocked"} and claim_active and claimed_by and claimed_by != dispatch_id:
        print(f"BLOCK:status_claimed:{claimed_by}:{status}")
        sys.exit(0)

    if status in {"working", "blocked"} and not claimed_by and last_activity is not None:
        age_seconds = max(0, int((now - last_activity).total_seconds()))
        if age_seconds <= 900:
            print(f"BLOCK:recent_{status}_without_claim:{age_seconds}s")
            sys.exit(0)

    print("ALLOW:clear")


if __name__ == "__main__":
    main()
