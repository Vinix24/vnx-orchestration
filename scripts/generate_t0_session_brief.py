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

from model_inference_guard import evaluate_activity_routing, HINT

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


def _activity_sessions(conn: sqlite3.Connection, since: str) -> dict:
    """Load per-session rows grouped by activity -> model -> [session dicts].

    Returns raw per-session records (not pre-aggregated) so the inference guard can
    bucket by task-difficulty before any model-vs-model comparison. ``has_error_recovery``
    is loaded but deliberately NOT mapped to a reasoning-error signal — it conflates
    resilience / infra recovery with model errors (see model_inference_guard).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT
            session_model,
            primary_activity,
            total_output_tokens,
            duration_minutes,
            has_error_recovery
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
          AND primary_activity IS NOT NULL
    """, (since,))

    activity_sessions: dict = defaultdict(lambda: defaultdict(list))
    for row in cur.fetchall():
        model, activity, tokens, duration, err = row[0], row[1], row[2], row[3], row[4]
        activity_sessions[activity][model].append({
            "total_output_tokens": tokens or 0,
            "duration_minutes": duration,
            "has_error_recovery": bool(err),
            # No infra-excluded reasoning-error signal exists in session_analytics;
            # the guard treats this as non-diagnostic rather than blaming a model.
            "reasoning_error": None,
        })
    return activity_sessions


def get_model_routing_hints(conn: sqlite3.Connection, since: str) -> list:
    """Generate task-type routing hints, difficulty-controlled and infra-excluded.

    Routes through model_inference_guard: a hint is only emitted when two models share
    a task-difficulty bucket with >= MIN_COMPARABLE_SAMPLE sessions each AND a clean
    (infra-excluded) reasoning-error signal shows a meaningful gap. Otherwise the
    activity is dropped — the loop prefers no hint over a confounded one.
    """
    activity_sessions = _activity_sessions(conn, since)

    hints = []
    for activity, sessions_by_model in activity_sessions.items():
        result = evaluate_activity_routing(activity, sessions_by_model)
        if result.get("status") != HINT:
            continue
        hints.append({
            "task_type": result["task_type"],
            "recommended_model": result["recommended_model"],
            "confidence": result["confidence"],
            "evidence": result["evidence"],
        })

    return sorted(hints, key=lambda x: x["confidence"], reverse=True)


def get_active_concerns(conn: sqlite3.Connection, since: str) -> list:
    """Identify models with a defensible, difficulty-controlled quality concern.

    A concern is only raised when the inference guard yields a real routing hint —
    i.e. within a comparable difficulty bucket, on an infra-excluded reasoning-error
    signal, with a meaningful gap. A high ``has_error_recovery`` rate alone is NOT a
    concern: it is usually resilience (the model recovered from an infra/tool failure
    it diagnosed), so it never triggers a "switch model" recommendation here.
    """
    activity_sessions = _activity_sessions(conn, since)

    concerns = []
    for activity, sessions_by_model in activity_sessions.items():
        result = evaluate_activity_routing(activity, sessions_by_model)
        if result.get("status") != HINT:
            continue
        concerns.append({
            "model": result["avoid_model"],
            "concern": (
                f"Lagere reasoning-success bij {activity} taken binnen vergelijkbare "
                f"moeilijkheid ({result['bucket']} bucket)"
            ),
            "recommendation": (
                f"Overweeg {result['recommended_model']} voor {activity} taken — "
                f"{result['evidence']}"
            ),
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
