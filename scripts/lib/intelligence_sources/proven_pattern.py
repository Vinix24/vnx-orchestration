"""
proven_pattern source — success_patterns lookup + scoring.

Handles per-project DB query, central DB query, and shadow comparison dispatch.
Canonical-ID resolution and diversity/governance helpers live in _common.py.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from ._common import (
    MAX_CONTENT_CHARS_PER_ITEM,
    IntelligenceItem,
    _content_hash,
    _now_utc,
    _project_scope_clause,
    _scope_matches,
    _short_content_hash,
    _stable_item_id,
    _table_has_column,
    classify_pattern_category,
)
try:
    from project_scope import current_project_id, project_filter_enabled
except ImportError:
    from project_scope import current_project_id, project_filter_enabled  # type: ignore

try:
    import shadow_verifier as _shadow_verifier
    import shadow_logger as _shadow_logger
except ImportError:
    _shadow_verifier = None  # type: ignore[assignment]
    _shadow_logger = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _resolve_canonical_id(db, short_hash, *, fallback_id, cache, has_content_hash_col, has_project_id_col=False, project_id=None):
    """Return the canonical (smallest-id) row key for a given content hash."""
    fallback_key = str(fallback_id) if fallback_id is not None else ""
    if not short_hash or not has_content_hash_col:
        return fallback_key
    cache_key = (project_id, short_hash) if has_project_id_col else short_hash
    if cache_key in cache:
        return cache[cache_key]
    try:
        if has_project_id_col and project_filter_enabled():
            row = db.execute("SELECT MIN(id) FROM success_patterns WHERE content_hash = ? AND project_id = ?", (short_hash, project_id)).fetchone()
        else:
            row = db.execute("SELECT MIN(id) FROM success_patterns WHERE content_hash = ?", (short_hash,)).fetchone()
    except sqlite3.Error:
        cache[cache_key] = fallback_key
        return fallback_key
    canonical_id = row[0] if row is not None else None
    if canonical_id is None:
        cache[cache_key] = fallback_key
        return fallback_key
    canonical_key = str(canonical_id)
    cache[cache_key] = canonical_key
    return canonical_key


def _fetch_canonical_row(db, canonical_id, *, select_cols):
    """Fetch the canonical row's data after a duplicate-remap lookup."""
    if canonical_id in (None, ""):
        return None
    try:
        row = db.execute(f"SELECT {select_cols} FROM success_patterns WHERE id = ?", (canonical_id,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


_PROVEN_PATTERNS_SQL_TEMPLATE = (
    "SELECT id, title, description, confidence_score, usage_count "
    "FROM success_patterns "
    "WHERE (valid_until IS NULL OR valid_until > datetime('now')) "
    "ORDER BY confidence_score DESC LIMIT 20"
)


def query_proven_patterns(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    *,
    has_column_fn: Callable[[str, str], bool],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    reconcile_fn: Callable[[], None],
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """3-state dispatcher: per-project | central | shadow (metric 3, success_patterns)."""
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _query_per_project(db, task_class, scope_tags, has_column_fn, reconcile_fn)
    if flag == "1":
        return _query_central(task_class, scope_tags, central_conn_fn, project_id_fn=project_id_fn)
    legacy = _query_per_project(db, task_class, scope_tags, has_column_fn, reconcile_fn)
    central = _query_central(task_class, scope_tags, central_conn_fn, project_id_fn=project_id_fn)
    _maybe_shadow_compare(legacy, central, task_class)
    return legacy


def _maybe_shadow_compare(legacy, central, task_class):
    if _shadow_verifier is None:
        return
    project_id = current_project_id()
    try:
        cmp = _shadow_verifier.compare(
            [item.to_dict() for item in legacy],
            [item.to_dict() for item in central],
            project_id=project_id,
            read_site="IntelligenceSelector._query_proven_patterns",
            sql_template=_PROVEN_PATTERNS_SQL_TEMPLATE,
            metric_id=3,
        )
        if cmp.divergences and _shadow_logger is not None:
            _shadow_logger.write_comparison_result(
                cmp, project_id, "IntelligenceSelector._query_proven_patterns",
            )
    except Exception as e:
        logger.debug("Shadow compare (proven_patterns) skipped: %s", e)


def _query_per_project(
    db: sqlite3.Connection,
    task_class: str,
    scope_tags: List[str],
    has_column_fn: Callable[[str, str], bool],
    reconcile_fn: Callable[[], None],
) -> List[IntelligenceItem]:
    """Query success_patterns from the per-project DB."""
    reconcile_fn()
    items: List[IntelligenceItem] = []
    has_pattern_cat = has_column_fn("success_patterns", "pattern_category")
    has_content_hash_col = has_column_fn("success_patterns", "content_hash")
    has_project_id_col = has_column_fn("success_patterns", "project_id")
    select_cols = (
        "id, title, description, category, confidence_score, "
        "usage_count, source_dispatch_ids, first_seen, last_used"
    )
    if has_pattern_cat:
        select_cols += ", pattern_category"
    if has_content_hash_col:
        select_cols += ", content_hash"
    if has_project_id_col:
        select_cols += ", project_id"
    scope_clause, scope_params = _project_scope_clause(has_project_id_col)
    active_project_id = current_project_id() if has_project_id_col else None
    try:
        rows = db.execute(
            f"""SELECT {select_cols} FROM success_patterns
            WHERE (valid_until IS NULL OR valid_until > datetime('now'))
              {scope_clause}
            ORDER BY confidence_score DESC LIMIT 20""",
            scope_params,
        ).fetchall()
    except Exception as exc:
        logger.warning("proven_pattern per-project query failed: %s", exc)
        return items
    canonical_cache: Dict[Any, Any] = {}
    for row in rows:
        item = _per_project_row_to_item(
            row, scope_tags, select_cols, db,
            has_content_hash_col, has_project_id_col, active_project_id, canonical_cache,
        )
        if item is not None:
            items.append(item)
    return items


def _per_project_row_to_item(
    row: Any, scope_tags: List[str], select_cols: str, db: sqlite3.Connection,
    has_content_hash_col: bool, has_project_id_col: bool,
    active_project_id: Optional[str], canonical_cache: Dict[Any, Any],
) -> Optional[IntelligenceItem]:
    """Convert a per-project success_patterns row to IntelligenceItem."""
    row_d = dict(row)
    category = row_d.get("category", "")
    if not _scope_matches([category] if category else [], scope_tags):
        return None
    title = (row_d.get("title") or "Proven pattern")[:120]
    content = (row_d.get("description") or "")[:MAX_CONTENT_CHARS_PER_ITEM]
    stored_short_hash = row_d.get("content_hash") if has_content_hash_col else None
    short_hash = stored_short_hash or _short_content_hash(title, content)
    row_project_id = row_d.get("project_id") if has_project_id_col else None
    canonical_key = _resolve_canonical_id(
        db, short_hash, fallback_id=row_d.get("id"), cache=canonical_cache,
        has_content_hash_col=has_content_hash_col, has_project_id_col=has_project_id_col,
        project_id=row_project_id or active_project_id,
    )
    canonical_row_d = row_d
    if canonical_key and canonical_key != str(row_d.get("id") or ""):
        fetched = _fetch_canonical_row(db, canonical_key, select_cols=select_cols)
        if fetched is not None:
            canonical_row_d = fetched
    canonical_category = canonical_row_d.get("category", "") or ""
    if not _scope_matches([canonical_category] if canonical_category else [], scope_tags):
        return None
    source_refs = []
    if canonical_row_d.get("source_dispatch_ids"):
        try:
            import json
            source_refs = json.loads(canonical_row_d["source_dispatch_ids"])
        except (ValueError, TypeError):
            pass
    canonical_title = (canonical_row_d.get("title") or title)[:120]
    canonical_content = (canonical_row_d.get("description") or content)[:MAX_CONTENT_CHARS_PER_ITEM]
    last_seen = canonical_row_d.get("last_used") or canonical_row_d.get("first_seen") or _now_utc()
    stored_cat = canonical_row_d.get("pattern_category")
    pattern_category = stored_cat or classify_pattern_category(canonical_title, canonical_content)
    return IntelligenceItem(
        item_id=_stable_item_id("sp", canonical_key),
        item_class="proven_pattern",
        title=canonical_title,
        content=canonical_content,
        confidence=float(canonical_row_d.get("confidence_score", 0.0)),
        evidence_count=int(canonical_row_d.get("usage_count", 0)),
        last_seen=last_seen,
        scope_tags=[canonical_category] if canonical_category else [],
        source_refs=source_refs[:5],
        task_class_filter=[],
        pattern_category=pattern_category,
        content_hash=_content_hash(canonical_title, canonical_content),
    )


def _query_central(
    task_class: str,
    scope_tags: List[str],
    central_conn_fn: Callable[[], Optional[sqlite3.Connection]],
    *,
    project_id_fn: Optional[Callable[[], Optional[str]]] = None,
) -> List[IntelligenceItem]:
    """Query success_patterns from central DB (independent connection, project-scoped)."""
    items: List[IntelligenceItem] = []
    conn = central_conn_fn()
    if conn is None:
        return items
    try:
        project_id = (project_id_fn or current_project_id)()
        has_pattern_cat = _table_has_column(conn, "success_patterns", "pattern_category")
        has_content_hash_col = _table_has_column(conn, "success_patterns", "content_hash")
        has_project_id_col = _table_has_column(conn, "success_patterns", "project_id")
        select_cols = (
            "id, title, description, category, confidence_score, "
            "usage_count, source_dispatch_ids, first_seen, last_used"
        )
        if has_pattern_cat:
            select_cols += ", pattern_category"
        if has_content_hash_col:
            select_cols += ", content_hash"
        if has_project_id_col:
            select_cols += ", project_id"
        if has_project_id_col and project_id:
            rows = conn.execute(
                f"""SELECT {select_cols} FROM success_patterns
                WHERE (valid_until IS NULL OR valid_until > datetime('now'))
                  AND project_id = ?
                ORDER BY confidence_score DESC LIMIT 20""",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT {select_cols} FROM success_patterns
                WHERE (valid_until IS NULL OR valid_until > datetime('now'))
                ORDER BY confidence_score DESC LIMIT 20"""
            ).fetchall()
        for row in rows:
            item = _central_row_to_item(row, scope_tags)
            if item is not None:
                items.append(item)
    except sqlite3.Error as e:
        logger.debug("Central proven-patterns query failed: %s", e)
    finally:
        conn.close()
    return items


def _central_row_to_item(row: Any, scope_tags: List[str]) -> Optional[IntelligenceItem]:
    """Convert a central-DB success_patterns row to IntelligenceItem (no canonical remap)."""
    row_d = dict(row)
    category = row_d.get("category", "")
    if not _scope_matches([category] if category else [], scope_tags):
        return None
    title = (row_d.get("title") or "Proven pattern")[:120]
    content = (row_d.get("description") or "")[:MAX_CONTENT_CHARS_PER_ITEM]
    stored_cat = row_d.get("pattern_category")
    pattern_category = stored_cat or classify_pattern_category(title, content)
    return IntelligenceItem(
        item_id=_stable_item_id("sp", str(row_d.get("id", ""))),
        item_class="proven_pattern",
        title=title,
        content=content,
        confidence=float(row_d.get("confidence_score", 0.0)),
        evidence_count=int(row_d.get("usage_count", 0)),
        last_seen=row_d.get("last_used") or row_d.get("first_seen") or _now_utc(),
        scope_tags=[category] if category else [],
        task_class_filter=[],
        pattern_category=pattern_category,
        content_hash=_content_hash(title, content),
    )
