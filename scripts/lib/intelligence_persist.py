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
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)

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

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
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

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
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

        # Record a confidence_events audit row in the SAME transaction as the
        # confidence UPDATE above so they commit atomically (or roll back together).
        # Columns are appended only when present so the insert works across every
        # migration level.  Schema-absent errors (table/column not yet migrated)
        # are silently skipped; resource errors (locked, disk-full) are logged at
        # WARNING so the lost-audit case is never silent.
        _SCHEMA_ABSENT_MSGS = ("no such table", "no such column", "has no column")
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
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if any(token in msg for token in _SCHEMA_ABSENT_MSGS):
                pass  # vnx-silent-except: table/column not yet migrated in older DBs
            else:
                # Resource error (locked/disk-full): roll back the confidence UPDATE too so it
                # never commits without its audit row (atomic). Fail-open + visible: the caller
                # gets an audit-consistent no-op, not an exception, and the loss is logged.
                _LOG.warning(
                    "intelligence_persist: confidence_events audit insert failed — rolling back "
                    "the confidence update to stay atomic (dispatch=%s): %s",
                    dispatch_id, exc,
                )
                conn.rollback()
                return {"boosted": 0, "decayed": 0}

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
            if not isinstance(items, list):
                # Audit C5: a valid-but-non-list JSON value (e.g. {} or "x") would otherwise crash
                # items.append() and abort the whole persist batch. Treat it as empty.
                items = []
        except (json.JSONDecodeError, TypeError):
            items = []
    if new_item and new_item not in items:
        items.append(new_item)
    return json.dumps(items[-20:])  # keep last 20


# ---------------------------------------------------------------------------
# Shadow / divergence comparison (D5)
# ---------------------------------------------------------------------------

def _projected_conf(
    found: bool,
    current_conf: float,
    cur_succ: int,
    cur_fail: int,
    is_success: bool,
) -> float:
    """Shadow helper: projected confidence after one outcome — no DB write.

    When ``found`` is False the pattern is not matched by this path, so the
    confidence is left unchanged.  When True, the beta-posterior is recomputed
    with the incremented counter.
    """
    if not found:
        return current_conf
    from confidence_reconcile import beta_score
    new_s = cur_succ + (1 if is_success else 0)
    new_f = cur_fail + (0 if is_success else 1)
    return round(beta_score(new_s, new_f), 6)


