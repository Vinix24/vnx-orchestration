"""smart_router — Smart router submodule re-exports.

Canonical import: from providers.smart_router import classify_task, decide, route_dispatch
Backward-compat: from smart_router import classify_task, decide

PR-2 additions: classify_dispatch(), TierRoute, resolve_tier_route(), route_dispatch().
route_dispatch() is default-on since 2026-08-02; disable with VNX_SMART_ROUTER_DISABLE=1.
"""
from __future__ import annotations

import os as _os
from typing import Optional

from .classifier import (  # noqa: F401
    RouteCandidate,
    RouteDecision,
    classify_task,
    decide,
    parse_route_model_id,
    recommend,
    write_route_decision,
)
from .cost_tier import classify_dispatch  # noqa: F401
from .tier_routing import TierRoute, resolve_tier_route  # noqa: F401

__all__ = [
    "classify_task",
    "decide",
    "recommend",
    "parse_route_model_id",
    "write_route_decision",
    "RouteCandidate",
    "RouteDecision",
    "classify_dispatch",
    "TierRoute",
    "resolve_tier_route",
    "route_dispatch",
]


def route_dispatch(
    task_spec: dict,
    file_paths: Optional[list] = None,
    loc_estimate: int = 0,
    env: Optional[dict] = None,
) -> Optional[TierRoute]:
    """Smart router entry point. Default-on since 2026-08-02.

    Tier-low routing (deepseek-v4-flash, codex fallback) is active by default.
    Operators can disable it with VNX_SMART_ROUTER_DISABLE=1. The old VNX_AUTO_ROUTE
    opt-in flag is still honoured for backward compat but is superseded.

    When active: classifies via classify_dispatch() and resolves a TierRoute via
    resolve_tier_route(). Returns None when the router is disabled or when
    classification fails unexpectedly (fail-open).
    """
    _env = env if env is not None else dict(_os.environ)

    # Operator opt-out: VNX_SMART_ROUTER_DISABLE=1 suppresses the router entirely.
    if _env.get("VNX_SMART_ROUTER_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    # Backward compat: VNX_AUTO_ROUTE=0/false/off explicitly disables, matching the
    # old default-off contract. Any other value (including unset) → router runs.
    auto_route = _env.get("VNX_AUTO_ROUTE", "").strip().lower()
    if auto_route in ("0", "false", "no", "off"):
        return None

    try:
        tier = classify_dispatch(task_spec, file_paths or [], loc_estimate)
        return resolve_tier_route(tier, _env)
    except Exception:
        # Fail-open: a broken classifier must never block a dispatch.
        return None
