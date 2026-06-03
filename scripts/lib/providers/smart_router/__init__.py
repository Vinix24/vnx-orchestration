"""smart_router — Pip-installable smart router submodule re-exports.

New canonical import: from providers.smart_router.classifier import classify_task, decide
Old import (backward compat): from smart_router import classify_task, decide
"""
from .classifier import (  # noqa: F401
    RouteCandidate,
    RouteDecision,
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
]
