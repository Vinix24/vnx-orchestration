"""smart_router — Smart router submodule re-exports.

Canonical import: from providers.smart_router import classify_task, decide, route_dispatch
Backward-compat: from smart_router import classify_task, decide

PR-2 additions: classify_dispatch(), TierRoute, resolve_tier_route(), route_dispatch().
route_dispatch() is default-off; returns None unless VNX_AUTO_ROUTE=1.
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
    """Smart router entry point. Returns None when VNX_AUTO_ROUTE is unset.

    Default-off per memory smart-router-built-not-operative: the router is built
    but not operative until VNX_AUTO_ROUTE=1 is set. Callers fall back to the
    existing dispatch path on None return.

    When VNX_AUTO_ROUTE=1: classifies via classify_dispatch() and resolves a
    TierRoute via resolve_tier_route().
    """
    _env = env if env is not None else dict(_os.environ)
    # Audit D7: a bare truthiness check treated VNX_AUTO_ROUTE=0/false as ENABLING (any non-empty
    # string is truthy). Honour only the canonical truthy values, matching the docstring.
    if _env.get("VNX_AUTO_ROUTE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    tier = classify_dispatch(task_spec, file_paths or [], loc_estimate)
    return resolve_tier_route(tier, _env)
