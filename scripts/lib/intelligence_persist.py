#!/usr/bin/env python3
"""Persist governance signals to quality_intelligence.db tables.

Bridges the gap between governance_signal_extractor (which produces in-memory
GovernanceSignal objects) and intelligence_selector (which queries DB tables).

Called by intelligence_daemon.GovernanceDigestRunner after signal collection.

Tables written:
  - success_patterns:  from gate_success signals (recurring gate passes → proven patterns)
  - antipatterns:      from gate_failure and queue_anomaly signals
  - dispatch_metadata: outcome_status updated from gate results
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from project_scope import current_project_id
except ImportError:  # pragma: no cover - lib path bootstrap
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from project_scope import current_project_id


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        if name == column:
            return True
    return False


def persist_signals_to_db(
    signals: List[Any],
    db_path: Path,
) -> Dict[str, int]:
    """Persist governance signals to quality_intelligence.db.

    Args:
        signals: List of GovernanceSignal objects (duck-typed).
        db_path: Path to quality_intelligence.db.

    Returns:
        Dict with counts: patterns_upserted, antipatterns_upserted, metadata_updated.
    """
    if not signals or not db_path.exists():
        return {"patterns_upserted": 0, "antipatterns_upserted": 0, "metadata_updated": 0}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    patterns_upserted = 0
    antipatterns_upserted = 0
    metadata_updated = 0

    try:
        for sig in signals:
            sig_type = getattr(sig, "signal_type", "")
            content = getattr(sig, "content", "")
            severity = getattr(sig, "severity", "info")
            corr = getattr(sig, "correlation", None)
            dispatch_id = getattr(corr, "dispatch_id", "") if corr else ""
            feature_id = getattr(corr, "feature_id", "") if corr else ""
            defect_family = getattr(sig, "defect_family", "") or ""

            if sig_type == "gate_success":
                patterns_upserted += _upsert_success_pattern(
                    conn, content, feature_id, dispatch_id, now,
                )

            elif sig_type in ("gate_failure", "queue_anomaly"):
                antipatterns_upserted += _upsert_antipattern(
                    conn, sig_type, content, severity, dispatch_id,
                    defect_family, now,
                )

            if dispatch_id and sig_type in ("gate_success", "gate_failure"):
                outcome = "success" if sig_type == "gate_success" else "failure"
                metadata_updated += _update_dispatch_outcome(
                    conn, dispatch_id, outcome, now,
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "patterns_upserted": patterns_upserted,
        "antipatterns_upserted": antipatterns_upserted,
        "metadata_updated": metadata_updated,
    }


def _upsert_success_pattern(
    conn: sqlite3.Connection,
    content: str,
    feature_id: str,
    dispatch_id: str,
    now: str,
) -> int:
    """Insert or update a success_pattern from a gate_success signal."""
    title = content[:120] if content else "Gate passed"
    category = "governance"
    project_id = current_project_id()
    has_project = _has_column(conn, "success_patterns", "project_id")

    if has_project:
        existing = conn.execute(
            "SELECT id, usage_count, source_dispatch_ids FROM success_patterns "
            "WHERE title = ? AND category = ? AND project_id = ?",
            (title, category, project_id),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, usage_count, source_dispatch_ids FROM success_patterns "
            "WHERE title = ? AND category = ?",
            (title, category),
        ).fetchone()

    if existing:
        row = dict(existing)
        usage_count = (row.get("usage_count") or 0) + 1
        source_ids = _append_to_json_list(row.get("source_dispatch_ids"), dispatch_id)
        confidence = min(1.0, 0.5 + (usage_count * 0.05))
        conn.execute(
            "UPDATE success_patterns SET usage_count = ?, confidence_score = ?, "
            "source_dispatch_ids = ?, last_used = ? WHERE id = ?",
            (usage_count, confidence, source_ids, now, row["id"]),
        )
    else:
        source_ids = json.dumps([dispatch_id]) if dispatch_id else "[]"
        if has_project:
            conn.execute(
                "INSERT INTO success_patterns "
                "(pattern_type, category, title, description, pattern_data, "
                " confidence_score, usage_count, source_dispatch_ids, "
                " first_seen, last_used, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("approach", category, title, content[:500],
                 json.dumps({"source": "governance_signal"}),
                 0.55, 1, source_ids, now, now, project_id),
            )
        else:
            conn.execute(
                "INSERT INTO success_patterns "
                "(pattern_type, category, title, description, pattern_data, "
                " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("approach", category, title, content[:500],
                 json.dumps({"source": "governance_signal"}),
                 0.55, 1, source_ids, now, now),
            )

    return 1


def _upsert_antipattern(
    conn: sqlite3.Connection,
    sig_type: str,
    content: str,
    severity: str,
    dispatch_id: str,
    defect_family: str,
    now: str,
) -> int:
    """Insert or update an antipattern from a gate_failure or queue_anomaly signal."""
    title = content[:120] if content else f"{sig_type} detected"
    category = "governance"
    project_id = current_project_id()
    has_project = _has_column(conn, "antipatterns", "project_id")

    if has_project:
        existing = conn.execute(
            "SELECT id, occurrence_count, source_dispatch_ids FROM antipatterns "
            "WHERE title = ? AND category = ? AND project_id = ?",
            (title, category, project_id),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id, occurrence_count, source_dispatch_ids FROM antipatterns "
            "WHERE title = ? AND category = ?",
            (title, category),
        ).fetchone()

    if existing:
        row = dict(existing)
        occurrence_count = (row.get("occurrence_count") or 0) + 1
        source_ids = _append_to_json_list(row.get("source_dispatch_ids"), dispatch_id)
        conn.execute(
            "UPDATE antipatterns SET occurrence_count = ?, "
            "source_dispatch_ids = ?, last_seen = ? WHERE id = ?",
            (occurrence_count, source_ids, now, row["id"]),
        )
    else:
        source_ids = json.dumps([dispatch_id]) if dispatch_id else "[]"
        db_severity = "high" if severity == "blocker" else severity
        if db_severity not in ("critical", "high", "medium", "low"):
            db_severity = "medium"
        if has_project:
            conn.execute(
                "INSERT INTO antipatterns "
                "(pattern_type, category, title, description, pattern_data, "
                " why_problematic, severity, occurrence_count, "
                " source_dispatch_ids, first_seen, last_seen, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("approach", category, title, content[:500],
                 json.dumps({"source": "governance_signal", "defect_family": defect_family}),
                 content[:500], db_severity, 1,
                 source_ids, now, now, project_id),
            )
        else:
            conn.execute(
                "INSERT INTO antipatterns "
                "(pattern_type, category, title, description, pattern_data, "
                " why_problematic, severity, occurrence_count, "
                " source_dispatch_ids, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("approach", category, title, content[:500],
                 json.dumps({"source": "governance_signal", "defect_family": defect_family}),
                 content[:500], db_severity, 1,
                 source_ids, now, now),
            )

    return 1


def _update_dispatch_outcome(
    conn: sqlite3.Connection,
    dispatch_id: str,
    outcome: str,
    now: str,
) -> int:
    """Update dispatch_metadata outcome_status if the row exists.

    Scoped to ``current_project_id()`` so a dispatch_id collision across
    tenants does not let one project overwrite another's outcome.
    """
    if _has_column(conn, "dispatch_metadata", "project_id"):
        cur = conn.execute(
            "UPDATE dispatch_metadata SET outcome_status = ?, completed_at = ? "
            "WHERE dispatch_id = ? AND outcome_status IS NULL "
            "AND project_id = ?",
            (outcome, now, dispatch_id, current_project_id()),
        )
    else:
        cur = conn.execute(
            "UPDATE dispatch_metadata SET outcome_status = ?, completed_at = ? "
            "WHERE dispatch_id = ? AND outcome_status IS NULL",
            (outcome, now, dispatch_id),
        )
    return cur.rowcount


def update_confidence_from_outcome(
    db_path: Path,
    dispatch_id: str,
    terminal: str,
    status: str,
) -> Dict[str, int]:
    """Update pattern confidence scores based on dispatch outcome.

    Uses Beta(success+1, failure+1) Laplace smoothing instead of fixed
    +0.05 / -0.1 deltas so the score reflects total usage volume rather
    than the number of consecutive boosts.  Each outcome increments
    success_count or failure_count on the matching pattern_usage row, then
    the new Beta posterior is written back to success_patterns.confidence_score.

    Linkage is via success_patterns.source_dispatch_ids (JSON array).
    pattern_usage rows are matched / created using the stable
    "intel_sp_<id>" id convention so the daily reconcile and the
    per-dispatch update read and write the same row.

    A confidence_events row is written for audit/learning-summary queries.

    Args:
        db_path:     Path to quality_intelligence.db.
        dispatch_id: Dispatch identifier.
        terminal:    Terminal that ran the dispatch (T1/T2/T3).
        status:      'success' or 'failure'.

    Returns:
        Dict with boosted/decayed counts.
    """
    if not db_path.exists():
        return {"boosted": 0, "decayed": 0}

    # Local import to keep the module standalone-importable.
    from confidence_reconcile import beta_score, SUCCESS_PATTERN_PREFIX

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    boosted = 0
    decayed = 0
    net_change = 0.0
    project_id = current_project_id()
    sp_has_project = _has_column(conn, "success_patterns", "project_id")
    pu_has_project = _has_column(conn, "pattern_usage", "project_id")
    ce_has_project = _has_column(conn, "confidence_events", "project_id")

    try:
        if sp_has_project:
            rows = conn.execute(
                "SELECT id, confidence_score, usage_count, title FROM success_patterns "
                "WHERE source_dispatch_ids LIKE ? AND project_id = ?",
                (f"%{dispatch_id}%", project_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, confidence_score, usage_count, title FROM success_patterns "
                "WHERE source_dispatch_ids LIKE ?",
                (f"%{dispatch_id}%",),
            ).fetchall()

        is_success = status == "success"

        for row in rows:
            sp_id = row["id"]
            pattern_id = f"{SUCCESS_PATTERN_PREFIX}{sp_id}"
            title = (row["title"] or f"success_pattern_{sp_id}")[:255]
            pattern_hash = _sha1(pattern_id)

            usage = conn.execute(
                "SELECT success_count, failure_count, used_count "
                "FROM pattern_usage WHERE pattern_id = ?",
                (pattern_id,),
            ).fetchone()

            if usage is None:
                succ = 1 if is_success else 0
                fail = 0 if is_success else 1
                used = 1 if is_success else 0
                if pu_has_project:
                    conn.execute(
                        "INSERT INTO pattern_usage "
                        "(pattern_id, pattern_title, pattern_hash, used_count, "
                        " ignored_count, success_count, failure_count, "
                        " last_used, confidence, created_at, updated_at, project_id) "
                        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                        (pattern_id, title, pattern_hash, used,
                         succ, fail, now, beta_score(succ, fail), now, now,
                         project_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO pattern_usage "
                        "(pattern_id, pattern_title, pattern_hash, used_count, "
                        " ignored_count, success_count, failure_count, "
                        " last_used, confidence, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                        (pattern_id, title, pattern_hash, used,
                         succ, fail, now, beta_score(succ, fail), now, now),
                    )
            else:
                succ = int(usage["success_count"] or 0) + (1 if is_success else 0)
                fail = int(usage["failure_count"] or 0) + (0 if is_success else 1)
                used = int(usage["used_count"] or 0) + (1 if is_success else 0)
                conn.execute(
                    "UPDATE pattern_usage SET success_count = ?, "
                    "failure_count = ?, used_count = ?, "
                    "confidence = ?, last_used = ?, updated_at = ? "
                    "WHERE pattern_id = ?",
                    (succ, fail, used, beta_score(succ, fail), now, now, pattern_id),
                )

            old_conf = float(row["confidence_score"] or 0.0)
            new_conf = round(beta_score(succ, fail), 6)
            net_change += new_conf - old_conf

            if is_success:
                new_usage_count = (row["usage_count"] or 0) + 1
                conn.execute(
                    "UPDATE success_patterns SET confidence_score = ?, "
                    "usage_count = ?, last_used = ? WHERE id = ?",
                    (new_conf, new_usage_count, now, sp_id),
                )
                boosted += 1
            else:
                conn.execute(
                    "UPDATE success_patterns SET confidence_score = ? WHERE id = ?",
                    (new_conf, sp_id),
                )
                decayed += 1

        # Record a confidence_events audit row (best-effort — table may not exist yet)
        try:
            if ce_has_project:
                conn.execute(
                    "INSERT INTO confidence_events "
                    "(dispatch_id, terminal, outcome, patterns_boosted, patterns_decayed, "
                    " confidence_change, occurred_at, project_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (dispatch_id, terminal, status, boosted, decayed,
                     round(net_change, 4), now, project_id),
                )
            else:
                conn.execute(
                    "INSERT INTO confidence_events "
                    "(dispatch_id, terminal, outcome, patterns_boosted, patterns_decayed, "
                    " confidence_change, occurred_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (dispatch_id, terminal, status, boosted, decayed,
                     round(net_change, 4), now),
                )
        except sqlite3.OperationalError:
            pass  # Table not yet migrated in older DBs

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"boosted": boosted, "decayed": decayed}


def _sha1(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _append_to_json_list(existing_json: Optional[str], new_item: str) -> str:
    """Append new_item to a JSON array string, deduplicating."""
    items: list = []
    if existing_json:
        try:
            items = json.loads(existing_json)
        except (json.JSONDecodeError, TypeError):
            items = []
    if new_item and new_item not in items:
        items.append(new_item)
    return json.dumps(items[-20:])  # keep last 20
