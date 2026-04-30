#!/usr/bin/env python3
"""dispatch_receipt.py — Receipt writing and telemetry helpers for subprocess dispatch.

Provides completion receipt appending, dispatch parameter capture,
outcome recording, and pattern-confidence feedback.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dispatch_context import _default_state_dir
from dispatch_git_query import _count_lines_changed_since_sha

logger = logging.getLogger(__name__)


def _write_receipt(
    dispatch_id: str,
    terminal_id: str,
    status: str,
    *,
    event_count: int = 0,
    session_id: str | None = None,
    attempt: int | None = None,
    failure_reason: str | None = None,
    commit_missing: bool = False,
    committed: bool = False,
    commit_hash_before: str = "",
    commit_hash_after: str = "",
    manifest_path: str | None = None,
    stuck_event_count: int = 0,
) -> Path:
    """Append a subprocess completion receipt to t0_receipts.ndjson.

    Returns the path to the receipt file.
    """
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "terminal": terminal_id,
        "status": status,
        "event_count": event_count,
        "session_id": session_id,
        "source": "subprocess",
    }
    if commit_hash_before:
        receipt["commit_hash_before"] = commit_hash_before
    if commit_hash_after:
        receipt["commit_hash_after"] = commit_hash_after
    if commit_hash_before and commit_hash_after:
        receipt["committed"] = committed or (commit_hash_before != commit_hash_after)
    elif committed:
        receipt["committed"] = True
    if manifest_path:
        receipt["manifest_path"] = manifest_path
    if attempt is not None:
        receipt["attempt"] = attempt
    if failure_reason:
        receipt["failure_reason"] = failure_reason
    if commit_missing:
        receipt["commit_missing"] = True
    if stuck_event_count:
        receipt["stuck_event_count"] = stuck_event_count

    _scripts_dir = Path(__file__).resolve().parents[1]
    try:
        sys.path.insert(0, str(_scripts_dir))
        from append_receipt import append_receipt_payload
        result = append_receipt_payload(receipt)
        receipt_path = result.receipts_file
        if result.status == "duplicate":
            logger.debug(
                "Receipt already appended (idempotent skip): dispatch=%s", dispatch_id
            )
        else:
            logger.info(
                "Receipt written: dispatch=%s terminal=%s status=%s",
                dispatch_id, terminal_id, status,
            )
        return receipt_path
    except Exception as exc:
        # Fallback: bare write to prevent receipt loss on import error (e.g. circular import)
        logger.warning(
            "append_receipt_payload failed (%s); falling back to bare write", exc
        )
        receipt_path = _default_state_dir() / "t0_receipts.ndjson"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(receipt_path, "a") as f:
            f.write(json.dumps(receipt) + "\n")
        logger.info(
            "Receipt written (bare): dispatch=%s terminal=%s status=%s",
            dispatch_id, terminal_id, status,
        )
        return receipt_path


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
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timezone as _tz

    if not db_path.exists():
        return 0

    is_success = (status == "success")
    now = _dt.now(_tz.utc).isoformat()
    updated = 0

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row

        # Query dispatch_pattern_offered (isolated per-dispatch junction table) so that
        # patterns offered to multiple concurrent dispatches are not misattributed.
        # Falls back to pattern_usage.dispatch_id for DBs that predate the junction table.
        offered_table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dispatch_pattern_offered'"
        ).fetchone()

        if offered_table_exists:
            injected = conn.execute(
                "SELECT pattern_id, pattern_title FROM dispatch_pattern_offered "
                "WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchall()
        else:
            injected = conn.execute(
                "SELECT pattern_id, pattern_title FROM pattern_usage "
                "WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchall()

        for row in injected:
            pattern_id = row["pattern_id"]
            title = row["pattern_title"]

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
            updated += 1

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("_update_pattern_confidence failed for %s: %s", dispatch_id, exc)

    return updated
