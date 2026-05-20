"""
Intelligence injection recording helpers.

Writes to:
  - intelligence_injections (coord DB) — audit trail
  - pattern_usage (quality DB) — feedback loop
  - dispatch_pattern_offered (quality DB) — per-dispatch offering junction
  - success_patterns / antipatterns source_dispatch_ids (quality DB) — decay linkage
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Callable, List, Optional

from ._common import (
    IntelligenceItem,
    _item_hash,
    _new_id,
    _now_utc,
    _table_has_column,
)

if TYPE_CHECKING:
    from ._models import InjectionResult

try:
    from project_scope import current_project_id, project_filter_enabled
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from project_scope import current_project_id, project_filter_enabled  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def record_injection_audit(
    result: "InjectionResult",
    state_dir: object,
    project_id: Optional[str],
) -> None:
    """Insert one row into intelligence_injections in the coord DB."""
    try:
        from runtime_coordination import get_connection
    except ImportError:
        return
    injection_id = _new_id()
    items_json = json.dumps([item.to_dict() for item in result.items])
    suppressed_json = json.dumps([s.to_dict() for s in result.suppressed])
    ab_arm = getattr(result, "ab_arm", "treatment") or "treatment"
    try:
        with get_connection(state_dir) as conn:
            has_project = _table_has_column(conn, "intelligence_injections", "project_id")
            has_ab_arm = _table_has_column(conn, "intelligence_injections", "ab_arm")
            if has_project and has_ab_arm:
                conn.execute(
                    """INSERT INTO intelligence_injections
                        (injection_id, dispatch_id, injection_point, task_class,
                         items_injected, items_suppressed, payload_chars,
                         items_json, suppressed_json, project_id, ab_arm)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (injection_id, result.dispatch_id, result.injection_point,
                     result.task_class, result.items_injected, result.items_suppressed,
                     result.payload_chars, items_json, suppressed_json, project_id, ab_arm),
                )
            elif has_project:
                conn.execute(
                    """INSERT INTO intelligence_injections
                        (injection_id, dispatch_id, injection_point, task_class,
                         items_injected, items_suppressed, payload_chars,
                         items_json, suppressed_json, project_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (injection_id, result.dispatch_id, result.injection_point,
                     result.task_class, result.items_injected, result.items_suppressed,
                     result.payload_chars, items_json, suppressed_json, project_id),
                )
            elif has_ab_arm:
                conn.execute(
                    """INSERT INTO intelligence_injections
                        (injection_id, dispatch_id, injection_point, task_class,
                         items_injected, items_suppressed, payload_chars,
                         items_json, suppressed_json, ab_arm)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (injection_id, result.dispatch_id, result.injection_point,
                     result.task_class, result.items_injected, result.items_suppressed,
                     result.payload_chars, items_json, suppressed_json, ab_arm),
                )
            else:
                conn.execute(
                    """INSERT INTO intelligence_injections
                        (injection_id, dispatch_id, injection_point, task_class,
                         items_injected, items_suppressed, payload_chars,
                         items_json, suppressed_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (injection_id, result.dispatch_id, result.injection_point,
                     result.task_class, result.items_injected, result.items_suppressed,
                     result.payload_chars, items_json, suppressed_json),
                )
            conn.commit()
    except sqlite3.Error as e:
        logger.warning("Failed to record injection audit: %s", e)


def record_pattern_usage(
    result: "InjectionResult",
    db: sqlite3.Connection,
    has_column_fn: Callable[[str, str], bool],
) -> None:
    """Write pattern_usage + dispatch_pattern_offered rows for the feedback loop."""
    if db is None:
        return
    now = _now_utc()
    project_id = current_project_id()
    pu_has_project = has_column_fn("pattern_usage", "project_id")
    dpo_has_project = has_column_fn("dispatch_pattern_offered", "project_id")
    try:
        db.execute(
            """CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
                dispatch_id   TEXT NOT NULL,
                pattern_id    TEXT NOT NULL,
                pattern_title TEXT NOT NULL,
                offered_at    TEXT NOT NULL,
                PRIMARY KEY (dispatch_id, pattern_id)
            )"""
        )
        dpo_has_project = has_column_fn("dispatch_pattern_offered", "project_id")
        for item in result.items:
            _upsert_pattern_usage(db, item, now, project_id, pu_has_project)
            _upsert_dispatch_pattern_offered(db, item, result.dispatch_id, now, project_id, dpo_has_project)
            _stamp_source_dispatch_id(db, item, result.dispatch_id)
        db.commit()
    except sqlite3.Error as e:
        logger.warning("Failed to record pattern usage: %s", e)


def stamp_source_dispatch_ids(
    result: "InjectionResult",
    db: Optional[sqlite3.Connection],
) -> int:
    """Public injection-time stamping: append dispatch_id to source row JSON arrays."""
    if not result.items or not result.dispatch_id or db is None:
        return 0
    stamped = 0
    for item in result.items:
        try:
            if _stamp_source_dispatch_id(db, item, result.dispatch_id):
                stamped += 1
        except Exception as exc:
            logger.warning("stamp_source_dispatch_ids: failed to stamp item %s: %s", getattr(item, "item_id", "?"), exc)
            continue
    try:
        db.commit()
    except sqlite3.Error as exc:
        logger.warning("stamp_source_dispatch_ids: commit failed: %s", exc)
    return stamped


def _upsert_pattern_usage(db, item, now, project_id, has_project):
    pattern_hash = _item_hash(item.item_id)
    if has_project:
        db.execute(
            """INSERT INTO pattern_usage
                (pattern_id, pattern_title, pattern_hash, used_count,
                 ignored_count, success_count, failure_count,
                 last_offered, confidence, created_at, updated_at, project_id)
               VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?)
               ON CONFLICT(pattern_id) DO UPDATE SET
                pattern_title = excluded.pattern_title,
                pattern_hash  = excluded.pattern_hash,
                last_offered  = excluded.last_offered,
                updated_at    = excluded.updated_at""",
            (item.item_id, item.title[:255], pattern_hash, now, item.confidence, now, now, project_id),
        )
    else:
        db.execute(
            """INSERT INTO pattern_usage
                (pattern_id, pattern_title, pattern_hash, used_count,
                 ignored_count, success_count, failure_count,
                 last_offered, confidence, created_at, updated_at)
               VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
               ON CONFLICT(pattern_id) DO UPDATE SET
                pattern_title = excluded.pattern_title,
                pattern_hash  = excluded.pattern_hash,
                last_offered  = excluded.last_offered,
                updated_at    = excluded.updated_at""",
            (item.item_id, item.title[:255], pattern_hash, now, item.confidence, now, now),
        )


def _upsert_dispatch_pattern_offered(db, item, dispatch_id, now, project_id, has_project):
    if has_project:
        db.execute(
            """INSERT INTO dispatch_pattern_offered
                (dispatch_id, pattern_id, pattern_title, offered_at, project_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(dispatch_id, pattern_id) DO UPDATE SET
                offered_at = excluded.offered_at""",
            (dispatch_id, item.item_id, item.title[:255], now, project_id),
        )
    else:
        db.execute(
            """INSERT INTO dispatch_pattern_offered
                (dispatch_id, pattern_id, pattern_title, offered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(dispatch_id, pattern_id) DO UPDATE SET
                offered_at = excluded.offered_at""",
            (dispatch_id, item.item_id, item.title[:255], now),
        )


def _stamp_source_dispatch_id(db, item, dispatch_id: str) -> bool:
    """Append dispatch_id to source_dispatch_ids on the originating pattern row."""
    if not dispatch_id:
        return False
    item_id = item.item_id or ""
    if item.item_class == "proven_pattern" and item_id.startswith("intel_sp_"):
        table, row_key = "success_patterns", item_id[len("intel_sp_"):]
    elif item.item_class == "failure_prevention" and item_id.startswith("intel_ap_"):
        table, row_key = "antipatterns", item_id[len("intel_ap_"):]
    else:
        return False
    try:
        row_id = int(row_key)
    except (ValueError, TypeError):
        return False
    has_pid = _table_has_column(db, table, "project_id")
    pid_clause = " AND project_id = ?" if (has_pid and project_filter_enabled()) else ""
    pid_params: tuple = (current_project_id(),) if pid_clause else ()
    try:
        row = db.execute(
            f"SELECT source_dispatch_ids FROM {table} WHERE id = ?{pid_clause}",
            (row_id, *pid_params),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    existing_json = row["source_dispatch_ids"] if isinstance(row, sqlite3.Row) else row[0]
    ids: List[str] = []
    if existing_json:
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, list):
                ids = [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            ids = []
    if dispatch_id in ids:
        return True
    ids.append(dispatch_id)
    ids = ids[-20:]
    try:
        db.execute(
            f"UPDATE {table} SET source_dispatch_ids = ? WHERE id = ?{pid_clause}",
            (json.dumps(ids), row_id, *pid_params),
        )
    except sqlite3.Error:
        return False
    return True
