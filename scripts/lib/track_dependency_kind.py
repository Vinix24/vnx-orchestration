#!/usr/bin/env python3
"""track_dependency_kind.py — which track_dependencies edges block.

One helper for every reader that turns a dependency edge into "blocked":
track_reconciler (blocking detail), track_reconciler_status (derived status),
track_reconciler_closure (gh-evidence close revalidation) and planning_cli
(drift reason).

Only a ``hard`` edge blocks. ``soft`` and ``overlap`` are advice. A kind that
is none of the three (NULL, or an old value in a store with an older schema)
counts as ``hard``: fail-closed, with a warning. vnx-dev cannot produce that
case (``kind`` is NOT NULL with a CHECK); older project stores might.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

KIND_HARD = "hard"
KIND_SOFT = "soft"
KIND_OVERLAP = "overlap"
ADVISORY_KINDS = frozenset({KIND_SOFT, KIND_OVERLAP})


def dependency_blocks(
    kind: Optional[str],
    *,
    from_track_id: str = "",
    from_project_id: str = "",
    to_track_id: str = "",
    to_project_id: str = "",
) -> bool:
    """True when an unfinished target of this edge kind blocks the source track."""
    if kind == KIND_HARD:
        return True
    if kind in ADVISORY_KINDS:
        return False
    logger.warning(
        "track_dependencies: unrecognized kind %r on edge (%s, %s) -> (%s, %s); "
        "treated as hard (fail-closed)",
        kind, from_track_id, from_project_id, to_track_id, to_project_id,
    )
    return True
