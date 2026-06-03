#!/usr/bin/env python3
"""VNX Decision Log — append operator decisions to decisions_log.ndjson (ADR-005 ledger)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_UTC = timezone.utc


def _state_dir() -> Path:
    explicit = os.environ.get("VNX_STATE_DIR")
    if explicit:
        return Path(explicit)
    data = os.environ.get("VNX_DATA_DIR", "")
    return Path(data) / "state" if data else Path(".vnx-data") / "state"


def append_decision(
    dec_id: str,
    action: str,
    reason: str = "",
    actor: str = "operator",
    timestamp: str | None = None,
) -> Path:
    """Append one decision record to decisions_log.ndjson.

    Returns the path written to.
    """
    ts = timestamp or datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z")
    record = {
        "event_type": "decision",
        "dec_id": dec_id,
        "action": action,
        "reason": reason,
        "actor": actor,
        "timestamp": ts,
    }
    log_path = _state_dir() / "decisions_log.ndjson"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return log_path


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: decisions_log.py <dec_id> <action> [reason] [actor]", file=sys.stderr)
        return 1
    dec_id, action = args[0], args[1]
    reason = args[2] if len(args) > 2 else ""
    actor = args[3] if len(args) > 3 else "operator"
    path = append_decision(dec_id, action, reason, actor)
    print(f"Decision logged: {dec_id} -> {action} ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
