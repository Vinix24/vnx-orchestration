"""Bridge Phase 2 heuristic findings into intelligence DB tables.

Writes session-derived signals to success_patterns and antipatterns so that
intelligence_selector.py can inject them into future dispatches. Uses empty
category for universal scope matching (same convention as
learning_loop.py:persist_to_intelligence_db).
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

# .models puts scripts/lib on sys.path, so it is imported first.
from .models import SessionMetrics, SessionFlags, log
import content_class
from pattern_upsert import upsert_antipattern, upsert_success_pattern


def _upsert_pattern(conn: Any, title: str, description: str,
                    pattern_data_json: str, now: str, project_id: str) -> bool:
    return upsert_success_pattern(
        conn,
        project_id=project_id,
        pattern_type="approach",
        title=title,
        category="",
        description=description,
        pattern_data=pattern_data_json,
        confidence_score=0.7,
        usage_count=1,
        first_seen=now,
        last_used=now,
    ).inserted


def _upsert_antipattern(conn: Any, title: str, description: str,
                        why: str, severity: str,
                        pattern_data_json: str, now: str, project_id: str,
                        pattern_type: str = "approach",
                        always_update: tuple = ()) -> bool:
    return upsert_antipattern(
        conn,
        project_id=project_id,
        pattern_type=pattern_type,
        title=title,
        category="",
        description=description,
        pattern_data=pattern_data_json,
        why_problematic=why,
        severity=severity,
        occurrence_count=1,
        first_seen=now,
        last_seen=now,
        always_update=always_update,
    ).inserted


def _write_test_cycle_pattern(conn: Any, now: str, project_id: str):
    _upsert_pattern(
        conn,
        title="Test-driven workflow detected",
        description="Session contained test-run/edit cycles indicating test-driven workflow",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
        project_id=project_id,
    )


def _write_debugging_antipattern(conn: Any, metrics: SessionMetrics, now: str,
                                 project_id: str):
    _upsert_antipattern(
        conn,
        title="Extended debugging session",
        description=f"Session spent {metrics.duration_minutes:.0f} minutes primarily debugging",
        why="Prolonged debugging may indicate unclear problem definition or insufficient tests",
        severity="medium",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
        project_id=project_id,
    )


def _write_error_recovery_antipattern(conn: Any, now: str, project_id: str):
    _upsert_antipattern(
        conn,
        title="Error recovery required",
        description="Session required error recovery (repeated error signals detected)",
        why="Repeated errors suggest unclear instructions or environmental issues",
        severity="low",
        pattern_data_json=json.dumps({"source": "session_analysis"}),
        now=now,
        project_id=project_id,
    )


def _bridge_improvement_suggestions(conn: Any, now: str, project_id: str) -> int:
    _priority_to_severity = {"critical": "critical", "high": "high"}
    # antipatterns feed the intelligence injected into kimi, deepseek and glm prompts, so a
    # suggestion derived from client, personal or unclassified content must not enter them.
    # Only an explicitly unrestricted origin bridges; NULL (a legacy row) counts as restricted.
    open_classes = (content_class.FABRIC, content_class.OWN)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(improvement_suggestions)")}
    if "origin_class" not in columns:
        # A store below QI v34 has no origin on any row: every row is legacy, so none bridges.
        return 0
    suggestion_rows = conn.execute(
        "SELECT id, category, component, suggested_improvement, priority "
        "FROM improvement_suggestions "
        "WHERE priority IN ('critical', 'high') AND status = 'new' "
        f"AND origin_class IN ({','.join('?' for _ in open_classes)})",
        open_classes,
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
        # The latest priority always sets the severity, as before.
        _upsert_antipattern(
            conn,
            title=title,
            description=improvement,
            why=f"Priority {sg['priority']} improvement suggestion",
            severity=severity,
            pattern_data_json=pattern_data_json,
            now=now,
            project_id=project_id,
            pattern_type="suggestion",
            always_update=("severity",),
        )
        count += 1
    return count


def bridge_session_to_intelligence(conn: Any, metrics: SessionMetrics,
                                   flags: SessionFlags,
                                   project_id: Optional[str] = None):
    """Orchestrate bridge of Phase 2 findings into intelligence DB.

    ``project_id`` is the tenant the rows are stamped with. The runner passes
    its fail-closed ``_resolve_project_id()``; without it the id comes from
    ``resolve_stamp_project_id()`` (``VNX_PROJECT_ID``), which refuses to guess.
    """
    now = datetime.now().isoformat()
    patterns_written = 0
    antipatterns_written = 0

    try:
        if not project_id:
            from project_scope import resolve_stamp_project_id
            project_id = resolve_stamp_project_id()

        if flags.has_test_cycle:
            _write_test_cycle_pattern(conn, now, project_id)
            patterns_written += 1

        if flags.primary_activity == "debugging" and metrics.duration_minutes > 30:
            _write_debugging_antipattern(conn, metrics, now, project_id)
            antipatterns_written += 1

        if flags.has_error_recovery:
            _write_error_recovery_antipattern(conn, now, project_id)
            antipatterns_written += 1

        antipatterns_written += _bridge_improvement_suggestions(conn, now, project_id)

        log("INFO", f"  Bridge→intelligence: {patterns_written} success_patterns, "
                    f"{antipatterns_written} antipatterns")

    except Exception as e:
        log("WARNING", f"  bridge_session_to_intelligence failed: {e}")
        try:
            conn.rollback()
        except (sqlite3.Error, AttributeError) as rb_exc:
            log("WARNING", f"  rollback failed: {rb_exc}")
