#!/usr/bin/env python3
"""Reconcile pattern_usage learning state into success_patterns.confidence_score.

Closes the open-circuit feedback loop documented in
``claudedocs/2026-04-30-self-learning-loop-audit.md``:

  - ``intelligence_selector`` reads ``success_patterns.confidence_score``.
  - ``learning_loop`` and ``update_confidence_from_outcome`` write to
    ``pattern_usage`` (and previously to ``success_patterns`` with fixed
    +0.05 / -0.1 deltas that ignored prior usage volume).

Linkage between the two tables is the stable item-id convention used by
``intelligence_selector._stable_item_id``: a ``success_patterns`` row with
``id = N`` corresponds to a ``pattern_usage`` row with
``pattern_id = "intel_sp_<N>"``.

The Beta(alpha, beta) score with Laplace smoothing
``score = (success_count + 1) / (success_count + failure_count + 2)`` is the
canonical confidence used by both the per-dispatch updater and the periodic
reconciler.  It naturally weights by usage volume: a pattern with
8 successes / 2 failures resolves to ``9 / 12 = 0.75`` while a single bad
outcome moves only from the 0.5 prior to ``1 / 3 = 0.333``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional, Tuple

# Reconcile cache TTL (seconds) for the selector-side fallback safety net.
RECONCILE_CACHE_TTL_SECONDS = 300

# pattern_usage.pattern_id prefix that maps onto success_patterns rows.
SUCCESS_PATTERN_PREFIX = "intel_sp_"


def beta_score(success_count: int, failure_count: int) -> float:
    """Beta posterior with Laplace smoothing: (s+1) / (s+f+2).

    Returns 0.5 when both counts are zero (uniform prior).
    """
    s = max(0, int(success_count or 0))
    f = max(0, int(failure_count or 0))
    return (s + 1) / (s + f + 2)


def _aggregate_for_pattern(
    conn: sqlite3.Connection,
    success_pattern_id: int,
) -> Optional[Tuple[float, int, int, int]]:
    """Return (new_score, used_count, success_count, failure_count) or None.

    None means "no usage data — caller must keep the current score".
    """
    pattern_id = f"{SUCCESS_PATTERN_PREFIX}{success_pattern_id}"
    row = conn.execute(
        """
        SELECT used_count, success_count, failure_count, confidence
        FROM pattern_usage
        WHERE pattern_id = ?
        """,
        (pattern_id,),
    ).fetchone()
    if row is None:
        return None

    used = int(row[0] or 0)
    succ = int(row[1] or 0)
    fail = int(row[2] or 0)
    conf = float(row[3] if row[3] is not None else 0.0)

    if succ + fail > 0:
        return beta_score(succ, fail), used, succ, fail

    if used > 0:
        # Older rows that pre-date success_count/failure_count tracking
        # still carry a confidence value updated by the legacy decay/boost
        # path.  Treat that as a single weighted sample.
        return max(0.0, min(1.0, conf)), used, succ, fail

    return None


def reconcile_pattern_confidence(db_path: Path) -> int:
    """Sync pattern_usage learning state into success_patterns.confidence_score.

    For each ``success_patterns`` row, look up the matching ``pattern_usage``
    row (``pattern_id = "intel_sp_<id>"``).  If usage data exists, recompute
    the confidence score via Beta-Laplace smoothing and write it back.  If
    no usage data exists the existing ``confidence_score`` is preserved.

    Idempotent: a second invocation with no new usage data is a no-op.

    Returns the number of ``success_patterns`` rows whose
    ``confidence_score`` was updated.
    """
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, confidence_score FROM success_patterns"
        ).fetchall()

        updated = 0
        for sp_id, current_score in rows:
            agg = _aggregate_for_pattern(conn, int(sp_id))
            if agg is None:
                continue
            new_score = round(float(agg[0]), 6)
            current = float(current_score or 0.0)
            if abs(new_score - current) < 1e-6:
                continue
            conn.execute(
                "UPDATE success_patterns SET confidence_score = ? WHERE id = ?",
                (new_score, sp_id),
            )
            updated += 1

        conn.commit()
        return updated
    finally:
        conn.close()


def maybe_reconcile(
    db_path: Path,
    state_dir: Optional[Path] = None,
    ttl_seconds: int = RECONCILE_CACHE_TTL_SECONDS,
) -> bool:
    """Run reconcile if the last reconcile happened more than ``ttl_seconds`` ago.

    Used as a safety net at injection time so the selector never reads stale
    confidence scores even if the daily ``learning_loop`` cron has not run.
    The timestamp is cached in
    ``<state_dir>/.last_confidence_reconcile_ts``.

    Returns ``True`` if reconcile was executed.
    """
    if not db_path.exists():
        return False
    if state_dir is None:
        state_dir = db_path.parent

    ts_file = state_dir / ".last_confidence_reconcile_ts"
    now = time.time()

    if ts_file.exists():
        try:
            last = float(ts_file.read_text().strip())
            if now - last < ttl_seconds:
                return False
        except (OSError, ValueError):
            pass

    reconcile_pattern_confidence(db_path)

    try:
        ts_file.write_text(str(now))
    except OSError:
        pass

    return True
