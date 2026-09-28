#!/usr/bin/env python3
"""antipattern_extractor.py — Filtered insert wrapper for antipatterns.

Forward-only noise filter: two gates before persisting any antipattern row:
  1. Skip rows where category == 'memory_consolidation'
  2. Skip rows where title matches 'dispatches: ... success rate'
     (meta_consolidation stats with no actionable prevention value)

Addresses Sonnet audit BLOCKER #2: 26% of antipattern occurrences were
meta_consolidation rows that pollute the failure-prevention signal.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_META_STAT_RE = re.compile(r"dispatches:.*success rate", re.IGNORECASE)

try:
    from pattern_upsert import upsert_antipattern
except ImportError:  # pragma: no cover
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pattern_upsert import upsert_antipattern


def _is_meta_consolidation(category: Optional[str], title: Optional[str]) -> bool:
    """Return True if this antipattern row is meta_consolidation noise."""
    if (category or "").lower() == "memory_consolidation":
        return True
    if _META_STAT_RE.search(title or ""):
        return True
    return False


def insert_filtered_antipattern(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    category: str = "governance",
    pattern_type: str = "approach",
    severity: str = "medium",
    occurrence_count: int = 1,
    source_dispatch_ids: str = "[]",
    why_problematic: Optional[str] = None,
    project_id: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    """Write an antipattern only when category/title pass the consolidation filter.

    The write goes through ``pattern_upsert`` under the natural key
    ``(project_id, pattern_type, title)``: a second write of the same pattern
    folds into the existing row. A NULL/empty ``project_id`` or ``title``
    raises ValueError.

    Returns 1 if written (inserted or merged), 0 if filtered out.
    """
    if _is_meta_consolidation(category, title):
        logger.info(
            "antipattern_extractor: skipped meta_consolidation category=%s title=%s",
            category,
            title,
        )
        return 0

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    upsert_antipattern(
        conn,
        project_id=project_id,
        pattern_type=pattern_type,
        title=title,
        category=category,
        description=description[:500],
        pattern_data=json.dumps({"source": "governance_signal"}),
        why_problematic=(why_problematic or description)[:500],
        severity=severity,
        occurrence_count=occurrence_count,
        source_dispatch_ids=source_dispatch_ids,
        first_seen=now,
        last_seen=now,
    )
    return 1
