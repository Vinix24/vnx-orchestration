"""Runtime env-var timeout overrides — shared between delivery + recovery."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Complexity-scaled timeout defaults.
#
# A subprocess dispatch was killed by the per-chunk timeout (300s) while a
# worker was doing legitimate compute-heavy work (static analysis for a
# schema-drift test) without emitting a tool-event for >300s. The per-chunk
# timeout fires when no event arrives within the window, so it mis-fires on
# quiet-but-working compute. Scaling the base defaults by --complexity gives
# heavy dispatches more headroom while leaving low/medium unchanged.
#
# Precedence is preserved by apply_runtime_overrides running AFTER these values
# are seeded as the base: VNX_CHUNK_TIMEOUT / VNX_TOTAL_DEADLINE env overrides
# still win over the complexity-scaled default, which in turn wins over the
# function-signature base default.
_COMPLEXITY_TIMEOUTS: dict[str, tuple[float, float]] = {
    "low": (300.0, 900.0),
    "medium": (300.0, 900.0),
    "high": (600.0, 1800.0),
}
_BASE_COMPLEXITY_TIMEOUTS: tuple[float, float] = (300.0, 900.0)


def complexity_timeout_defaults(complexity: str) -> tuple[float, float]:
    """Return (chunk_timeout, total_deadline) scaled by dispatch complexity.

    low/medium keep the historical base defaults (300s / 900s); high gets more
    headroom (600s / 1800s) so compute-heavy workers that go quiet during a long
    analysis step are not killed prematurely by the per-chunk timeout.

    Unknown or None values fall back to the base defaults so an unexpected
    --complexity value never shrinks the headroom below the historical baseline.
    """
    return _COMPLEXITY_TIMEOUTS.get((complexity or "").lower(), _BASE_COMPLEXITY_TIMEOUTS)


def apply_runtime_overrides(chunk_timeout: float, total_deadline: float) -> tuple[float, float]:
    """Honor VNX_CHUNK_TIMEOUT / VNX_TOTAL_DEADLINE env overrides.

    Returns: (chunk_timeout, total_deadline) with any env-var overrides applied.
    Silently ignores missing or non-float values.
    """
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
        logger.info("runtime_overrides: applied VNX_CHUNK_TIMEOUT -> %s", chunk_timeout)
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
        logger.info("runtime_overrides: applied VNX_TOTAL_DEADLINE -> %s", total_deadline)
    except (KeyError, ValueError):
        pass
    return chunk_timeout, total_deadline
