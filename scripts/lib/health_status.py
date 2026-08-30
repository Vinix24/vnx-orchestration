"""Shared severity ordering for nested VNX health signals (D2).

Several readers/builders each roll a nested ok/degraded/fail signal into one
summary ``system_health.status``: ``build_t0_state.py``'s
``_build_system_health`` (beacon_health, daemon_liveness) and ``vnx status``'s
session-freshness check. A summary must never report healthier than the
worst thing it summarizes — ``beacon_health.overall == "fail"`` sitting next
to ``status: "healthy"`` in the same object was a structurally possible
outcome of the code, not a fluke (measured 2026-08-30 in production
t0_state.json). This is the one place the aggregation rule lives, so every
caller applies it the same way instead of re-deriving its own threshold.
"""
from __future__ import annotations

from typing import Optional

_SEVERITY = {
    "healthy": 0, "ok": 0,
    "degraded": 1, "stale": 1, "fail": 1,
    "failed": 2,
}
_BY_SEVERITY = {0: "healthy", 1: "degraded", 2: "failed"}


def worst_status(current: str, *nested: Optional[str]) -> str:
    """Aggregate ``current`` with any number of nested status/overall values.

    A value absent from the severity table (``None``, ``"unknown"``, or any
    other unrecognized string) is intentionally excluded from the max: an
    unmeasured nested signal suspends judgment, it does not assert a
    severity. Only values ``worst_status`` recognizes as a real signal can
    drag the aggregate down.

    Note ``"fail"`` (the nested-signal vocabulary word used by
    ``beacon_health.overall`` / ``daemon_liveness.overall``) floors at
    ``"degraded"``, one tier below the top-level status word ``"failed"``
    (reserved for db-level corruption, R6.1). A beacon or a daemon being
    down is real and must not report ``healthy``, but it is not the same
    severity as the database itself being unreadable.
    """
    worst = _SEVERITY.get(current, 0)
    for value in nested:
        if value in _SEVERITY:
            worst = max(worst, _SEVERITY[value])
    return _BY_SEVERITY[worst]


__all__ = ["worst_status"]
