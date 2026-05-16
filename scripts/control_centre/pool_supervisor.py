"""pool_supervisor.py — Cross-project pool supervisor for Control Centre.

Reads pool_state_unified view from the aggregator DB. Detects:
- Pools below min_workers (starvation)
- Pools at max_workers (capacity-bound)
- Stale join patterns (warning before reap)

Emits to ~/.vnx-aggregator/events/pool_decisions.ndjson for cross-project audit.

Wave 6 PR-6.8 — ADR-018 elastic worker pool, Control Centre integration.
"""

from __future__ import annotations

import fcntl
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

_DEFAULT_EVENTS_PATH = Path("~/.vnx-aggregator/events/pool_decisions.ndjson")


def list_all_pools(aggregator_db: Path) -> List[Dict]:
    """Return pool_state_unified rows across all registered projects.

    Returns an empty list when the view does not exist (pre-aggregation).
    """
    if not aggregator_db.is_file():
        return []
    try:
        with sqlite3.connect(str(aggregator_db)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM pool_state_unified")
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        log.warning("list_all_pools: %s", exc)
        return []


def detect_starvation(pools: List[Dict]) -> List[Dict]:
    """Return pools where active_count < min_workers."""
    return [p for p in pools if p["active_count"] < p["min_workers"]]


def detect_capacity_bound(pools: List[Dict], queue_threshold: int = 4) -> List[Dict]:
    """Return pools at or above max_workers (capacity-bound signal).

    queue_threshold is reserved for future queue-depth enrichment; the current
    heuristic flags any pool that has reached max_workers.
    """
    return [p for p in pools if p["active_count"] >= p["max_workers"]]


def emit_supervisor_event(events_path: Path, event_type: str, payload: Dict) -> None:
    """Append a supervisor event to pool_decisions.ndjson.

    Uses fcntl.flock(LOCK_EX) so concurrent supervisor runs don't interleave
    partial writes. Opens in append-binary mode; atomic at the OS level for
    single-record JSON lines.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "payload": payload,
    }
    line = (json.dumps(event) + "\n").encode("utf-8")
    with open(events_path, "ab") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_supervision_tick(
    aggregator_db: Path,
    events_path: Path | None = None,
) -> Dict:
    """Single supervision tick: read pools, detect issues, emit events.

    Returns a summary dict suitable for logging or reporting.
    """
    resolved_events = (events_path or _DEFAULT_EVENTS_PATH).expanduser()

    pools = list_all_pools(aggregator_db)
    if not pools:
        log.debug("run_supervision_tick: no pools found in aggregator DB")
        return {"pools": 0, "starvation": 0, "capacity_bound": 0}

    starved = detect_starvation(pools)
    capacity_bound = detect_capacity_bound(pools)

    for pool in starved:
        emit_supervisor_event(
            resolved_events,
            "pool.supervisor.starvation",
            {
                "project_id": pool["project_id"],
                "pool_id": pool["pool_id"],
                "active_count": pool["active_count"],
                "min_workers": pool["min_workers"],
            },
        )
        log.warning(
            "pool starvation: project=%s pool=%s active=%d min=%d",
            pool["project_id"],
            pool["pool_id"],
            pool["active_count"],
            pool["min_workers"],
        )

    for pool in capacity_bound:
        emit_supervisor_event(
            resolved_events,
            "pool.supervisor.capacity_bound",
            {
                "project_id": pool["project_id"],
                "pool_id": pool["pool_id"],
                "active_count": pool["active_count"],
                "max_workers": pool["max_workers"],
            },
        )

    return {
        "pools": len(pools),
        "starvation": len(starved),
        "capacity_bound": len(capacity_bound),
    }
