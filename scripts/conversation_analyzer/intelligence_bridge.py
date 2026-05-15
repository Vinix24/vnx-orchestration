"""Bridge Phase 2 heuristic findings into intelligence DB tables.

Writes session-derived signals to success_patterns and antipatterns so that
intelligence_selector.py can inject them into future dispatches. Uses empty
category for universal scope matching (same convention as
learning_loop.py:persist_to_intelligence_db).
"""

import json
import sqlite3
from datetime import datetime
from typing import Any

from .models import SessionMetrics, SessionFlags, log


def _upsert_pattern(conn: Any, title: str, description: str,
                    pattern_data_json: str, now: str) -> bool:
    existing = conn.execute(
        "SELECT id, usage_count FROM success_patterns "
        "WHERE title = ? AND pattern_data LIKE '%session_analysis%'",
        (title,),
    ).fetchone()
    if existing:
        row = dict(existing)
        conn.execute(
            "UPDATE success_patterns SET usage_count = ?, last_used = ? WHERE id = ?",
            (row["usage_count"] + 1, now, row["id"]),
        )
        return False
    conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, pattern_data, "
        " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("approach", "", title, description, pattern_data_json, 0.7, 1, "[]", now, now),
    )
    return True


def _upsert_antipattern(conn: Any, title: str, description: str,
                        why: str, severity: str,
                        pattern_data_json: str, now: str) -> bool:
    existing = conn.execute(
        "SELECT id, occurrence_count FROM antipatterns "
        "WHERE title = ? AND pattern_data LIKE '%session_analysis%'",
        (title,),
    ).fetchone()
    if existing:
        row = dict(existing)
        conn.execute(
            "UPDATE antipatterns SET occurrence_count = ?, last_seen = ? WHERE id = ?",
            (row["occurrence_count"] + 1, now, row["id"]),
        )
        return False
    conn.execute(
        "INSERT INTO antipatterns "
        "(pattern_type, category, title, description, pattern_data, "
        " why_problematic, severity, occurrence_count, "
        " source_dispatch_ids, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("approach", "", title, description, pattern_data_json,
         why, severity, 1, "[]", now, now),
    )
    return True


def _write_test_cycle_pattern(conn: Any, now: str):
    _upsert_pattern(
        conn,
        title="Test-driven workflow detected",
        description="Session contained test-run/edit cycles indicating test-driven workflow",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
    )


def _write_debugging_antipattern(conn: Any, metrics: SessionMetrics, now: str):
    _upsert_antipattern(
        conn,
        title="Extended debugging session",
        description=f"Session spent {metrics.duration_minutes:.0f} minutes primarily debugging",
        why="Prolonged debugging may indicate unclear problem definition or insufficient tests",
        severity="medium",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
    )


def _write_error_recovery_antipattern(conn: Any, now: str):
    _upsert_antipattern(
        conn,
        title="Error recovery required",
        description="Session required error recovery (repeated error signals detected)",
        why="Repeated errors suggest unclear instructions or environmental issues",
        severity="low",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
    )


def _bridge_improvement_suggestions(conn: Any, now: str) -> int:
    _priority_to_severity = {"critical": "critical", "high": "high"}
    suggestion_rows = conn.execute(
        "SELECT id, category, component, suggested_improvement, priority "
        "FROM improvement_suggestions "
        "WHERE priority IN ('critical', 'high') AND status = 'new'",
    ).fetchall()

    count = 0
    for sg_row in suggestion_rows:
        sg = dict(sg_row)
        component = sg.get("component") or "unknown"
        improvement = sg.get("suggested_improvement", "")
        raw_title = f"[{sg['priority'].upper()}] {component}: {improvement}"
        title = raw_title[:120]
        severity = _priority_to_severity.get(sg["priority"], "high")
        pattern_data_json = json.dumps({"source": "session_analysis",
                                        "suggestion_id": sg["id"]})

        existing = conn.execute(
            "SELECT id, occurrence_count FROM antipatterns "
            "WHERE title = ? AND pattern_data LIKE '%session_analysis%'",
            (title,),
        ).fetchone()
        if existing:
            ex = dict(existing)
            conn.execute(
                "UPDATE antipatterns SET occurrence_count = ?, severity = ?, "
                "last_seen = ? WHERE id = ?",
                (ex["occurrence_count"] + 1, severity, now, ex["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO antipatterns "
                "(pattern_type, category, title, description, pattern_data, "
                " why_problematic, severity, occurrence_count, "
                " source_dispatch_ids, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("suggestion", "", title, improvement, pattern_data_json,
                 f"Priority {sg['priority']} improvement suggestion",
                 severity, 1, "[]", now, now),
            )
        count += 1
    return count


def bridge_session_to_intelligence(conn: Any, metrics: SessionMetrics,
                                   flags: SessionFlags):
    """Orchestrate bridge of Phase 2 findings into intelligence DB."""
    now = datetime.now().isoformat()
    patterns_written = 0
    antipatterns_written = 0

    try:
        if flags.has_test_cycle:
            _write_test_cycle_pattern(conn, now)
            patterns_written += 1

        if flags.primary_activity == "debugging" and metrics.duration_minutes > 30:
            _write_debugging_antipattern(conn, metrics, now)
            antipatterns_written += 1

        if flags.has_error_recovery:
            _write_error_recovery_antipattern(conn, now)
            antipatterns_written += 1

        antipatterns_written += _bridge_improvement_suggestions(conn, now)

        conn.commit()
        log("INFO", f"  Bridge→intelligence: {patterns_written} success_patterns, "
                    f"{antipatterns_written} antipatterns")

    except Exception as e:
        log("WARNING", f"  bridge_session_to_intelligence failed: {e}")
        try:
            conn.rollback()
        except (sqlite3.Error, AttributeError) as rb_exc:
            log("WARNING", f"  rollback failed: {rb_exc}")
