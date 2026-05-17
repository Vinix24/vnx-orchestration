"""
failure_prevention source — antipattern evidence + prevention rules.

Handles per-project DB query (antipatterns + prevention_rules tables),
central DB query, and shadow comparison dispatch.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Callable, List, Optional

from ._common import (
    MAX_CONTENT_CHARS_PER_ITEM,
    PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE,
    PATTERN_CATEGORY_PROCESS,
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

_FAILURE_PREVENTION_SQL_TEMPLATE = (
    "SELECT id, title, description, severity, occurrence_count "
    "FROM antipatterns "
    "WHERE occurrence_count >= 1 "
    "AND (valid_until IS NULL OR valid_until > datetime('now')) "
    "ORDER BY occurrence_count DESC LIMIT 5"
)

_SEVERITY_CONFIDENCE = {"critical": 0.9, "high": 0.75, "medium": 0.6, "low": 0.5}


def query_failure_prevention(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    *,
    has_column_fn: Callable[[str, str], bool],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """3-state dispatcher: per-project | central | shadow (metric 3, antipatterns)."""
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
                read_site="IntelligenceSelector._query_failure_prevention",
                sql_template=_FAILURE_PREVENTION_SQL_TEMPLATE,
                metric_id=3,
            )
            if cmp.divergences and _shadow_logger is not None:
                _shadow_logger.write_comparison_result(
                    cmp, project_id,
                    "IntelligenceSelector._query_failure_prevention",
                )
        except Exception as e:
            logger.debug("Shadow compare (failure_prevention) skipped: %s", e)
    return legacy


def _query_per_project(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    has_column_fn: Callable[[str, str], bool],
) -> List[IntelligenceItem]:
    """Query antipatterns and prevention_rules tables."""
    items = _query_antipatterns(db, scope_tags, has_column_fn)
    items += _query_prevention_rules(db, scope_tags, has_column_fn)
    return items


def _query_antipatterns(
    db: sqlite3.Connection,
    scope_tags: List[str],
    has_column_fn: Callable[[str, str], bool],
) -> List[IntelligenceItem]:
    """Query antipatterns table for failure_prevention candidates."""
    items: List[IntelligenceItem] = []
    ap_scope_clause, ap_scope_params = _project_scope_clause(
        has_column_fn("antipatterns", "project_id")
    )
    try:
        rows = db.execute(
            f"""
            SELECT id, title, description, category, severity,
                   why_problematic, better_alternative,
                   occurrence_count, first_seen, last_seen
            FROM antipatterns
            WHERE occurrence_count >= 1
              AND (valid_until IS NULL OR valid_until > datetime('now'))
              {ap_scope_clause}
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 4
                    WHEN 'high' THEN 3
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 1
                    ELSE 0
                END DESC,
                occurrence_count DESC
            LIMIT 5
            """,
            ap_scope_params,
        ).fetchall()
    except Exception:
        return items
    for row in rows:
        row_d = dict(row)
        category = row_d.get("category", "")
        pattern_scope = [category] if category else []
        if not _scope_matches(pattern_scope, scope_tags):
            continue
        content_parts = []
        if row_d.get("why_problematic"):
            content_parts.append(row_d["why_problematic"])
        if row_d.get("better_alternative"):
            content_parts.append(f"Instead: {row_d['better_alternative']}")
        content = " ".join(content_parts)[:MAX_CONTENT_CHARS_PER_ITEM]
        severity = row_d.get("severity", "medium")
        confidence = _SEVERITY_CONFIDENCE.get(severity, 0.5)
        ap_title = (row_d.get("title") or "Failure prevention")[:120]
        items.append(IntelligenceItem(
            item_id=_stable_item_id("ap", str(row_d.get("id", ""))),
            item_class="failure_prevention",
            title=ap_title,
            content=content,
            confidence=confidence,
            evidence_count=int(row_d.get("occurrence_count", 1)),
            last_seen=row_d.get("last_seen") or row_d.get("first_seen") or _now_utc(),
            scope_tags=pattern_scope,
            source_refs=[f"antipattern_{row_d['id']}"],
            task_class_filter=[],
            pattern_category=PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE,
            content_hash=_content_hash(ap_title, content),
        ))
    return items


def _query_prevention_rules(
    db: sqlite3.Connection,
    scope_tags: List[str],
    has_column_fn: Callable[[str, str], bool],
) -> List[IntelligenceItem]:
    """Query prevention_rules table for failure_prevention candidates."""
    items: List[IntelligenceItem] = []
    pr_scope_clause, pr_scope_params = _project_scope_clause(
        has_column_fn("prevention_rules", "project_id")
    )
    try:
        rule_rows = db.execute(
            f"""
            SELECT id, tag_combination, rule_type, description,
                   recommendation, confidence, triggered_count, last_triggered
            FROM prevention_rules
            WHERE (valid_until IS NULL OR valid_until > datetime('now'))
              {pr_scope_clause}
            ORDER BY confidence DESC
            LIMIT 10
            """,
            pr_scope_params,
        ).fetchall()
    except Exception:
        return items
    for row in rule_rows:
        row_d = dict(row)
        tag_combo = row_d.get("tag_combination", "") or ""
        if not tag_combo:
            rule_scope: List[str] = []
        else:
            try:
                parsed = json.loads(tag_combo)
                rule_scope = parsed if isinstance(parsed, list) else (
                    [str(parsed)] if parsed else []
                )
            except (json.JSONDecodeError, TypeError):
                rule_scope = [t.strip() for t in tag_combo.split(",") if t.strip()]
        if not _scope_matches(rule_scope, scope_tags):
            continue
        content = (row_d.get("recommendation") or row_d.get("description") or "")[:MAX_CONTENT_CHARS_PER_ITEM]
        pr_title = (row_d.get("description") or "Prevention rule")[:120]
        items.append(IntelligenceItem(
            item_id=_stable_item_id("pr", str(row_d.get("id", ""))),
            item_class="failure_prevention",
            title=pr_title,
            content=content,
            confidence=float(row_d.get("confidence", 0.5)),
            evidence_count=max(1, int(row_d.get("triggered_count", 1))),
            last_seen=row_d.get("last_triggered") or _now_utc(),
            scope_tags=rule_scope,
            source_refs=[f"prevention_rule_{row_d['id']}"],
            task_class_filter=[],
            pattern_category=PATTERN_CATEGORY_PROCESS,
            content_hash=_content_hash(pr_title, content),
        ))
    return items


_CENTRAL_AP_COLS = (
    "id, title, description, category, severity, "
    "why_problematic, better_alternative, "
    "occurrence_count, first_seen, last_seen"
)
_CENTRAL_AP_ORDER = (
    "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC, "
    "occurrence_count DESC"
)
_CENTRAL_AP_WHERE = (
    "occurrence_count >= 1 AND (valid_until IS NULL OR valid_until > datetime('now'))"
)


def _central_row_to_item(row_d: dict, scope_tags: List[str]) -> Optional[IntelligenceItem]:
    """Convert a central-DB antipatterns row dict to IntelligenceItem, or None if filtered."""
    category = row_d.get("category", "")
    pattern_scope = [category] if category else []
    if not _scope_matches(pattern_scope, scope_tags):
        return None
    content_parts = []
    if row_d.get("why_problematic"):
        content_parts.append(row_d["why_problematic"])
    if row_d.get("better_alternative"):
        content_parts.append(f"Instead: {row_d['better_alternative']}")
    content = " ".join(content_parts)[:MAX_CONTENT_CHARS_PER_ITEM]
    severity = row_d.get("severity", "medium")
    confidence = _SEVERITY_CONFIDENCE.get(severity, 0.5)
    ap_title = (row_d.get("title") or "Failure prevention")[:120]
    return IntelligenceItem(
        item_id=_stable_item_id("ap", str(row_d.get("id", ""))),
        item_class="failure_prevention",
        title=ap_title,
        content=content,
        confidence=confidence,
        evidence_count=int(row_d.get("occurrence_count", 1)),
        last_seen=row_d.get("last_seen") or row_d.get("first_seen") or _now_utc(),
        scope_tags=pattern_scope,
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE,
        content_hash=_content_hash(ap_title, content),
    )


def _query_central(
    task_class: str,
    scope_tags: List[str],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    *,
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """Query antipatterns from central DB (independent connection, project_id-scoped)."""
    items: List[IntelligenceItem] = []
    conn = central_conn_fn()
    if conn is None:
        return items
    try:
        project_id = (project_id_fn or current_project_id)()
        has_project_id = _table_has_column(conn, "antipatterns", "project_id")
        if has_project_id and project_id:
            rows = conn.execute(
                f"SELECT {_CENTRAL_AP_COLS} FROM antipatterns "
                f"WHERE {_CENTRAL_AP_WHERE} AND project_id = ? "
                f"ORDER BY {_CENTRAL_AP_ORDER} LIMIT 5",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_CENTRAL_AP_COLS} FROM antipatterns "
                f"WHERE {_CENTRAL_AP_WHERE} "
                f"ORDER BY {_CENTRAL_AP_ORDER} LIMIT 5"
            ).fetchall()
        for row in rows:
            item = _central_row_to_item(dict(row), scope_tags)
            if item is not None:
                items.append(item)
    except sqlite3.Error as e:
        logger.debug("Central failure-prevention query failed: %s", e)
    finally:
        conn.close()
    return items
