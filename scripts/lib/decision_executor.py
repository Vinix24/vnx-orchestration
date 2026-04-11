#!/usr/bin/env python3
"""Decision executor — execute parsed T0 decisions from the headless loop.

Routes by decision type (DISPATCH / WAIT / COMPLETE / REJECT / ESCALATE)
and enforces loop guards:
  - MAX_DISPATCHES_PER_CYCLE: hard cap on dispatches per invocation cycle.
  - Duplicate detection: same dispatch_task within 30 min → refuse.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

MAX_DISPATCHES_PER_CYCLE = 3

# Duplicate window: 30 minutes in seconds
_DUPLICATE_WINDOW_SECONDS = 1800

# Terminal → track mapping
_TERMINAL_TO_TRACK: dict[str, str] = {
    "T1": "A",
    "T2": "B",
    "T3": "C",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_hash(task_text: str) -> str:
    return hashlib.sha256(task_text.encode("utf-8")).hexdigest()[:16]


def _load_recent_hashes(state_dir: Path) -> dict[str, str]:
    """Load recent_dispatch_hashes.json → {hash: iso_timestamp}."""
    path = state_dir / "recent_dispatch_hashes.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_recent_hashes(state_dir: Path, hashes: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "recent_dispatch_hashes.json"
    path.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")


def _purge_expired_hashes(hashes: dict[str, str]) -> dict[str, str]:
    """Remove entries older than _DUPLICATE_WINDOW_SECONDS."""
    now_ts = datetime.now(timezone.utc).timestamp()
    result = {}
    for h, ts in hashes.items():
        try:
            age = now_ts - datetime.fromisoformat(ts).timestamp()
            if age < _DUPLICATE_WINDOW_SECONDS:
                result[h] = ts
        except Exception:
            pass
    return result


def _log_decision_event(state_dir: Path, event: dict[str, Any]) -> None:
    events_dir = state_dir.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / "t0_decisions.ndjson"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _get_event_store(state_dir: Path) -> Any | None:
    """Import EventStore from the lib directory if available."""
    lib_dir = Path(__file__).parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    try:
        from event_store import EventStore  # type: ignore[import]
        events_dir = state_dir.parent / "events"
        return EventStore(events_dir=events_dir)
    except Exception:
        return None


def _get_dispatch_writer():
    """Import write_dispatch and generate_dispatch_id from headless_dispatch_writer."""
    lib_dir = Path(__file__).parent
    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))
    from headless_dispatch_writer import write_dispatch, generate_dispatch_id  # type: ignore[import]
    return write_dispatch, generate_dispatch_id


# ---------------------------------------------------------------------------
# Dispatch counter (per-process cycle guard)
# ---------------------------------------------------------------------------

_cycle_dispatch_count: int = 0


def reset_cycle_counter() -> None:
    """Reset the per-cycle dispatch counter. Call at the start of each trigger cycle."""
    global _cycle_dispatch_count
    _cycle_dispatch_count = 0


# ---------------------------------------------------------------------------
# Decision handlers
# ---------------------------------------------------------------------------

def _handle_dispatch(
    decision: dict[str, Any],
    trigger_reason: str,
    *,
    state_dir: Path,
    dry_run: bool,
) -> str:
    global _cycle_dispatch_count

    dispatch_target = str(decision.get("dispatch_target", "")).upper()
    dispatch_task = str(decision.get("dispatch_task", "")).strip()
    role = str(decision.get("role", "backend-developer")).strip() or "backend-developer"

    # Validate target
    if dispatch_target not in _TERMINAL_TO_TRACK:
        msg = f"Invalid dispatch_target {dispatch_target!r}; must be T1/T2/T3"
        _LOG.warning(msg)
        return f"error: {msg}"

    # Validate task
    if not dispatch_task:
        msg = "dispatch_task is empty — refusing dispatch"
        _LOG.warning(msg)
        return f"error: {msg}"

    # Cycle guard
    if _cycle_dispatch_count >= MAX_DISPATCHES_PER_CYCLE:
        msg = f"MAX_DISPATCHES_PER_CYCLE ({MAX_DISPATCHES_PER_CYCLE}) reached — refusing dispatch"
        _LOG.warning(msg)
        return f"error: {msg}"

    # Duplicate detection
    h = _task_hash(dispatch_task)
    hashes = _purge_expired_hashes(_load_recent_hashes(state_dir))
    if h in hashes:
        msg = f"Duplicate dispatch_task (hash={h}) within {_DUPLICATE_WINDOW_SECONDS}s window — refusing"
        _LOG.warning(msg)
        return f"duplicate: {msg}"

    track = _TERMINAL_TO_TRACK[dispatch_target]
    feature = str(decision.get("feature", "")).strip() or None
    pr_id = str(decision.get("pr_id", "")).strip() or None

    if dry_run:
        _LOG.info("[dry-run] Would dispatch to %s (track %s): %.80s…", dispatch_target, track, dispatch_task)
        return "dry-run dispatch"

    try:
        write_dispatch, generate_dispatch_id = _get_dispatch_writer()
        dispatch_id = generate_dispatch_id(
            prefix=f"t0-auto-{dispatch_target.lower()}",
            track=track,
        )
        dispatch_path = write_dispatch(
            dispatch_id=dispatch_id,
            terminal=dispatch_target,
            track=track,
            role=role,
            instruction=dispatch_task,
            feature=feature,
            pr_id=pr_id,
        )
        _LOG.info("Dispatch written: %s → %s", dispatch_id, dispatch_path)

        # Record hash
        hashes[h] = _now_utc()
        _save_recent_hashes(state_dir, hashes)

        # Increment cycle counter
        _cycle_dispatch_count += 1

        # Log event
        event_store = _get_event_store(state_dir)
        event = {
            "event_type": "t0_dispatch",
            "dispatch_id": dispatch_id,
            "dispatch_target": dispatch_target,
            "trigger_reason": trigger_reason,
            "task_hash": h,
            "timestamp": _now_utc(),
        }
        if event_store:
            try:
                event_store.append("T0", event, dispatch_id=dispatch_id)
            except Exception:
                pass
        _log_decision_event(state_dir, event)

        return "dispatched"
    except Exception as exc:
        _LOG.error("write_dispatch failed: %s", exc)
        return f"error: {exc}"


def _handle_wait(decision: dict[str, Any], state_dir: Path) -> str:
    reason = str(decision.get("reason", "no reason given"))
    _LOG.info("T0 WAIT: %s", reason)
    _log_decision_event(state_dir, {
        "event_type": "t0_wait",
        "reason": reason,
        "timestamp": _now_utc(),
    })
    return "waited"


def _handle_complete(decision: dict[str, Any], state_dir: Path) -> str:
    reason = str(decision.get("reason", ""))
    _LOG.info("T0 COMPLETE: %s", reason)
    _log_decision_event(state_dir, {
        "event_type": "t0_complete",
        "reason": reason,
        "timestamp": _now_utc(),
    })
    return "completed"


def _handle_reject(decision: dict[str, Any], state_dir: Path) -> str:
    reason = str(decision.get("reason", ""))
    _LOG.info("T0 REJECT: %s", reason)
    _log_decision_event(state_dir, {
        "event_type": "t0_reject",
        "reason": reason,
        "timestamp": _now_utc(),
    })
    return "rejected"


def _handle_escalate(decision: dict[str, Any], state_dir: Path, dry_run: bool) -> str:
    reason = str(decision.get("reason", ""))
    _LOG.info("T0 ESCALATE: %s", reason)

    if not dry_run:
        escalations_dir = state_dir.parent / "state" / "escalations"
        # Handle case where state_dir IS the state dir
        alt = state_dir / "escalations"
        if not (state_dir.parent / "state").exists():
            escalations_dir = alt
        escalations_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        esc_path = escalations_dir / f"escalation_{ts}.json"
        esc_path.write_text(
            json.dumps({
                "timestamp": _now_utc(),
                "decision": decision,
                "reason": reason,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        _LOG.info("Escalation written to %s", esc_path)

    _log_decision_event(state_dir, {
        "event_type": "t0_escalate",
        "reason": reason,
        "timestamp": _now_utc(),
    })
    return "escalated"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_decision(
    decision: dict[str, Any],
    trigger_reason: str,
    *,
    state_dir: Path,
    dry_run: bool = False,
) -> str:
    """Execute a parsed T0 decision. Returns a status string.

    Args:
        decision:       Parsed decision dict with at minimum a 'decision' key.
        trigger_reason: Human-readable reason for the trigger (e.g. 'new_report').
        state_dir:      VNX state directory (contains recent_dispatch_hashes.json).
        dry_run:        If True, log but do not write files or call dispatch writer.

    Returns:
        One of: 'dispatched', 'waited', 'completed', 'rejected', 'escalated',
        'dry-run dispatch', 'duplicate: …', 'error: …'
    """
    decision_type = str(decision.get("decision", "UNKNOWN")).upper()
    _LOG.debug("execute_decision: type=%s trigger=%s dry_run=%s", decision_type, trigger_reason, dry_run)

    if decision_type == "DISPATCH":
        return _handle_dispatch(decision, trigger_reason, state_dir=state_dir, dry_run=dry_run)
    elif decision_type == "WAIT":
        return _handle_wait(decision, state_dir)
    elif decision_type == "COMPLETE":
        return _handle_complete(decision, state_dir)
    elif decision_type == "REJECT":
        return _handle_reject(decision, state_dir)
    elif decision_type == "ESCALATE":
        return _handle_escalate(decision, state_dir, dry_run)
    else:
        _LOG.warning("Unknown decision type %r — no action taken", decision_type)
        _log_decision_event(state_dir, {
            "event_type": "t0_unknown_decision",
            "raw_decision": decision,
            "timestamp": _now_utc(),
        })
        return f"unknown decision type: {decision_type}"
