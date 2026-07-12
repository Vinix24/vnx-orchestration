"""Component health beacon API for the dashboard.

Exposes :func:`_operator_get_health` which serves
``GET /api/operator/health`` — JSON of every component beacon under
``$VNX_DATA_DIR/health/``, plus a roll-up summary and a per-subsystem
effectiveness summary (framework-status-audit-and-cockpit PR-18).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

_logger = logging.getLogger(__name__)

# Make scripts/lib importable so we can reuse health_beacon helpers.
_DASHBOARD_DIR = Path(__file__).resolve().parent
_SCRIPTS_LIB = _DASHBOARD_DIR.parent / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from health_beacon import all_beacons, beacon_summary  # noqa: E402


def _resolve_data_dir() -> Path:
    """Resolve the .vnx-data root, mirroring api_operator's logic."""
    import os
    project_root = _DASHBOARD_DIR.parent
    state_dir = Path(os.environ.get("VNX_STATE_DIR", str(project_root / ".vnx-data" / "state")))
    return state_dir.parent


def _subsystem_effectiveness_summary(data_dir: Path) -> List[Dict[str, Any]]:
    """Every known cockpit subsystem (PR-1..8/17 registry) joined against its
    beacon-derived health. Read-only: reads ``config_registry``/probe registries
    for subsystem NAMES and ``health_beacon.all_beacons()`` for their last-written
    status — it never runs a probe (that stays owned by ``vnx subsystems --probe``,
    PR-3/PR-5). A subsystem with no beacon on disk (never probed, or its probe
    itself reported ``unknown``) is reported ``health="unknown"`` so the health
    page can prompt the operator to add/improve a probe (PR-18 acceptance
    criterion), rather than silently omitting the card.
    """
    try:
        import subsystem_health  # noqa: PLC0415
    except Exception as exc:
        _logger.warning("subsystem_health unavailable; effectiveness summary empty: %s", exc)
        return []

    try:
        names = subsystem_health.known_subsystems()
    except Exception:
        _logger.exception("known_subsystems() failed")
        return []

    beacons = all_beacons(data_dir)
    rows: List[Dict[str, Any]] = []
    for name in names:
        beacon = beacons.get(name)
        if beacon:
            rows.append({
                "subsystem": name,
                "health": beacon.get("health", "unknown"),
                "status": beacon.get("status", ""),
                "last_signal": beacon.get("last_run_iso", ""),
                "detail": beacon.get("details") or {},
            })
        else:
            rows.append({
                "subsystem": name,
                "health": "unknown",
                "status": "unknown",
                "last_signal": "",
                "detail": {},
            })
    return rows


def _operator_get_health() -> Dict[str, Any]:
    """GET /api/operator/health — beacon dump with classification, plus the
    per-subsystem effectiveness summary (PR-18)."""
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
            "subsystems": _subsystem_effectiveness_summary(data_dir),
        }
    except Exception as exc:
        return {
            "queried_at": now,
            "data_dir": str(data_dir),
            "overall": "fail",
            "counts": {"ok": 0, "stale": 0, "fail": 0, "corrupt": 0},
            "beacons": {},
            "subsystems": [],
            "error": str(exc),
        }


__all__ = ["_operator_get_health"]
