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
    from pattern_dedup import _is_governance_event, _column_exists
except ImportError:  # pragma: no cover
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pattern_dedup import _is_governance_event, _column_exists


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
    """Insert a success_pattern row only when title passes the governance filter.

    Returns 1 if inserted, 0 if filtered out.
    """
    if _is_governance_event(title):
        return 0

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    has_project = _column_exists(conn, "success_patterns", "project_id")

    if has_project and project_id is not None:
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, "
            " first_seen, last_used, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pattern_type, category, title, description[:500],
                json.dumps({"source": "governance_signal"}),
                confidence_score, usage_count, source_dispatch_ids,
                now, now, project_id,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pattern_type, category, title, description[:500],
                json.dumps({"source": "governance_signal"}),
                confidence_score, usage_count, source_dispatch_ids, now, now,
            ),
        )
    return 1
