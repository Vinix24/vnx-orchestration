"""classifier.py — Re-export of smart_router public API.

New canonical import: from providers.smart_router.classifier import classify_task, decide
Old import (backward compat, 90-day alias): from smart_router import classify_task, decide

Routing YAML files live alongside smart_router.py in scripts/lib/providers/:
  - routing_recommendations.yaml
  - routing_policy.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parents[2])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from smart_router import (  # noqa: F401, E402
    ROLE_TO_TASK_CLASS,
    TASK_CLASSES,
    RouteCandidate,
    RouteDecision,
    _cost_aware_sort_key,
    _filter_by_constraints,
    _load_recommendations,
    classify_task,
    decide,
    parse_route_model_id,
    recommend,
    write_route_decision,
)

__all__ = [
    "classify_task",
    "decide",
    "recommend",
    "parse_route_model_id",
    "write_route_decision",
    "RouteCandidate",
    "RouteDecision",
    "TASK_CLASSES",
    "ROLE_TO_TASK_CLASS",
    "_cost_aware_sort_key",
    "_filter_by_constraints",
    "_load_recommendations",
]
