#!/usr/bin/env python3
"""
VNX Supervisor Shadow — Observer that mirrors supervisor outcomes as typed incident records.

PR-1 deliverable: thin shadow wrapper that records incidents from the existing
supervisor without changing its recovery behavior.

Shadow mode is controlled by VNX_INCIDENT_SHADOW (default "1" = on).
When shadow mode is off (VNX_INCIDENT_SHADOW=0), all functions are no-ops
and the supervisor runs exactly as before.

Design rules:
  - Shadow mode NEVER changes restart decisions (A-R9: legacy paths stay active).
  - Shadow mode NEVER blocks or delays the supervisor monitor loop.
  - Shadow mode failures are caught and logged — they must not surface to supervisor.
  - Each supervisor outcome maps to exactly one IncidentClass (G-R1).

Usage in supervisor (conceptual bash → Python bridge):
    The supervisor can call this module via:
      python -c "from supervisor_shadow import record_process_crash; ..."
    Or source vnx_supervisor_shadow_bridge.sh which wraps these calls.

    More ergonomically, vnx_supervisor_simple.sh calls the bridge script in
    shadow mode before/after restart attempts.

Incident class mapping:
  - process died (crash) → process_crash
  - process failed health check after restart → process_crash (repeat)
  - restart limit hit → repeated_failure_loop (auto-detected by detect_repeated_failure_loop)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Path setup — allow standalone invocation from scripts/
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_SCRIPTS_LIB = _HERE.parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from incident_log import (  # noqa: E402  # sys.path adjusted above for standalone invocation
    consume_budget,
    create_incident,
    detect_repeated_failure_loop,
    escalate_incident,
    generate_incident_summary,
    is_shadow_mode,
    resolve_incident,
)
from incident_taxonomy import IncidentClass, REPEATED_FAILURE_THRESHOLD  # noqa: E402

logger = logging.getLogger("vnx.supervisor_shadow")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_state_dir() -> Optional[Path]:
    """Resolve state dir from VNX_STATE_DIR environment variable."""
    sd = os.environ.get("VNX_STATE_DIR")
    if sd:
        return Path(sd)
    return None


def _safe_record(fn, *args, **kwargs) -> Optional[Dict[str, Any]]:
    """Call fn safely — catch all exceptions so shadow never disrupts supervisor."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("supervisor_shadow: record failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API — one function per supervisor outcome type
# ---------------------------------------------------------------------------

def record_process_crash(
    component_name: str,
    *,
    pid: Optional[int] = None,
    failure_detail: Optional[str] = None,
    dispatch_id: Optional[str] = None,
    terminal_id: Optional[str] = None,
    state_dir: Optional[str | Path] = None,
) -> Optional[str]:
    """Record a process_crash incident for a supervised component.

    Called by the supervisor monitor loop when a process is found dead.

    Returns the incident_id if the record was written, None otherwise.
    Shadow mode off → returns None immediately (no-op).
    """
    if not is_shadow_mode():
        return None

    sd = Path(state_dir) if state_dir else _get_state_dir()
    if sd is None:
        return None

    meta: Dict[str, Any] = {"component": component_name}
    if pid is not None:
        meta["pid"] = pid

    incident = _safe_record(
        create_incident,
        sd,
        incident_class=IncidentClass.PROCESS_CRASH,
        entity_type="component",
        entity_id=component_name,
        component_name=component_name,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        failure_detail=failure_detail or f"{component_name} process not found",
        actor="supervisor_shadow",
        metadata=meta,
    )

    if incident is None:
        return None

    incident_id = incident["incident_id"]

    # Consume budget for this component+class
    _safe_record(
        consume_budget,
        sd,
        entity_type="component",
        entity_id=component_name,
        incident_class=IncidentClass.PROCESS_CRASH,
        incident_id=incident_id,
    )

    # Check for repeated failure loop
    if _safe_record(
        detect_repeated_failure_loop,
        sd,
        entity_type="component",
        entity_id=component_name,
        incident_class=IncidentClass.PROCESS_CRASH,
    ):
        _safe_record(
            create_incident,
            sd,
            incident_class=IncidentClass.REPEATED_FAILURE_LOOP,
            entity_type="component",
            entity_id=component_name,
            component_name=component_name,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            failure_detail=(
                f"{component_name} has crashed >= {REPEATED_FAILURE_THRESHOLD} times "
                f"(repeated_failure_loop circuit-breaker)"
            ),
            actor="supervisor_shadow",
            metadata={"triggered_by": incident_id, "component": component_name},
        )

    return incident_id


