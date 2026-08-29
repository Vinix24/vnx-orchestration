"""decision_log.py — Observation ledger for every smart-router decision (OI-1494).

The router has been default-on since 2026-08-02 — no kill-switch is set anywhere — while
every per-tier enable flag and the canary sat at 0. ``staging.staging_verdict`` declines
before ``tier_routing.resolve_tier_route`` is ever reached, so the route the router WOULD
have chosen was not merely unused: it was never computed, and nothing about the decision
was written down.

That is a rollout with no observation layer. The staging design ramps a tier up on
evidence, but no evidence can accumulate while the declined half of the split records
nothing — so the flags stay at 0 because there is nothing to justify raising them, and
there is nothing to justify raising them because the flags are at 0.

This module breaks that loop. Every decision the door makes is appended here: the applied
ones AND the declined ones, each carrying the provider, model and lane the router settled
on. The dispatch is unaffected — a declined dispatch still follows the legacy lane, byte
for byte as before. The router observes; it does not act.

Recording is ON by default, with ``VNX_ROUTER_DECISION_LOG_DISABLE`` as the kill-switch.
That direction is deliberate: an observation layer behind an opt-in flag that nobody sets
observes nothing, which is the exact shape of the bug this closes.

Writing is fail-open throughout — a broken ledger must never block a dispatch. The append
follows the fcntl-locked NDJSON contract of ``decision_shadow._atomic_append`` (exclusive
flock on a sentinel, then a plain append under a second flock). Deliberately NOT the
fixed-name ``<path>.tmp`` + ``os.replace`` form: two writers sharing one tmp name lose
records (OI-1486). There is no shared NDJSON-append helper in scripts/lib — some twenty
modules each carry their own copy of this primitive — so this follows the decision_shadow
copy verbatim to keep a later consolidation a grep rather than a search.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROUTER_DECISION_LEDGER = "router_decisions.ndjson"

# Kill-switch, not an opt-in: see the module docstring on why the direction matters.
DISABLE_FLAG = "VNX_ROUTER_DECISION_LOG_DISABLE"

_TRUTHY = ("1", "true", "yes", "on")


def recording_enabled(env: Optional[dict] = None) -> bool:
    """True unless the kill-switch is set. Default ON."""
    _env = os.environ if env is None else env
    return str(_env.get(DISABLE_FLAG, "")).strip().lower() not in _TRUTHY


def _state_dir(state_dir: "str | Path | None") -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from vnx_paths import resolve_state_dir  # noqa: PLC0415

    return resolve_state_dir()


def ledger_path(state_dir: "str | Path | None" = None) -> Path:
    """Absolute path of the router-decision ledger for this project's state dir."""
    return _state_dir(state_dir) / ROUTER_DECISION_LEDGER


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_append(path: Path, record: Dict[str, Any], lock_name: str) -> None:
    """fcntl-locked NDJSON append (same contract as decision_shadow/append_receipt)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = path.parent / lock_name
    with sentinel.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")


def record_router_decision(
    *,
    tier: Optional[str],
    applied: bool,
    decline_reason: Optional[str],
    would_route: Optional[Dict[str, Any]],
    target_slot: str,
    dispatch_id: Optional[str] = None,
    compute_error: Optional[str] = None,
    state_dir: "str | Path | None" = None,
    env: Optional[dict] = None,
    ts: Optional[str] = None,
) -> bool:
    """Append one router decision to the ledger. Returns whether a record was written.

    ``applied`` separates the two halves of the trail: True means the door acted on
    ``would_route``, False means it computed the route and followed the legacy lane
    anyway. ``compute_error`` is set when the route could not be computed at all — an
    observation failure, which is recorded rather than raised.

    Never raises. A ledger that cannot be written is an observability problem; a dispatch
    that dies because of one is an outage.
    """
    if not recording_enabled(env):
        return False
    try:
        record = {
            "event": "router_decision",
            "ts": ts or _now_iso(),
            "dispatch_id": dispatch_id,
            "target_slot": target_slot,
            "tier": tier,
            "applied": applied,
            "decline_reason": decline_reason,
            "would_route": would_route,
            "compute_error": compute_error,
            "note": (
                "OBSERVED — the router computed this route and acted on it"
                if applied
                else "OBSERVED — the router computed this route and did NOT act on it"
            ),
        }
        _atomic_append(
            ledger_path(state_dir), record, ROUTER_DECISION_LEDGER + ".lock"
        )
        return True
    except Exception:  # noqa: BLE001 — observation must never break the decision path
        return False
