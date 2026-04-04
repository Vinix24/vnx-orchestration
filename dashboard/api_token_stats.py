"""Token analytics and event-stream API handlers.

Extracted from serve_dashboard.py to keep module size manageable.
All path constants are accessed from serve_dashboard at call time so that
unittest.mock.patch on serve_dashboard attributes is respected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


def _sd():
    """Lazy accessor for serve_dashboard constants (avoids circular import)."""
    import serve_dashboard
    return serve_dashboard


# ---------- Events API ----------

_EVENT_TYPE_ICONS = {
    "dispatch_created": "dispatch",
    "dispatch_promoted": "dispatch",
    "receipt_complete": "receipt",
    "task_started": "task",
    "task_complete": "receipt",
    "gate_passed": "gate",
    "gate_failed": "gate",
    "context_pressure": "context",
    "open_item_created": "item",
    "open_item_resolved": "item",
}


def _parse_receipts_events(limit: int = 50) -> list[dict]:
    """Read last N events from t0_receipts.ndjson, skipping malformed lines."""
    sd = _sd()
    if not sd.RECEIPTS_PATH.exists():
        return []

    lines: list[str] = []
    try:
        with open(sd.RECEIPTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    events: list[dict] = []
    for line in lines[-(limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = record.get("event_type") or record.get("event") or "unknown"
        timestamp = record.get("timestamp") or ""
        terminal = record.get("terminal") or ""
        dispatch_id = record.get("dispatch_id") or ""
        status = record.get("status") or ""
        gate = record.get("gate") or ""

        pr_id = record.get("pr_id") or ""
        if not pr_id and record.get("type", "").startswith("PR-"):
            pr_id = record["type"].split()[0]

        mapped_type = event_type
        if event_type == "task_complete":
            if status == "success" and gate:
                mapped_type = "gate_passed"
            elif status in ("failure", "failed") and gate:
                mapped_type = "gate_failed"
            else:
                mapped_type = "receipt_complete"

        icon = _EVENT_TYPE_ICONS.get(mapped_type, "event")

        events.append({
            "type": mapped_type,
            "icon": icon,
            "timestamp": timestamp,
            "terminal": terminal,
            "dispatch_id": dispatch_id,
            "pr_id": pr_id,
            "gate": gate,
            "status": status,
            "summary": _event_summary(mapped_type, record),
        })

    return events


def _event_summary(event_type: str, record: dict) -> str:
    """Build a human-readable one-line summary for an event."""
    terminal = record.get("terminal") or "?"
    dispatch_id = record.get("dispatch_id") or ""
    short_dispatch = dispatch_id.split("-", 2)[-1][:30] if dispatch_id else ""
    gate = record.get("gate") or ""
    status = record.get("status") or ""

    if event_type == "gate_passed":
        return f"{gate} passed"
    if event_type == "gate_failed":
        return f"{gate} failed"
    if event_type == "receipt_complete":
        return f"Receipt: {short_dispatch} ({status})"
    if event_type == "task_started":
        return f"Task started: {short_dispatch}"
    if event_type == "context_pressure":
        pct = record.get("context_used_pct", "?")
        return f"Context pressure {pct}% on {terminal}"
    if event_type in ("dispatch_created", "dispatch_promoted"):
        return f"Dispatch {short_dispatch}"
    return short_dispatch or event_type


def _scan_dispatch_events() -> list[dict]:
    """Scan dispatch directories for lifecycle events based on file timestamps."""
    sd = _sd()
    events: list[dict] = []
    if not sd.DISPATCH_DIR.exists():
        return events

    for stage in sd.DISPATCH_STAGES:
        stage_dir = sd.DISPATCH_DIR / stage
        if not stage_dir.is_dir():
            continue
        for dispatch_file in stage_dir.iterdir():
            if not dispatch_file.name.endswith(".md"):
                continue
            try:
                mtime = dispatch_file.stat().st_mtime
                ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                continue

            dispatch_id = dispatch_file.stem
            pr_id = ""
            try:
                content = dispatch_file.read_text(encoding="utf-8")
                for line in content.splitlines()[:20]:
                    if line.startswith("PR-ID:"):
                        pr_id = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass

            if stage == "staging":
                event_type = "dispatch_created"
            elif stage in ("pending", "active"):
                event_type = "dispatch_promoted"
            elif stage == "completed":
                event_type = "dispatch_promoted"
            elif stage == "rejected":
                event_type = "dispatch_promoted"
            else:
                event_type = "dispatch_created"

            icon = _EVENT_TYPE_ICONS.get(event_type, "dispatch")
            summary = f"Dispatch moved to {stage}"

            events.append({
                "type": event_type,
                "icon": icon,
                "timestamp": ts,
                "terminal": "",
                "dispatch_id": dispatch_id,
                "pr_id": pr_id,
                "gate": "",
                "status": stage,
                "summary": summary,
            })

    return events


def _query_events(params: dict[str, list[str]]) -> dict:
    """Combine receipt events + dispatch events, filter, sort, and cap at 30."""
    terminal_filter = (params.get("terminal") or [None])[0]
    pr_filter = (params.get("pr") or [None])[0]
    type_filter = (params.get("type") or [None])[0]
    limit = 30

    receipt_events = _parse_receipts_events(limit=80)
    dispatch_events = _scan_dispatch_events()

    all_events = receipt_events + dispatch_events

    # Deduplicate: receipt events take priority over dispatch scan for same dispatch_id
    seen_dispatches: dict[str, dict] = {}
    unique_events: list[dict] = []
    for evt in all_events:
        did = evt.get("dispatch_id")
        if did and did in seen_dispatches:
            existing = seen_dispatches[did]
            if evt.get("type") != existing.get("type"):
                unique_events.append(evt)
        else:
            if did:
                seen_dispatches[did] = evt
            unique_events.append(evt)

    # Apply filters
    filtered: list[dict] = []
    for evt in unique_events:
        if terminal_filter and evt.get("terminal") != terminal_filter:
            continue
        if pr_filter and evt.get("pr_id") != pr_filter:
            continue
        if type_filter and evt.get("type") != type_filter:
            continue
        filtered.append(evt)

    # Sort by timestamp descending (most recent first)
    filtered.sort(key=lambda e: e.get("timestamp") or "", reverse=True)

    return {
        "events": filtered[:limit],
        "total": len(filtered),
        "filters": {
            "terminal": terminal_filter,
            "pr": pr_filter,
            "type": type_filter,
        },
    }


# ---------- Token Stats API ----------

_GROUP_SQL = {
    "day": "session_date",
    "week": "strftime('%Y-W%W', session_date)",
    "month": "strftime('%Y-%m', session_date)",
}


def _get_db() -> sqlite3.Connection | None:
    sd = _sd()
    if not sd.DB_PATH.exists() or sd.DB_PATH.stat().st_size == 0:
        return None
    conn = sqlite3.connect(str(sd.DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _token_stats_sql(group_expr: str, where: str) -> str:
    """Return the session_analytics aggregation SQL for the given group/where."""
    return f"""
        SELECT
            {group_expr} AS period,
            terminal,
            session_model AS model,
            COUNT(*) AS sessions,
            SUM(assistant_message_count) AS api_calls,
            ROUND(AVG(
                (total_input_tokens + cache_creation_tokens + cache_read_tokens) * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 0) AS context_per_call_K,
            ROUND(
                SUM(cache_read_tokens) * 100.0
                / NULLIF(SUM(total_input_tokens + cache_creation_tokens + cache_read_tokens), 0), 1
            ) AS cache_hit_pct,
            ROUND(AVG(
                (total_input_tokens + cache_creation_tokens) * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 1) AS new_per_call_K,
            ROUND(AVG(
                total_output_tokens * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 1) AS output_per_call_K,
            SUM(total_output_tokens) AS total_output_tokens,
            SUM(total_input_tokens) AS total_input_tokens,
            SUM(cache_creation_tokens) AS total_cache_creation_tokens,
            SUM(cache_read_tokens) AS total_cache_read_tokens,
            SUM(COALESCE(context_reset_count, CASE WHEN has_context_reset THEN 1 ELSE 0 END)) AS context_rotations,
            GROUP_CONCAT(DISTINCT primary_activity) AS activities
        FROM session_analytics
        WHERE {where}
        GROUP BY period, terminal, model
        ORDER BY period DESC, terminal
    """


def _query_token_stats(params: dict[str, list[str]]) -> list[dict]:
    conn = _get_db()
    if conn is None:
        return []

    date_from = (params.get("from") or [None])[0]
    date_to = (params.get("to") or [None])[0]
    group = (params.get("group") or ["day"])[0]
    terminal = (params.get("terminal") or [None])[0]
    model = (params.get("model") or [None])[0]

    if group not in _GROUP_SQL:
        group = "day"

    group_expr = _GROUP_SQL[group]
    today = datetime.now().strftime("%Y-%m-%d")
    if not date_from:
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today

    conditions = ["session_date >= ?", "session_date <= ?"]
    bind = [date_from, date_to]
    if terminal:
        conditions.append("terminal = ?")
        bind.append(terminal)
    if model:
        conditions.append("session_model = ?")
        bind.append(model)

    sql = _token_stats_sql(_GROUP_SQL[group], " AND ".join(conditions))
    try:
        rows = conn.execute(sql, bind).fetchall()
        result = [dict(r) for r in rows]
    finally:
        conn.close()
    return result


def _query_token_sessions(params: dict[str, list[str]]) -> list[dict]:
    conn = _get_db()
    if conn is None:
        return []

    date = (params.get("date") or [None])[0]
    terminal = (params.get("terminal") or [None])[0]

    if not date:
        return []

    conditions = ["session_date = ?"]
    bind: list[str] = [date]

    if terminal:
        conditions.append("terminal = ?")
        bind.append(terminal)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            session_id,
            terminal,
            session_model AS model,
            session_date AS date,
            assistant_message_count AS api_calls,
            ROUND(
                (total_input_tokens + cache_creation_tokens + cache_read_tokens) * 1.0
                / NULLIF(assistant_message_count, 0) / 1000.0, 0
            ) AS context_per_call_K,
            ROUND(
                cache_read_tokens * 100.0
                / NULLIF(total_input_tokens + cache_creation_tokens + cache_read_tokens, 0), 1
            ) AS cache_hit_pct,
            ROUND(
                total_output_tokens * 1.0
                / NULLIF(assistant_message_count, 0) / 1000.0, 1
            ) AS output_per_call_K,
            duration_minutes,
            primary_activity,
            tool_calls_total,
            has_error_recovery
        FROM session_analytics
        WHERE {where}
        ORDER BY assistant_message_count DESC
    """

    try:
        rows = conn.execute(sql, bind).fetchall()
        result = [dict(r) for r in rows]
    finally:
        conn.close()
    return result
