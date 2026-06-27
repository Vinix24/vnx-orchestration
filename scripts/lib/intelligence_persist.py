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
from typing import Any, Dict, List, Optional, Tuple

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


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _outcome_grounding_v2_enabled() -> bool:
    """Opt-in flag for junction-grounded confidence updates (default OFF).

    When unset, ``update_confidence_from_outcome`` keeps its legacy
    ``source_dispatch_ids`` substring join byte-for-byte. When ``=1`` and the
    ``dispatch_pattern_offered`` junction exists, the precise per-dispatch
    offered↔pattern linkage grounds confidence instead. Governance-critical
    path (runs on every completion receipt) — gated for a deliberate rollout.
    """
    import config_runtime
    return config_runtime.get_bool("VNX_OUTCOME_GROUNDING_V2")


def _legacy_source_id_rows(
    conn: sqlite3.Connection,
    dispatch_id: str,
    project_id: str,
    sp_has_project: bool,
) -> list:
    """Legacy join: success_patterns whose source_dispatch_ids contains dispatch_id."""
    if sp_has_project:
        return conn.execute(
            "SELECT id, confidence_score, usage_count, title FROM success_patterns "
            "WHERE source_dispatch_ids LIKE ? AND project_id = ?",
            (f"%{dispatch_id}%", project_id),
        ).fetchall()
    return conn.execute(
        "SELECT id, confidence_score, usage_count, title FROM success_patterns "
        "WHERE source_dispatch_ids LIKE ?",
        (f"%{dispatch_id}%",),
    ).fetchall()


def _junction_grounded_rows(
    conn: sqlite3.Connection,
    dispatch_id: str,
    project_id: str,
    sp_has_project: bool,
) -> list:
    """Precise join: success_patterns OFFERED for this dispatch via the junction.

    ``dispatch_pattern_offered`` is the per-dispatch offering junction (PK
    ``(dispatch_id, pattern_id)``), written atomically alongside
    ``source_dispatch_ids`` at injection time. Joining on the stable
    ``intel_sp_<id>`` pattern-id convention gives an exact offered↔pattern
    linkage — no substring false positives, no source_dispatch_ids 20-entry cap.
    Tenant-scoped on BOTH sides when the project_id column is present so a
    cross-tenant pattern_id/id collision cannot leak a confidence update.
    """
    from confidence_reconcile import SUCCESS_PATTERN_PREFIX

    params: list = [SUCCESS_PATTERN_PREFIX]  # binds the JOIN's (? || sp.id)
    where_parts = ["dpo.dispatch_id = ?"]
    params.append(dispatch_id)
    if sp_has_project:
        where_parts.append("sp.project_id = ?")
        params.append(project_id)
    if _has_column(conn, "dispatch_pattern_offered", "project_id"):
        where_parts.append("dpo.project_id = ?")
        params.append(project_id)
    sql = (
        "SELECT DISTINCT sp.id, sp.confidence_score, sp.usage_count, sp.title "
        "FROM success_patterns sp "
        "JOIN dispatch_pattern_offered dpo ON dpo.pattern_id = (? || sp.id) "
        "WHERE " + " AND ".join(where_parts)
    )
    return conn.execute(sql, params).fetchall()


def _resolve_grounded_patterns(
    conn: sqlite3.Connection,
    dispatch_id: str,
    project_id: str,
    sp_has_project: bool,
) -> Tuple[list, str]:
    """Return (success_pattern rows offered for this dispatch, grounding_source).

    With ``VNX_OUTCOME_GROUNDING_V2`` and the junction present, the junction is
    authoritative ('junction'); otherwise the legacy substring join is used
    ('source_dispatch_ids'). The junction and source_dispatch_ids are co-written
    in one transaction at injection, so "junction present" implies it is the
    complete, precise set — the legacy join only remains for pre-junction DBs
    and the flag-off default.
    """
    if _outcome_grounding_v2_enabled() and _has_table(conn, "dispatch_pattern_offered"):
        return _junction_grounded_rows(conn, dispatch_id, project_id, sp_has_project), "junction"
    return _legacy_source_id_rows(conn, dispatch_id, project_id, sp_has_project), "source_dispatch_ids"


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

    Linkage from a dispatch to the patterns it grounds is resolved by
    ``_resolve_grounded_patterns``: with ``VNX_OUTCOME_GROUNDING_V2=1`` the
    ``dispatch_pattern_offered`` junction (precise per-dispatch offered↔pattern
    linkage) is authoritative; otherwise the legacy ``source_dispatch_ids``
    substring join is used (default, byte-identical to the prior behaviour).
    pattern_usage rows are matched / created using the stable "intel_sp_<id>"
    id convention so the daily reconcile and the per-dispatch update read and
    write the same row.

    A confidence_events row is written for audit/learning-summary queries; its
    ``grounding_source`` records which linkage produced the event.

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
        rows, grounding_source = _resolve_grounded_patterns(
            conn, dispatch_id, project_id, sp_has_project
        )

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

        # Record a confidence_events audit row (best-effort — table/columns may
        # not exist yet on older DBs). Columns are appended only when present so
        # the same insert works across every migration level.
        try:
            ce_cols = ["dispatch_id", "terminal", "outcome", "patterns_boosted",
                       "patterns_decayed", "confidence_change", "occurred_at"]
            ce_vals: list = [dispatch_id, terminal, status, boosted, decayed,
                             round(net_change, 4), now]
            if ce_has_project:
                ce_cols.append("project_id")
                ce_vals.append(project_id)
            if _has_column(conn, "confidence_events", "grounding_source"):
                ce_cols.append("grounding_source")
                ce_vals.append(grounding_source)
            placeholders = ", ".join("?" for _ in ce_vals)
            conn.execute(
                f"INSERT INTO confidence_events ({', '.join(ce_cols)}) "
                f"VALUES ({placeholders})",
                ce_vals,
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