def record_process_restart_success(
    component_name: str,
    *,
    incident_id: Optional[str] = None,
    pid: Optional[int] = None,
    state_dir: Optional[str | Path] = None,
) -> None:
    """Record that a crashed component was successfully restarted.

    Resolves the open incident and resets the retry budget.
    Shadow mode off → no-op.
    """
    if not is_shadow_mode():
        return

    sd = Path(state_dir) if state_dir else _get_state_dir()
    if sd is None:
        return

    if incident_id:
        _safe_record(resolve_incident, sd, incident_id, actor="supervisor_shadow")

    from incident_log import reset_budget
    _safe_record(
        reset_budget,
        sd,
        entity_type="component",
        entity_id=component_name,
        incident_class=IncidentClass.PROCESS_CRASH,
        actor="supervisor_shadow",
    )


def record_restart_budget_exhausted(
    component_name: str,
    *,
    attempt_count: int,
    max_attempts: int,
    failure_detail: Optional[str] = None,
    state_dir: Optional[str | Path] = None,
) -> None:
    """Record that a component has exhausted its restart budget.

    This mirrors the supervisor's MAX_RESTART_ATTEMPTS logic as an incident trail.
    Shadow mode off → no-op.
    """
    if not is_shadow_mode():
        return

    sd = Path(state_dir) if state_dir else _get_state_dir()
    if sd is None:
        return

    _safe_record(
        create_incident,
        sd,
        incident_class=IncidentClass.REPEATED_FAILURE_LOOP,
        entity_type="component",
        entity_id=component_name,
        component_name=component_name,
        failure_detail=(
            failure_detail
            or f"{component_name} exhausted restart budget ({attempt_count}/{max_attempts})"
        ),
        actor="supervisor_shadow",
        metadata={
            "component": component_name,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "source": "supervisor_budget_exhausted",
        },
    )


def get_shadow_summary(
    state_dir: Optional[str | Path] = None,
) -> Optional[Dict[str, Any]]:
    """Return the current incident summary for operator display.

    Shadow mode off → returns None.
    """
    if not is_shadow_mode():
        return None

    sd = Path(state_dir) if state_dir else _get_state_dir()
    if sd is None:
        return None

    return _safe_record(generate_incident_summary, sd)


# ---------------------------------------------------------------------------
# CLI entrypoint — called from bash bridge
# ---------------------------------------------------------------------------

def _cli() -> None:
    """Minimal CLI for bash bridge invocations.

    Usage:
        python supervisor_shadow.py crash <component> [--pid PID] [--detail DETAIL]
        python supervisor_shadow.py restart_ok <component> [--incident-id ID]
        python supervisor_shadow.py budget_exhausted <component> --attempts N --max N
        python supervisor_shadow.py summary
    """
    import argparse

    parser = argparse.ArgumentParser(description="VNX Supervisor Shadow bridge")
    sub = parser.add_subparsers(dest="cmd")

    p_crash = sub.add_parser("crash")
    p_crash.add_argument("component")
    p_crash.add_argument("--pid", type=int, default=None)
    p_crash.add_argument("--detail", default=None)
    p_crash.add_argument("--dispatch-id", default=None)
    p_crash.add_argument("--terminal-id", default=None)

    p_ok = sub.add_parser("restart_ok")
    p_ok.add_argument("component")
    p_ok.add_argument("--incident-id", default=None)
    p_ok.add_argument("--pid", type=int, default=None)

    p_ex = sub.add_parser("budget_exhausted")
    p_ex.add_argument("component")
    p_ex.add_argument("--attempts", type=int, required=True)
    p_ex.add_argument("--max", type=int, required=True, dest="max_attempts")
    p_ex.add_argument("--detail", default=None)

    sub.add_parser("summary")

    args = parser.parse_args()

    if args.cmd == "crash":
        iid = record_process_crash(
            args.component,
            pid=args.pid,
            failure_detail=args.detail,
            dispatch_id=args.dispatch_id,
            terminal_id=args.terminal_id,
        )
        if iid:
            print(f"incident_id={iid}")
    elif args.cmd == "restart_ok":
        record_process_restart_success(
            args.component,
            incident_id=args.incident_id,
            pid=args.pid,
        )
    elif args.cmd == "budget_exhausted":
        record_restart_budget_exhausted(
            args.component,
            attempt_count=args.attempts,
            max_attempts=args.max_attempts,
            failure_detail=args.detail,
        )
    elif args.cmd == "summary":
        import json
        summary = get_shadow_summary()
        if summary:
            print(json.dumps(summary, indent=2))
        else:
            print('{"shadow_mode": false}')
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    _cli()
