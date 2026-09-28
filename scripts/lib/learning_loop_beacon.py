"""Beacon and shadow report of the nightly learning-loop phase.

The phase (``scripts/learning_loop_nightly.py``, run by
``conversation_analyzer_nightly.sh``) writes one beacon per run, also a failed
one. ``vnx_doctor`` reads it: a missing or stale beacon is the only way a
silently dead nightly phase becomes visible.

Both files live directly under the project's state dir. Writes are atomic
(per-writer temp file + ``os.replace`` via ``atomic_io``).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from atomic_io import atomic_write_json

BEACON_FILENAME = "learning_loop_nightly_beacon.json"
SHADOW_REPORT_FILENAME = "learning_loop_shadow_report.json"

# The job runs at 02:00 daily; 36 hours is one missed night plus slack.
MAX_BEACON_AGE_HOURS = 36

STATUS_OK = "ok"
STATUS_FAILED = "failed"


def beacon_path(state_dir: Path) -> Path:
    return Path(state_dir) / BEACON_FILENAME


def shadow_report_path(state_dir: Path) -> Path:
    return Path(state_dir) / SHADOW_REPORT_FILENAME


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_beacon(state_dir: Path, payload: Dict[str, Any]) -> Path:
    path = beacon_path(state_dir)
    atomic_write_json(path, payload)
    return path


def read_beacon(state_dir: Path) -> Optional[Dict[str, Any]]:
    """The beacon dict, or None when absent, unreadable or not a JSON object."""
    try:
        data = json.loads(beacon_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def beacon_age_hours(beacon: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    """Hours since the run ended (falls back to its start); None if unparseable."""
    raw = beacon.get("finished_at") or beacon.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds() / 3600
