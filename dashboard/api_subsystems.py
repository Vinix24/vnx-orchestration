"""Subsystem cockpit API (framework-status-audit-and-cockpit PR-4).

  GET /api/operator/subsystems -> {project_id, subsystems: [...], queried_at}

Rowset is the union of the two ``config_registry`` sources (mirrors ``vnx subsystems``, PR-3, but
reads the registry directly rather than shelling out to the CLI):

  (a) ``config_registry.CONFIG_REGISTRY`` — flag-backed subsystems. Several flags can share one
      subsystem (e.g. every intelligence-tuning flag maps to ``intelligence-self-learning-loop``);
      the row's canonical ``flag`` is picked by ``config_registry.canonical_flags()`` (OI-1385) —
      an EXPLICIT, order-independent rule (a flag with no production read-site can never win; a
      multi-candidate subsystem needs exactly one flag marked ``cockpit_canonical=True``), not
      "whichever entry CONFIG_REGISTRY iterates last".
  (b) ``config_registry.CONFIG_REGISTRY_SUBSYSTEMS`` — flag-less kernel subsystems (``phantom_guard``,
      ``dispatch-plan``, ...). Disjoint from (a) by construction (config_registry.py docstring).

``governance-enforcement-stack``'s row is a further exception (OI-1385): its ``effective_value``
is read directly from ``.vnx/governance_enforcement.yaml`` (via
``governance_enforcement_effective_value()``), not from its canonical flag's registry value — that
subsystem is governed by multi-level YAML checks, and the flag that used to be canonical here had
zero production read-sites (see its ``read_site_wired`` entry in config_registry.py); showing its
static default read as "parked" while the YAML ran hard-mandatory checks
(``docs/core/framework-status-audit-and-cockpit_PRD.md:89``).

HEALTH is read-only from ``health_beacon.all_beacons(data_dir)`` — the beacon root is
``VNX_DATA_DIR`` (health_beacon writes/reads under ``VNX_DATA_DIR/health``), NOT ``VNX_STATE_DIR``.
A subsystem with no beacon reports ``unknown`` — no probe framework exists yet (PR-5..7).

Single-tenant: reads this dashboard's project (``CANONICAL_STATE_DIR``), same as ``api_config.py``.
Fail-open: a missing/unavailable registry never raises past this module — the handler returns 503
with an empty subsystem list rather than a 500.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from api_health import _resolve_data_dir
from api_operator import VNX_DIR, _op_dashboard_project_id

_logger = logging.getLogger(__name__)

_SUB_SCRIPTS_LIB = str(VNX_DIR / "scripts" / "lib")
if _SUB_SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SUB_SCRIPTS_LIB)

# The real governance-enforcement-stack afdwingniveau source (OI-1385) — read-only, never
# written by this module. Matches governance_enforcer.DEFAULT_CONFIG_PATH's own derivation.
_GOVERNANCE_ENFORCEMENT_YAML = VNX_DIR / ".vnx" / "governance_enforcement.yaml"

try:
    import config_registry as _cr  # type: ignore[import]
    _REGISTRY_AVAILABLE = True
except Exception:
    # Optional dependency: the registry module may be absent in a stripped install. Log so a real
    # init error is visible (not silently mistaken for "no subsystems") but stay non-fatal.
    _logger.warning("config_registry unavailable; subsystems API will report 503", exc_info=True)
    _cr = None  # type: ignore[assignment]
    _REGISTRY_AVAILABLE = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_flags(cr) -> Dict[str, str]:
    """subsystem -> the one flag key shown as the row's canonical ``flag``.

    Delegates to ``config_registry.canonical_flags()`` (OI-1385) — the explicit,
    order-independent selection rule now lives there as the single source of truth so it can be
    reused by other consumers of ``CONFIG_REGISTRY`` (e.g. ``vnx_cli/commands/subsystems.py``)
    instead of re-deriving it. ``cr`` stays a parameter (rather than a bare module-level call)
    so tests can inject a substitute registry module.
    """
    return cr.canonical_flags()


# The one subsystem whose row's effective_value must NOT come from a CONFIG_REGISTRY bool at
# all (OI-1385): governance-enforcement-stack is governed by multi-level checks in
# .vnx/governance_enforcement.yaml, not a single on/off flag, and the flag that used to win
# canonical selection here has zero production read-sites (config_registry.py:
# read_site_wired=False). Showing its static default -- '0' -- read as "enforcement is parked"
# while the yaml ran hard-mandatory checks (framework-status-audit-and-cockpit_PRD.md:89
# predicted exactly this drift).
_YAML_SOURCED_SUBSYSTEMS = frozenset({"governance-enforcement-stack"})


def governance_enforcement_effective_value(config_path: Path) -> str:
    """Read-only ``"<mode>:<max_check_level>"`` summary of governance_enforcement.yaml.

    Never returns the registry's stale static bool. Returns the explicit sentinel "unknown" —
    never a silent "0" -- when the file is missing, unreadable, or malformed: an unknown
    enforcement level must never read as "off".
    """
    try:
        import governance_enforcer as _ge
        enforcer = _ge.GovernanceEnforcer()
        enforcer.load_config(config_path)
        summary = enforcer.effective_summary()
        return f"{summary['mode']}:{summary['max_level']}"
    except Exception:
        _logger.warning(
            "governance_enforcement.yaml at %s unreadable; reporting unknown effective value",
            config_path, exc_info=True,
        )
        return "unknown"


def build_rows(cr, project_id: Optional[str]) -> List[Dict[str, Any]]:
    """Rowset = (a) flag-less CONFIG_REGISTRY_SUBSYSTEMS union (b) flag-backed CONFIG_REGISTRY
    (one canonical flag per subsystem). Health is attached separately by the caller."""
    rows: List[Dict[str, Any]] = []

    for subsystem, meta in cr.CONFIG_REGISTRY_SUBSYSTEMS.items():
        status = meta.get("status", "COCKPIT")
        rows.append({
            "subsystem": subsystem,
            "what": meta.get("description", ""),
            "flag": None,
            "status": status,
            "effective_value": None,
        })

    for subsystem, flag_key in _canonical_flags(cr).items():
        entry = cr.CONFIG_REGISTRY[flag_key]
        if subsystem in _YAML_SOURCED_SUBSYSTEMS:
            effective_value = governance_enforcement_effective_value(_GOVERNANCE_ENFORCEMENT_YAML)
        else:
            effective_value = cr.get(flag_key, project_id)
        rows.append({
            "subsystem": subsystem,
            "what": entry.description,
            "flag": flag_key,
            "status": entry.status or "COCKPIT",
            "effective_value": effective_value,
        })

    rows.sort(key=lambda r: r["subsystem"])
    return rows


def _attach_health(rows: List[Dict[str, Any]], data_dir: Path) -> None:
    from health_beacon import all_beacons

    beacons = all_beacons(data_dir)  # data_dir = VNX_DATA_DIR, not VNX_STATE_DIR
    for row in rows:
        beacon = beacons.get(row["subsystem"])
        if beacon:
            row["health"] = beacon.get("health", "unknown")
            row["last_signal"] = beacon.get("last_run_iso", "")
        else:
            row["health"] = "unknown"
            row["last_signal"] = ""


def operator_get_subsystems(params: dict, *, project_id: "str | None" = None) -> "tuple[dict, int]":
    """GET /api/operator/subsystems -- the live subsystem cockpit ledger (MAP + ON/OFF + HEALTH).

    Returns (body, status): 200 on success; 503 when the registry is unavailable / errors (the body
    carries a generic message -- the exception detail is logged server-side, never returned)."""
    pid = project_id or _op_dashboard_project_id()
    if not _REGISTRY_AVAILABLE:
        return {"project_id": pid, "subsystems": [], "queried_at": _now(),
                "error": "config registry unavailable"}, 503
    try:
        rows = build_rows(_cr, pid)
        _attach_health(rows, _resolve_data_dir())
    except Exception:
        _logger.exception("subsystems inventory failed for project_id=%s", pid)
        return {"project_id": pid, "subsystems": [], "queried_at": _now(),
                "error": "subsystems inventory failed"}, 503
    return {"project_id": pid, "subsystems": rows, "queried_at": _now()}, 200


__all__ = ["operator_get_subsystems", "build_rows", "governance_enforcement_effective_value"]
