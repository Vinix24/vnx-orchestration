#!/usr/bin/env python3
"""
terminal_state_check.py — Check terminal lock state for dispatcher.

Usage: terminal_state_check.py <state_file> <terminal_id> <dispatch_id>
Outputs: ALLOW:<reason> or BLOCK:<reason>
Exits:   0 always (output encodes the result)

Extracted from dispatcher_v8_minimal.sh terminal_lock_allows_dispatch.
Output contract (matches terminal_lock_allows_dispatch in dispatch_lifecycle.sh):
  rc=0 + stdout not starting with BLOCK:  -> ALLOW
  rc=0 + stdout "BLOCK:<reason>"          -> explicit block
  rc!=0                                   -> check failed (treated as block)
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


def check_terminal(state_file: str, terminal_id: str, dispatch_id: str) -> str:
    """Return ALLOW:<reason> or BLOCK:<reason> for the terminal/dispatch pair."""
    try:
        with open(state_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return "BLOCK:terminal_state_unreadable"

    record = ((payload.get("terminals") or {}).get(terminal_id) or {})
    if not isinstance(record, dict) or not record:
        return "ALLOW:no_record"

    now = datetime.now(timezone.utc)
    status = str(record.get("status") or "").strip().lower()
    claimed_by = str(record.get("claimed_by") or "").strip()
    lease_expires_at = parse_iso(record.get("lease_expires_at"))
    last_activity = parse_iso(record.get("last_activity"))

    claim_active = bool(claimed_by) and (lease_expires_at is None or lease_expires_at > now)
    if claim_active and claimed_by != dispatch_id:
        return f"BLOCK:active_claim:{claimed_by}"

    # Only block by status when the claim is still active for the same owner.
    # Expired claims must not prevent new dispatches.
    if status in {"working", "blocked"} and claim_active and claimed_by and claimed_by != dispatch_id:
        return f"BLOCK:status_claimed:{claimed_by}:{status}"

    if status in {"working", "blocked"} and not claimed_by and last_activity is not None:
        age_seconds = max(0, int((now - last_activity).total_seconds()))
        if age_seconds <= 900:
            return f"BLOCK:recent_{status}_without_claim:{age_seconds}s"

    return "ALLOW:clear"


def main():
    if len(sys.argv) != 4:
        print("BLOCK:terminal_state_unreadable")
        sys.exit(0)

    state_file, terminal_id, dispatch_id = sys.argv[1], sys.argv[2], sys.argv[3]
    print(check_terminal(state_file, terminal_id, dispatch_id))


if __name__ == "__main__":
    main()
