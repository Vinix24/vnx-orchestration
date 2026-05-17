"""Runtime env-var timeout overrides — shared between delivery + recovery."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


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
