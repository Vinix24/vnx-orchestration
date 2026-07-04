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

Range contract (D1, 2026-07-04):
  ``pattern_usage.confidence`` is an UNCLAMPED accumulator: the learning_loop
  boost path pushes it to 2.0 (``learning_loop.py:286``).  Readers of that
  column (``_aggregate_for_pattern`` legacy fallback and
  ``recommendation_aggregator._read_confidence_trends``) see the raw value.
  Clamping to [0.0, 1.0] happens ONLY at the write boundary to
  ``success_patterns.confidence_score`` — inside this module.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# Reconcile cache TTL (seconds) for the selector-side fallback safety net.
RECONCILE_CACHE_TTL_SECONDS = 300

# pattern_usage.pattern_id prefix that maps onto success_patterns rows.
SUCCESS_PATTERN_PREFIX = "intel_sp_"


def _recency_decay(confidence: float, last_used: datetime) -> float:
    """Decay confidence by 0.95^weeks since last_used. Floor 0.1.

    A pattern unused for 8 weeks decays to ~0.66× its beta score.
    After ~29 weeks the floor of 0.1 kicks in, preventing full suppression.
    """
    weeks = (datetime.utcnow() - last_used).days / 7.0
    decayed = confidence * (0.95 ** weeks)
    return max(decayed, 0.1)


def _parse_last_used(raw: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 or SQLite datetime string into a naive UTC datetime.

    Uses fromisoformat (Python 3.11+) which correctly handles timezone offsets
    and Z suffix — raw[:26] truncation silently dropped offsets like +05:30.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


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
        # Range contract: the raw conf may exceed 1.0 (learning_loop boost
        # to 2.0).  Clamp here — this is the write-boundary for
        # success_patterns.confidence_score.  Readers of pattern_usage.confidence
        # must NOT clamp; they see the raw accumulator.
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
            "SELECT id, confidence_score, last_used FROM success_patterns"
        ).fetchall()

        updated = 0
        for sp_id, current_score, last_used_raw in rows:
            agg = _aggregate_for_pattern(conn, int(sp_id))
            if agg is None:
                continue
            beta = float(agg[0])
            last_used_dt = _parse_last_used(last_used_raw)
            if last_used_dt is not None:
                beta = _recency_decay(beta, last_used_dt)
            new_score = round(beta, 6)
            # Range contract: beta_score() + recency_decay() always yield
            # [0.0, 1.0]; the legacy fallback clamps before returning.
            # Assert here so any future writer that breaks the invariant is
            # caught at the single write boundary rather than silently
            # polluting success_patterns with an out-of-range score.
            assert 0.0 <= new_score <= 1.0, (
                f"confidence_score out of range before write: "
                f"{new_score!r} for sp_id={sp_id}"
            )
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
