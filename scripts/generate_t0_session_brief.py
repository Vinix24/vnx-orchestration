#!/usr/bin/env python3
"""
Generate T0 Session Brief — Model-based performance summary.

Reads session_analytics from quality_intelligence.db grouped by session_model
and writes t0_session_brief.json to VNX_STATE_DIR.

This is a read-only state file consumed by T0 for dispatch intelligence.
It runs automatically as part of the nightly pipeline.
"""

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_UTC = timezone.utc
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
OUTPUT_PATH = STATE_DIR / "t0_session_brief.json"

LOOKBACK_DAYS = 7


def get_model_performance(conn: sqlite3.Connection, since: str) -> dict:
    """Aggregate session metrics per model family."""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            session_model,
            COUNT(*) as session_count,
            COALESCE(AVG(total_output_tokens), 0) as avg_tokens,
            COALESCE(AVG(duration_minutes), 0) as avg_duration,
            COALESCE(SUM(CASE WHEN has_error_recovery = 1 THEN 1 ELSE 0 END), 0) as error_sessions,
            COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
            COALESCE(SUM(cache_creation_tokens), 0) as total_cache_create,
            primary_activity
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
        GROUP BY session_model, primary_activity
        ORDER BY session_model, session_count DESC
    """, (since,))

    model_data = defaultdict(lambda: {
        "sessions_7d": 0,
        "avg_tokens_per_session": 0,
        "primary_activities": defaultdict(int),
        "error_recovery_rate": 0.0,
        "cache_hit_ratio": 0.0,
        "avg_duration_minutes": 0.0,
        "_total_tokens": 0,
        "_total_duration": 0.0,
        "_error_count": 0,
        "_cache_read": 0,
        "_cache_create": 0,
    })

    for row in cur.fetchall():
        model = row[0]
        count = row[1]
        avg_tok = row[2]
        avg_dur = row[3]
        err_count = row[4]
        cache_read = row[5]
        cache_create = row[6]
        activity = row[7] or "unknown"

        d = model_data[model]
        d["sessions_7d"] += count
        d["primary_activities"][activity] += count
        d["_total_tokens"] += avg_tok * count
        d["_total_duration"] += avg_dur * count
        d["_error_count"] += err_count
        d["_cache_read"] += cache_read
        d["_cache_create"] += cache_create

    result = {}
    for model, d in model_data.items():
        total = d["sessions_7d"]
        if total == 0:
            continue
        total_cache = d["_cache_read"] + d["_cache_create"]
        result[model] = {
            "sessions_7d": total,
            "avg_tokens_per_session": round(d["_total_tokens"] / total),
            "primary_activities": dict(d["primary_activities"]),
            "error_recovery_rate": round(d["_error_count"] / total, 2),
            "cache_hit_ratio": round(d["_cache_read"] / total_cache, 2) if total_cache > 0 else 0.0,
            "avg_duration_minutes": round(d["_total_duration"] / total, 1),
        }

    return result


def get_model_routing_hints(conn: sqlite3.Connection, since: str) -> list:
    """Generate task-type routing hints based on model success patterns."""
    cur = conn.cursor()

    # Get activity success rates per model (error_recovery = proxy for difficulty)
    cur.execute("""
        SELECT
            session_model,
            primary_activity,
            COUNT(*) as total,
            SUM(CASE WHEN has_error_recovery = 0 THEN 1 ELSE 0 END) as success_count
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
          AND primary_activity IS NOT NULL
        GROUP BY session_model, primary_activity
        HAVING COUNT(*) >= 3
        ORDER BY primary_activity, success_count DESC
    """, (since,))

    activity_model_stats = defaultdict(dict)
    for row in cur.fetchall():
        model, activity, total, success = row[0], row[1], row[2], row[3]
        rate = success / total if total > 0 else 0
        activity_model_stats[activity][model] = {
            "total": total,
            "success": success,
            "rate": rate,
        }

    hints = []
    for activity, models in activity_model_stats.items():
        if len(models) < 2:
            continue

        sorted_models = sorted(models.items(), key=lambda x: x[1]["rate"], reverse=True)
        best_model, best_stats = sorted_models[0]
        second_model, second_stats = sorted_models[1]

        if best_stats["rate"] - second_stats["rate"] < 0.15:
            continue

        confidence = round(min(0.95, 0.5 + (best_stats["total"] / 20) * 0.3 +
                              (best_stats["rate"] - second_stats["rate"]) * 0.5), 2)

        hints.append({
            "task_type": activity,
            "recommended_model": best_model,
            "confidence": confidence,
            "evidence": (
                f"{best_stats['success']}/{best_stats['total']} succesvol op {best_model}, "
                f"{second_stats['success']}/{second_stats['total']} op {second_model}"
            ),
        })

    return sorted(hints, key=lambda x: x["confidence"], reverse=True)


def get_active_concerns(conn: sqlite3.Connection, since: str) -> list:
    """Identify models with concerning error patterns."""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            session_model,
            primary_activity,
            COUNT(*) as total,
            SUM(CASE WHEN has_error_recovery = 1 THEN 1 ELSE 0 END) as err_count
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
        GROUP BY session_model, primary_activity
        HAVING COUNT(*) >= 3
          AND (CAST(SUM(CASE WHEN has_error_recovery = 1 THEN 1 ELSE 0 END) AS REAL) / COUNT(*)) > 0.30
    """, (since,))

    concerns = []
    for row in cur.fetchall():
        model, activity, total, err_count = row[0], row[1], row[2], row[3]
        rate = round(err_count / total * 100)
        concerns.append({
            "model": model,
            "concern": f"Hoge error recovery rate bij {activity} taken ({rate}%)",
            "recommendation": f"Overweeg een ander model voor complexe {activity} taken",
        })

    return concerns


def generate_brief() -> dict:
    """Generate the complete T0 session brief."""
    if not DB_PATH.exists():
        return {"error": "quality_intelligence.db not found", "generated_at": datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z")}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    model_performance = get_model_performance(conn, since)
    routing_hints = get_model_routing_hints(conn, since)
    concerns = get_active_concerns(conn, since)

    # Session volume
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total FROM session_analytics WHERE session_date >= ?
    """, (since,))
    total_sessions = (cur.fetchone() or [0])[0]

    by_model = {}
    for model, data in model_performance.items():
        by_model[model] = data["sessions_7d"]

    conn.close()

    return {
        "generated_at": datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z"),
        "lookback_days": LOOKBACK_DAYS,
        "model_performance": model_performance,
        "model_routing_hints": routing_hints,
        "active_concerns": concerns,
        "session_volume": {
            "total_7d": total_sessions,
            "by_model": by_model,
        },
    }


def main():
    brief = generate_brief()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")

    model_count = len(brief.get("model_performance", {}))
    hint_count = len(brief.get("model_routing_hints", []))
    concern_count = len(brief.get("active_concerns", []))
    total = brief.get("session_volume", {}).get("total_7d", 0)

    print(f"T0 Session Brief generated: {OUTPUT_PATH}")
    print(f"  Models: {model_count} | Hints: {hint_count} | Concerns: {concern_count} | Sessions: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
