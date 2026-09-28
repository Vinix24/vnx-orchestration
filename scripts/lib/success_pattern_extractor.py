#!/usr/bin/env python3
"""success_pattern_extractor.py — Filtered insert wrapper for success_patterns.

Forward-only noise filter: applies _is_governance_event() before persisting
any success_pattern row. Prevents governance-event noise (gate X passed,
Recent dispatch lines) from entering the catalogue.

Addresses Sonnet audit BLOCKER #2: 81.6% (164/201 rows) of success_patterns
were gate-pass events with zero signal value for dispatch intelligence.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pattern_dedup import _is_governance_event
    from pattern_upsert import upsert_success_pattern
except ImportError:  # pragma: no cover
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pattern_dedup import _is_governance_event
    from pattern_upsert import upsert_success_pattern


def insert_filtered_success_pattern(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    category: str = "governance",
    pattern_type: str = "approach",
    confidence_score: float = 0.55,
    usage_count: int = 1,
    source_dispatch_ids: str = "[]",
    project_id: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    """Write a success_pattern only when title passes the governance filter.

    The write goes through ``pattern_upsert`` under the natural key
    ``(project_id, pattern_type, title)``: a second write of the same pattern
    folds into the existing row. A NULL/empty ``project_id`` or ``title``
    raises ValueError.

    Returns 1 if written (inserted or merged), 0 if filtered out.
    """
    if _is_governance_event(title):
        return 0

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    upsert_success_pattern(
        conn,
        project_id=project_id,
        pattern_type=pattern_type,
        title=title,
        category=category,
        description=description[:500],
        pattern_data=json.dumps({"source": "governance_signal"}),
        confidence_score=confidence_score,
        usage_count=usage_count,
        source_dispatch_ids=source_dispatch_ids,
        first_seen=now,
        last_used=now,
    )
    return 1
