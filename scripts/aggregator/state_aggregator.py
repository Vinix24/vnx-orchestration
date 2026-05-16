"""state_aggregator.py — Wave 5 PR-5.1: multi-project state aggregator write-pad.

Receives state-update events from N project T0's and central Control Centre.
Aggregates into central state.json + per-project facet files. Read-pad remains
build_central_view.py (Phase 6) — now fed by this write-pad.

ADR-005: every state mutation emits a structured NDJSON record to
.vnx-data/events/state_aggregator.ndjson. Aggregator events use
provider="aggregator" (domain-specific; not a CanonicalEvent stream-provider)
so we emit raw NDJSON dicts matching the canonical shape instead of
instantiating CanonicalEvent (which enforces a closed provider set).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

VALID_EVENT_TYPES = frozenset({
    "dispatch_created",
    "dispatch_completed",
    "t0_heartbeat",
    "incident",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class ProjectStateUpdate:
    project_id: str
    timestamp: str
    event_type: str
    payload: Dict[str, Any]
    source_t0: Optional[str] = None


class StateAggregator:
    """Append-only write-pad with per-project facet + central view.

    Thread-safe within process; atomic file writes for multi-process safety.
    """

    def __init__(self, vnx_data_dir: Path) -> None:
        self._data_dir = vnx_data_dir
        self._central_path = vnx_data_dir / "aggregator" / "central_state.json"
        self._facet_dir = vnx_data_dir / "aggregator" / "projects"
        self._events_path = vnx_data_dir / "events" / "state_aggregator.ndjson"
        self._lock = threading.Lock()
        self._central_path.parent.mkdir(parents=True, exist_ok=True)
        self._facet_dir.mkdir(parents=True, exist_ok=True)
        self._events_path.parent.mkdir(parents=True, exist_ok=True)

    def submit(self, update: ProjectStateUpdate) -> None:
        """Submit a state update. Atomic: emit event + update facet + central view."""
        with self._lock:
            self._emit_event(update)
            self._update_project_facet(update)
            self._update_central_view(update)

    def _emit_event(self, update: ProjectStateUpdate) -> None:
        record = {
            "event_id": str(uuid.uuid4()),
            "dispatch_id": (update.payload or {}).get("dispatch_id", ""),
            "terminal_id": update.source_t0 or "",
            "provider": "aggregator",
            "sub_provider": update.project_id,
            "event_type": update.event_type,
            "timestamp": update.timestamp or _now_iso(),
            "schema_version": 1,
            "data": update.payload,
        }
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _update_project_facet(self, update: ProjectStateUpdate) -> None:
        facet_path = self._facet_dir / f"{update.project_id}.json"
        facet = self._read_facet(facet_path)
        facet["last_updated"] = update.timestamp
        facet.setdefault("events", []).append({
            "event_type": update.event_type,
            "timestamp": update.timestamp,
            "payload": update.payload,
        })
        facet["events"] = facet["events"][-100:]
        tmp = facet_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(facet, indent=2), encoding="utf-8")
        os.replace(tmp, facet_path)

    def _update_central_view(self, update: ProjectStateUpdate) -> None:
        central: Dict[str, Any] = {}
        if self._central_path.exists():
            try:
                central = json.loads(self._central_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.error("state_aggregator: central_state.json corrupt, regenerating: %s", e)
                central = {}
        central.setdefault("projects", {})
        proj = central["projects"].setdefault(update.project_id, {
            "first_seen": update.timestamp,
            "event_counts": {},
        })
        proj["last_seen"] = update.timestamp
        proj["event_counts"][update.event_type] = (
            proj["event_counts"].get(update.event_type, 0) + 1
        )
        central["last_aggregated"] = update.timestamp
        tmp = self._central_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(central, indent=2), encoding="utf-8")
        os.replace(tmp, self._central_path)

    def _read_facet(self, facet_path: Path) -> Dict[str, Any]:
        if facet_path.exists():
            try:
                return json.loads(facet_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.error("state_aggregator: facet %s corrupt, regenerating: %s", facet_path, e)
        return {}

    def read_central(self) -> Dict[str, Any]:
        """Read-only access to central state. Phase 6 aggregator may call this."""
        if not self._central_path.exists():
            return {"projects": {}}
        return json.loads(self._central_path.read_text(encoding="utf-8"))
