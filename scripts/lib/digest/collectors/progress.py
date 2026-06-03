"""digest/collectors/progress.py — Collect yesterday's progress metrics from NDJSON.

collect_progress(state_dir, data_dir, window_hours) -> dict

Sources:
  state_dir/t0_receipts.ndjson  — dispatch completion events
  state_dir/open_items.ndjson   — OI lifecycle events

NDJSON-only. No DB calls, no sqlite3.
ADR-021: FileNotFoundError -> return zeros; json.JSONDecodeError -> skip line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digest.io import read_state_ndjson

logger = logging.getLogger(__name__)

_ZEROS: dict[str, object] = {
    "pr_merged": 0,
    "dispatches": 0,
    "dispatch_success_rate": "n/a",
    "ois_filed": 0,
    "ois_closed": 0,
    "auto_dream_cycles": 0,
    "failed_ci": 0,
}


def collect_progress(
    state_dir: Path,
    data_dir: Path,
    window_hours: int = 24,
) -> dict:
    """Return progress metrics for the last window_hours.

    All keys always present (mirrors _ZEROS). Returns _ZEROS copy on
    OSError (other than FileNotFoundError, which read_state_ndjson handles).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    try:
        receipts = read_state_ndjson(state_dir / "t0_receipts.ndjson")
        ois = read_state_ndjson(state_dir / "open_items.ndjson")
    except OSError as exc:
        logger.debug("digest.progress: state read failed: %s", exc)
        return dict(_ZEROS)

    window_receipts = [r for r in receipts if _ts(r) >= cutoff]
    window_ois = [o for o in ois if _ts(o) >= cutoff]

    dispatches = len(window_receipts)
    success_count = sum(1 for r in window_receipts if r.get("status") == "done")
    rate = f"{round(success_count / dispatches * 100)}%" if dispatches > 0 else "n/a"

    return {
        "pr_merged": sum(
            1 for r in window_receipts if r.get("event_type") == "pr_merged"
        ),
        "dispatches": dispatches,
        "dispatch_success_rate": rate,
        "ois_filed": sum(
            1 for o in window_ois if o.get("action") == "open"
        ),
        "ois_closed": sum(
            1 for o in window_ois if o.get("action") in ("close", "closed")
        ),
        "auto_dream_cycles": sum(
            1 for r in window_receipts if r.get("event_type") == "dream_cycle"
        ),
        "failed_ci": sum(
            1 for r in window_receipts if r.get("status") == "failed"
        ),
    }


def _ts(record: dict) -> datetime:
    """Parse ISO timestamp from record, returning epoch-min on failure."""
    raw = (record.get("timestamp") or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
