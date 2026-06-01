"""
recent_comparable source — recent dispatch similarity ranking.

Queries dispatch_metadata to surface recently successful/failed dispatches
that share skill, gate, or track context with the current dispatch.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from ._common import (
    MAX_CONTENT_CHARS_PER_ITEM,
    PATTERN_CATEGORY_PROCESS,
    RECENT_COMPARABLE_DAYS,
    IntelligenceItem,
    _content_hash,
    _now_utc,
    _project_scope_clause,
    _scope_matches,
    _stable_item_id,
    _table_has_column,
)
try:
    from project_scope import current_project_id
except ImportError:
    from project_scope import current_project_id  # type: ignore

try:
    import shadow_verifier as _shadow_verifier
    import shadow_logger as _shadow_logger
except ImportError:
    _shadow_verifier = None  # type: ignore[assignment]
    _shadow_logger = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_RECENT_COMPARABLE_SQL_TEMPLATE = (
    "SELECT dispatch_id, terminal, track, role, skill_name, gate, "
    "outcome_status, dispatched_at, pattern_count, prevention_rule_count, "
    "provider, model "
    "FROM dispatch_metadata "
    "WHERE dispatched_at >= ? AND outcome_status IS NOT NULL "
    "ORDER BY dispatched_at DESC LIMIT 20"
)


def query_recent_comparable(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    *,
    has_column_fn: Callable[[str, str], bool],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """3-state dispatcher: per-project | central | shadow (metric 4, dispatch_metadata)."""
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _query_per_project(db, task_class, scope_tags, has_column_fn)
    if flag == "1":
        return _query_central(task_class, scope_tags, central_conn_fn, project_id_fn=project_id_fn)
    legacy = _query_per_project(db, task_class, scope_tags, has_column_fn)
    central = _query_central(task_class, scope_tags, central_conn_fn, project_id_fn=project_id_fn)
    if _shadow_verifier is not None:
        project_id = current_project_id()
        try:
            cmp = _shadow_verifier.compare(
                [item.to_dict() for item in legacy],
                [item.to_dict() for item in central],
                project_id=project_id,
                read_site="IntelligenceSelector._query_recent_comparable",
                sql_template=_RECENT_COMPARABLE_SQL_TEMPLATE,
                metric_id=4,
                table="dispatch_metadata",
            )
            if cmp.divergences and _shadow_logger is not None:
                _shadow_logger.write_comparison_result(
                    cmp, project_id,
                    "IntelligenceSelector._query_recent_comparable",
                )
        except Exception as e:
            logger.debug("Shadow compare (recent_comparable) skipped: %s", e)
    return legacy


def _dm_has_provider(conn: sqlite3.Connection) -> bool:
    """Return True if dispatch_metadata has provider/model columns (migration guard)."""
    return _table_has_column(conn, "dispatch_metadata", "provider")


def _query_per_project(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    has_column_fn: Callable[[str, str], bool],
) -> List[IntelligenceItem]:
    """Query dispatch_metadata for recent_comparable candidates."""
    items: List[IntelligenceItem] = []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=RECENT_COMPARABLE_DAYS)
    ).isoformat()
    dm_scope_clause, dm_scope_params = _project_scope_clause(
        has_column_fn("dispatch_metadata", "project_id")
    )
    provider_cols = ", provider, model" if _dm_has_provider(db) else ""
    try:
        rows = db.execute(
            f"""
            SELECT dispatch_id, terminal, track, role, skill_name, gate,
                   outcome_status, dispatched_at, pattern_count,
                   prevention_rule_count{provider_cols}
            FROM dispatch_metadata
            WHERE dispatched_at >= ?
              AND outcome_status IS NOT NULL
              {dm_scope_clause}
            ORDER BY dispatched_at DESC
            LIMIT 20
            """,
            (cutoff, *dm_scope_params),
        ).fetchall()
    except Exception as exc:
        logger.warning("recent_comparable per-project query failed: %s", exc)
        return items
    for row in rows:
        item = _row_to_intelligence_item(row, scope_tags)
        if item is not None:
            items.append(item)
    return items


def _row_to_intelligence_item(
    row: object,
    scope_tags: List[str],
) -> Optional[IntelligenceItem]:
    """Convert a dispatch_metadata row to IntelligenceItem, or None if filtered out."""
    row_d = dict(row)  # type: ignore[call-overload]
    dispatch_scope = []
    if row_d.get("skill_name"):
        dispatch_scope.append(row_d["skill_name"])
    if row_d.get("gate"):
        dispatch_scope.append(row_d["gate"])
    if row_d.get("track"):
        dispatch_scope.append(f"Track-{row_d['track']}")
    if not _scope_matches(dispatch_scope, scope_tags):
        return None
    outcome = row_d.get("outcome_status", "unknown")
    skill = row_d.get("skill_name") or row_d.get("role") or "unknown"
    gate = row_d.get("gate") or ""
    provider_str = row_d.get("provider") or ""
    model_str = row_d.get("model") or ""
    provider_tag = f" [{provider_str}/{model_str}]" if provider_str else ""
    content = (
        f"Dispatch {row_d['dispatch_id']} ({skill}, {gate}){provider_tag} "
        f"completed with status: {outcome}. "
        f"Patterns used: {row_d.get('pattern_count', 0)}, "
        f"Prevention rules: {row_d.get('prevention_rule_count', 0)}."
    )[:MAX_CONTENT_CHARS_PER_ITEM]
    confidence = 0.7 if outcome == "success" else 0.45
    dm_title = f"Recent: {skill} dispatch ({outcome})"[:120]
    return IntelligenceItem(
        item_id=_stable_item_id("dm", str(row_d.get("dispatch_id", ""))),
        item_class="recent_comparable",
        title=dm_title,
        content=content,
        confidence=confidence,
        evidence_count=1,
        last_seen=row_d.get("dispatched_at") or _now_utc(),
        scope_tags=dispatch_scope,
        source_refs=[row_d["dispatch_id"]],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_PROCESS,
        content_hash=_content_hash(dm_title, content),
    )


def _query_central(
    task_class: str,
    scope_tags: List[str],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    *,
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """Query dispatch_metadata from central DB (independent connection, project_id-scoped)."""
    items: List[IntelligenceItem] = []
    conn = central_conn_fn()
    if conn is None:
        return items
    try:
        project_id = (project_id_fn or current_project_id)()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=RECENT_COMPARABLE_DAYS)
        ).isoformat()
        has_project_id = _table_has_column(conn, "dispatch_metadata", "project_id")
        provider_cols = ", provider, model" if _dm_has_provider(conn) else ""
        if has_project_id and project_id:
            rows = conn.execute(
                f"""SELECT dispatch_id, terminal, track, role, skill_name, gate,
                       outcome_status, dispatched_at, pattern_count,
                       prevention_rule_count{provider_cols}
                FROM dispatch_metadata
                WHERE dispatched_at >= ?
                  AND outcome_status IS NOT NULL
                  AND project_id = ?
                ORDER BY dispatched_at DESC LIMIT 20""",
                (cutoff, project_id),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT dispatch_id, terminal, track, role, skill_name, gate,
                       outcome_status, dispatched_at, pattern_count,
                       prevention_rule_count{provider_cols}
                FROM dispatch_metadata
                WHERE dispatched_at >= ?
                  AND outcome_status IS NOT NULL
                ORDER BY dispatched_at DESC LIMIT 20""",
                (cutoff,),
            ).fetchall()
        for row in rows:
            item = _row_to_intelligence_item(row, scope_tags)
            if item is not None:
                items.append(item)
    except sqlite3.Error as e:
        logger.debug("Central recent-comparable query failed: %s", e)
    finally:
        conn.close()
    return items
