"""pool_scaling_policies — Pluggable scaling policies for elastic worker pools.

Policy interface: pure function (config, state, members) -> target_size.
Registered policies looked up by name in decide().
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure scripts/lib is importable from within this sub-package.
_LIB_DIR = str(Path(__file__).resolve().parent.parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from .queue_depth_v1 import queue_depth_v1  # noqa: E402
from .cost_aware_v1 import cost_aware_v1    # noqa: E402

POLICIES = {
    "fixed": lambda cfg, st, m: cfg.min_workers,
    "queue_depth_v1": queue_depth_v1,
    "queue_aware": queue_depth_v1,   # backward-compat alias for PR-6.3
    "cost_aware_v1": cost_aware_v1,
}

__all__ = ["POLICIES", "queue_depth_v1", "cost_aware_v1"]
