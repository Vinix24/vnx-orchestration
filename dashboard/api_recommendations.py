"""Dashboard API handler: /api/operator/recommendations.

Returns t0_recommendations.json content with derived counts so the dashboard
can display pending T0 recommendations without reading raw state files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

VNX_DIR = Path(__file__).resolve().parents[1]
CANONICAL_STATE_DIR = Path(
    os.environ.get("VNX_STATE_DIR", str(VNX_DIR / ".vnx-data" / "state"))
)
RECOMMENDATIONS_FILE = CANONICAL_STATE_DIR / "t0_recommendations.json"

_EMPTY: dict = {
    "recommendations": [],
    "total": 0,
    "total_p0": 0,
    "total_p1": 0,
    "total_p2": 0,
    "active_conflicts": {},
    "timestamp": None,
    "engine_version": None,
}


def get_operator_recommendations() -> dict:
    """Read t0_recommendations.json and return with priority counts.

    Returns an empty-state dict when the file is absent or unparseable so
    the dashboard endpoint always returns valid JSON.
    """
    if not RECOMMENDATIONS_FILE.exists():
        return dict(_EMPTY)

    try:
        raw = RECOMMENDATIONS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {**_EMPTY, "error": "parse_error"}

    recs = data.get("recommendations") or []
    return {
        "recommendations": recs,
        "total": len(recs),
        "total_p0": sum(1 for r in recs if r.get("priority") == "P0"),
        "total_p1": sum(1 for r in recs if r.get("priority") == "P1"),
        "total_p2": sum(1 for r in recs if r.get("priority") == "P2"),
        "active_conflicts": data.get("active_conflicts") or {},
        "timestamp": data.get("timestamp"),
        "engine_version": data.get("engine_version"),
    }
