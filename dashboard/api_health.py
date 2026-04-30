"""Component health beacon API for the dashboard.

Exposes :func:`_operator_get_health` which serves
``GET /api/operator/health`` — JSON of every component beacon under
``$VNX_DATA_DIR/health/``, plus a roll-up summary.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Make scripts/lib importable so we can reuse health_beacon helpers.
_DASHBOARD_DIR = Path(__file__).resolve().parent
_SCRIPTS_LIB = _DASHBOARD_DIR.parent / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from health_beacon import beacon_summary  # noqa: E402


def _resolve_data_dir() -> Path:
    """Resolve the .vnx-data root, mirroring api_operator's logic."""
    import os
    project_root = _DASHBOARD_DIR.parent
    state_dir = Path(os.environ.get("VNX_STATE_DIR", str(project_root / ".vnx-data" / "state")))
    return state_dir.parent


def _operator_get_health() -> Dict[str, Any]:
    """GET /api/operator/health — beacon dump with classification."""
    now = datetime.now(timezone.utc).isoformat()
    data_dir = _resolve_data_dir()
    try:
        summary = beacon_summary(data_dir)
        return {
            "queried_at": now,
            "data_dir": str(data_dir),
            "overall": summary["overall"],
            "counts": summary["counts"],
            "beacons": summary["beacons"],
        }
    except Exception as exc:
        return {
            "queried_at": now,
            "data_dir": str(data_dir),
            "overall": "fail",
            "counts": {"ok": 0, "stale": 0, "fail": 0, "corrupt": 0},
            "beacons": {},
            "error": str(exc),
        }


__all__ = ["_operator_get_health"]
