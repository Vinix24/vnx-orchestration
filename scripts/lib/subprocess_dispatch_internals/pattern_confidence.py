"""pattern_confidence — capture/outcome + post-dispatch confidence feedback loop."""

from __future__ import annotations

import logging
import sqlite3 as _sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .git_helpers import _count_lines_changed_since_sha

logger = logging.getLogger(__name__)


def _capture_dispatch_parameters(
    dispatch_id: str,
    instruction: str,
    terminal_id: str,
    model: str,
    role: str | None,
    repo_map: str | None,
) -> None:
    """Capture DispatchParameters to dispatch_tracker.db. Never raises."""
    try:
        from dispatch_parameter_tracker import (
            DispatchParameterTracker,
            extract_parameters,
        )
        params = extract_parameters(
            instruction=instruction,
            terminal_id=terminal_id,
            model=model,
            role=role,
            repo_map=repo_map,
        )
        tracker = DispatchParameterTracker()
        tracker.capture_parameters(dispatch_id, params)
        logger.debug(
            "Parameter capture: dispatch=%s chars=%d ctx=%d role=%s",
            dispatch_id,
            params.instruction_char_count,
            params.context_item_count,
            params.role,
        )
    except Exception as exc:
        logger.debug("Parameter capture failed for %s: %s", dispatch_id, exc)


def _capture_dispatch_outcome(
    dispatch_id: str,
    success: bool,
    start_ts: str,
    committed: bool,
    pre_sha: str = "",
    manifest_paths: "list[str] | None" = None,
) -> None:
    """Capture DispatchOutcome after completion. Never raises.

    When pre_sha is provided (CFX-1), lines_changed is computed via
    HEAD-comparison against the pre-dispatch SHA — restricted to manifest_paths
    when supplied — so concurrent unrelated commits do not inflate the count.
    Falls back to the legacy time-window counter when pre_sha is empty.
    """
    try:
        from dispatch_parameter_tracker import (
            DispatchParameterTracker,
            DispatchOutcome,
            _count_lines_changed,
            _lookup_cqs,
        )

        # Compute completion minutes
        try:
            start_dt = datetime.fromisoformat(start_ts)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        if pre_sha:
            lines_changed = _count_lines_changed_since_sha(pre_sha, manifest_paths)
        else:
            lines_changed = _count_lines_changed(start_ts)

        outcome = DispatchOutcome(
            cqs=_lookup_cqs(dispatch_id),
            success=success,
            completion_minutes=round(elapsed, 2),
            test_count=0,        # not reliably parseable here
            committed=committed,
            lines_changed=lines_changed,
        )
        tracker = DispatchParameterTracker()
        tracker.capture_outcome(dispatch_id, outcome)
        logger.debug(
            "Outcome capture: dispatch=%s success=%s mins=%.1f cqs=%s",
            dispatch_id, success, elapsed, outcome.cqs,
        )
    except Exception as exc:
        logger.debug("Outcome capture failed for %s: %s", dispatch_id, exc)


def _update_pattern_confidence(
    dispatch_id: str,
    status: str,
    db_path: "Path",
) -> int:
    """Update confidence for patterns that were OFFERED in this dispatch.

    Looks up dispatch_pattern_offered (or legacy pattern_usage.dispatch_id) rows
    matching this dispatch, then:
    - success: boosts success_patterns.confidence_score + 0.05 (cap 1.0) and
               touches pattern_usage.last_used + updated_at
    - failure: decays success_patterns.confidence_score - 0.10 (floor 0.0) and
               touches pattern_usage.last_used + updated_at

    NOTE: pattern_usage.used_count must NOT be incremented here.  Existing
    consumers treat used_count > 0 as evidence that a worker actually consumed
    a pattern, not merely that it was offered.  Likewise success_count and
    failure_count are reserved for confirmed worker usage outcomes.  The
    legacy fallback only increments success_count when usage is unknown; the
    offered-only feedback loop touches timestamps and the confidence_score in
    success_patterns instead.

    Linkage is by title: pattern_usage.pattern_title → success_patterns.title.
    Returns count of pattern_usage rows updated.  Never raises.
    """
    if not db_path.exists():
        return 0

    is_success = (status == "success")
    now = datetime.now(timezone.utc).isoformat()
    updated = 0

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row

        injected = _query_offered_patterns(conn, dispatch_id)
        for row in injected:
            _apply_pattern_outcome(conn, row["pattern_id"], row["pattern_title"], is_success, now)
            updated += 1

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("_update_pattern_confidence failed for %s: %s", dispatch_id, exc)

    return updated


def _query_offered_patterns(conn, dispatch_id: str) -> list:
    """Return rows of (pattern_id, pattern_title) offered for this dispatch.

    Uses dispatch_pattern_offered when present (isolated per-dispatch junction
    table) so patterns offered to multiple concurrent dispatches are not
    misattributed.  Falls back to pattern_usage.dispatch_id for older DBs.
    """
    offered_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='dispatch_pattern_offered'"
    ).fetchone()

    if offered_table_exists:
        return conn.execute(
            "SELECT pattern_id, pattern_title FROM dispatch_pattern_offered "
            "WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchall()
    return conn.execute(
        "SELECT pattern_id, pattern_title FROM pattern_usage "
        "WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchall()


def _apply_pattern_outcome(
    conn, pattern_id, title: str, is_success: bool, now: str,
) -> None:
    """Apply success-boost or failure-decay to a single offered pattern."""
    if is_success:
        conn.execute(
            """
            UPDATE success_patterns
            SET confidence_score = MIN(confidence_score + 0.05, 1.0),
                last_used        = ?
            WHERE title = ?
            """,
            (now, title),
        )
        # Offered-only path: do NOT touch used_count, success_count, or
        # failure_count here.  Those are reserved for confirmed worker
        # usage signals (see learning_loop.update_confidence_scores).
        conn.execute(
            """
            UPDATE pattern_usage
            SET last_used  = ?,
                updated_at = ?
            WHERE pattern_id = ?
            """,
            (now, now, pattern_id),
        )
    else:
        conn.execute(
            """
            UPDATE success_patterns
            SET confidence_score = MAX(confidence_score - 0.10, 0.0)
            WHERE title = ?
            """,
            (title,),
        )
        conn.execute(
            """
            UPDATE pattern_usage
            SET last_used  = ?,
                updated_at = ?
            WHERE pattern_id = ?
            """,
            (now, now, pattern_id),
        )
