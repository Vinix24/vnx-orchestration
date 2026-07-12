"""subsystem_health — runs every registered effectiveness probe and emits a
HealthBeacon per subsystem (framework-status-audit-and-cockpit PR-5).

This module SUPPLIES the aggregator behind PR-3's guarded ``vnx subsystems
--probe`` import: PR-3 owns ``scripts/lib/subsystems.py`` (the CLI) and imports
this module at call time; this module does not import or edit PR-3's CLI, so the
two PRs are parallel siblings with no cross-file edit and no added DAG dependency.
Before PR-3 lands, nothing in the CLI surface changes.

ADR-007 scope: ``aggregate()`` is read-only over ``config_registry`` and each
probe's own read-only signal sources. Its only write is the beacon file under
``<state_dir>/health/<subsystem>.json`` via ``health_beacon.HealthBeacon``
(itself a read-only-safe atomic file write, not a DB table). No new central-DB
table is created, so the ADR-007 composite-``project_id``-key requirement does
not attach.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config_registry  # noqa: E402  (after the scripts/lib path guard above)
import project_root  # noqa: E402
from effectiveness_probe import (  # noqa: E402
    EFFECTIVENESS_PROBES,
    PROBE_TO_BEACON,
    ProbeResult,
)
from health_beacon import HealthBeacon  # noqa: E402

# Concrete probes self-register into EFFECTIVENESS_PROBES via `@register_probe`
# at import time (PR-7). Importing them here means aggregate() sees every
# registered probe regardless of which entrypoint (CLI, test, dashboard)
# triggers the first import of this module.
import governance_effectiveness_probe  # noqa: E402,F401
import injection_effectiveness_probe  # noqa: E402,F401
import migration_effectiveness_probe  # noqa: E402,F401
import plan_gate_effectiveness_probe  # noqa: E402,F401


def known_subsystems() -> List[str]:
    """Every cockpit subsystem name: flag-backed entries from ``CONFIG_REGISTRY``,
    flag-less entries from ``CONFIG_REGISTRY_SUBSYSTEMS``, and any subsystem with a
    registered probe but no registry entry yet. Deduped and sorted."""
    names = {
        entry.subsystem
        for entry in config_registry.CONFIG_REGISTRY.values()
        if entry.subsystem
    }
    names |= set(config_registry.CONFIG_REGISTRY_SUBSYSTEMS.keys())
    names |= set(EFFECTIVENESS_PROBES.keys())
    return sorted(names)


def aggregate(
    state_dir: Optional[Path] = None,
    subsystems: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run every registered probe over ``subsystems`` (default: every known
    cockpit subsystem), write a beacon for each probed result, and return a dict
    keyed by subsystem name -> ``{"status", "signal", "detail"}``.

    A subsystem with no entry in ``EFFECTIVENESS_PROBES`` reports
    ``status="unknown"``, ``signal="no probe registered"`` and gets NO beacon
    written (there is nothing measured to persist).
    """
    if state_dir is None:
        state_dir = project_root.resolve_data_dir(__file__)
    state_dir = Path(state_dir)

    target = sorted(subsystems) if subsystems is not None else known_subsystems()

    results: Dict[str, Dict[str, Any]] = {}
    for name in target:
        probe_cls = EFFECTIVENESS_PROBES.get(name)
        if probe_cls is None:
            result = ProbeResult(status="unknown", signal="no probe registered", detail={})
        else:
            result = probe_cls().run()

        results[name] = {"status": result.status, "signal": result.signal, "detail": result.detail}

        beacon_status = PROBE_TO_BEACON.get(result.status)
        if beacon_status is not None:
            HealthBeacon(state_dir, name).heartbeat(status=beacon_status, details=result.detail)

    return results


__all__ = ["known_subsystems", "aggregate"]
