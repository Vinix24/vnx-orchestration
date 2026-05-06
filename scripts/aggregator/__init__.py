"""Read-only federation aggregator for VNX multi-project deployments.

Phase 6 P1 of the single-VNX migration plan. Attaches all registered
project DBs in `?mode=ro` and materializes a unified view DB at
`~/.vnx-aggregator/data.db`. Reversible: operator can `rm -rf
~/.vnx-aggregator/` at any point with zero data loss.

See `claudedocs/2026-04-30-single-vnx-migration-plan.md` Sections 0.1
and 4.3 for context.
"""

from __future__ import annotations

DEFAULT_AGGREGATOR_DIR = "~/.vnx-aggregator"
DEFAULT_REGISTRY_PATH = "~/.vnx/projects.json"
DEFAULT_AGGREGATOR_DB = "data.db"

__all__ = [
    "DEFAULT_AGGREGATOR_DIR",
    "DEFAULT_REGISTRY_PATH",
    "DEFAULT_AGGREGATOR_DB",
]
