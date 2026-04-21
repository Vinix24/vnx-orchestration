#!/usr/bin/env python3
"""
terminal_state_check.py — invoked by dispatch_lifecycle.sh.

Reads $STATE_DIR/terminal_state.json and decides whether a dispatch may
proceed to the target terminal.

Args: state_file terminal_id dispatch_id

Output contract (matches terminal_lock_allows_dispatch in dispatch_lifecycle.sh):
  rc=0 + stdout empty or not starting with BLOCK:  -> ALLOW
  rc=0 + stdout "BLOCK:<reason>"                   -> explicit block
  rc!=0                                            -> check failed (treated as block)
"""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 4:
        print("BLOCK:missing_args")
        return 0

    state_file = Path(sys.argv[1])
    terminal_id = sys.argv[2]
    dispatch_id = sys.argv[3]

    if not state_file.exists():
        return 0

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("BLOCK:state_unreadable")
        return 0

    terminals = data.get("terminals") or {}
    entry = terminals.get(terminal_id)

    if entry is None:
        return 0

    status = (entry.get("status") or "idle").lower()

    if status == "idle":
        return 0

    if status == "working":
        claimed_by = entry.get("claimed_by") or ""
        if claimed_by == dispatch_id:
            return 0
        print(f"BLOCK:active_claim:{claimed_by or 'unknown'}")
        return 0

    if status in ("offline", "stopped"):
        print(f"BLOCK:terminal_{status}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