def shadow_grounding_compare(
    db_path: Path,
    dispatches: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Read-only V1 vs V2 confidence divergence report.

    For each entry in ``dispatches`` (list of
    ``{'dispatch_id': str, 'status': 'success'|'failure'}``), resolves which
    patterns V1 (``source_dispatch_ids LIKE``) and V2
    (``dispatch_pattern_offered`` junction) would update, then computes the
    projected beta-score each path would produce — WITHOUT writing anything.

    ``VNX_OUTCOME_GROUNDING_V2`` is intentionally ignored: shadow mode always
    evaluates both paths in parallel so the report is flag-independent.

    Returns::

        {
          'dispatches': [
            {
              'dispatch_id': str,
              'status': str,
              'v1_pattern_ids': [int, ...],
              'v2_pattern_ids': [int, ...],
              'v1_only':   [int, ...],   # matched by V1, not V2
              'v2_only':   [int, ...],   # matched by V2, not V1
              'agreement': [int, ...],   # matched by both
              'pattern_details': {
                  sp_id: {
                      'title': str,
                      'current_conf': float,
                      'v1_new_conf': float,  # current_conf when not matched
                      'v2_new_conf': float,
                      'in_v1': bool,
                      'in_v2': bool,
                  }
              },
              'has_divergence': bool,
            },
            ...
          ],
          'summary': {
              'total_dispatches': int,
              'diverged_dispatches': int,
              'v2_only_grounded': int,
              'v1_only_grounded': int,
              'junction_available': bool,
          },
        }
    """
    if not db_path.exists():
        return {
            "dispatches": [],
            "summary": {
                "total_dispatches": 0,
                "diverged_dispatches": 0,
                "v2_only_grounded": 0,
                "v1_only_grounded": 0,
                "junction_available": False,
            },
        }

    from confidence_reconcile import SUCCESS_PATTERN_PREFIX

    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        project_id = current_project_id()
        sp_has_project = _has_column(conn, "success_patterns", "project_id")
        junction_available = _has_table(conn, "dispatch_pattern_offered")
        pu_table_exists = _has_table(conn, "pattern_usage")

        dispatch_results: List[Dict[str, Any]] = []
        total_v2_only = 0
        total_v1_only = 0
        diverged = 0

        for entry in dispatches:
            dispatch_id = (entry.get("dispatch_id") or "").strip()
            status = (entry.get("status") or "").lower()
            is_success = status == "success"

            v1_rows = _legacy_source_id_rows(conn, dispatch_id, project_id, sp_has_project)
            v1_by_id = {int(row["id"]): row for row in v1_rows}
            v1_ids = set(v1_by_id)

            if junction_available:
                v2_rows = _junction_grounded_rows(conn, dispatch_id, project_id, sp_has_project)
            else:
                v2_rows = []
            v2_by_id = {int(row["id"]): row for row in v2_rows}
            v2_ids = set(v2_by_id)

            all_ids = v1_ids | v2_ids
            v1_only = sorted(v1_ids - v2_ids)
            v2_only = sorted(v2_ids - v1_ids)
            agreement = sorted(v1_ids & v2_ids)
            has_divergence = bool(v1_only or v2_only)

            if has_divergence:
                diverged += 1
            total_v2_only += len(v2_only)
            total_v1_only += len(v1_only)

            pattern_details: Dict[int, Dict[str, Any]] = {}
            for sp_id in all_ids:
                source_row = v1_by_id.get(sp_id) or v2_by_id.get(sp_id)
                current_conf = float(source_row["confidence_score"] or 0.0) if source_row else 0.0
                title = str(source_row["title"] or f"pattern_{sp_id}")[:120] if source_row else f"pattern_{sp_id}"

                cur_succ = 0
                cur_fail = 0
                if pu_table_exists:
                    pid = f"{SUCCESS_PATTERN_PREFIX}{sp_id}"
                    try:
                        pu_row = conn.execute(
                            "SELECT success_count, failure_count FROM pattern_usage "
                            "WHERE pattern_id = ?",
                            (pid,),
                        ).fetchone()
                        if pu_row:
                            cur_succ = int(pu_row["success_count"] or 0)
                            cur_fail = int(pu_row["failure_count"] or 0)
                    except sqlite3.OperationalError:
                        pass

                pattern_details[sp_id] = {
                    "title": title,
                    "current_conf": current_conf,
                    "v1_new_conf": _projected_conf(
                        sp_id in v1_ids, current_conf, cur_succ, cur_fail, is_success
                    ),
                    "v2_new_conf": _projected_conf(
                        sp_id in v2_ids, current_conf, cur_succ, cur_fail, is_success
                    ),
                    "in_v1": sp_id in v1_ids,
                    "in_v2": sp_id in v2_ids,
                }

            dispatch_results.append({
                "dispatch_id": dispatch_id,
                "status": status,
                "v1_pattern_ids": sorted(v1_ids),
                "v2_pattern_ids": sorted(v2_ids),
                "v1_only": v1_only,
                "v2_only": v2_only,
                "agreement": agreement,
                "pattern_details": pattern_details,
                "has_divergence": has_divergence,
            })

        return {
            "dispatches": dispatch_results,
            "summary": {
                "total_dispatches": len(dispatches),
                "diverged_dispatches": diverged,
                "v2_only_grounded": total_v2_only,
                "v1_only_grounded": total_v1_only,
                "junction_available": junction_available,
            },
        }
    finally:
        conn.close()
