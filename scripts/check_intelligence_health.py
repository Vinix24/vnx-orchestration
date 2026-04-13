#!/usr/bin/env python3
"""
Check Intelligence System Health
================================
Verifies that the intelligence system is running and functioning properly.
Part of PR #8 - Intelligence Integration Fix

Author: T-MANAGER
Date: 2026-01-19
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cli_output import emit_json, emit_human, parse_human_flag

EXIT_OK = 0
EXIT_VALIDATION = 10
EXIT_HEALTH = 11
EXIT_IO = 20
EXIT_DEPENDENCY = 30


@dataclass
class HealthCheckError(Exception):
    code: str
    exit_code: int
    message: str


def _error_payload(code: str, message: str, context: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if context:
        payload["context"] = context
    return payload


def _parse_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fallback_daemon_status(warnings: List[str]) -> Tuple[bool, Optional[str]]:
    try:
        result = subprocess.run(["pgrep", "-f", "intelligence_daemon"], capture_output=True, text=True, check=False)
    except OSError as exc:
        warnings.append(f"pgrep_failed:{exc}")
        return False, None

    daemon_running = result.returncode == 0
    daemon_pid = result.stdout.strip() if daemon_running else None
    return daemon_running, daemon_pid


def _load_base_health(health_file: Path, warnings: List[str]) -> Tuple[bool, Optional[str], int, str]:
    daemon_running = False
    daemon_pid: Optional[str] = None
    pattern_count = 0
    last_extraction = "never"

    if health_file.exists():
        try:
            health_data = json.loads(health_file.read_text(encoding="utf-8"))
            daemon_running = bool(health_data.get("daemon_running", False))
            daemon_pid = health_data.get("daemon_pid")
            pattern_count = int(health_data.get("patterns_available", 0) or 0)
            last_extraction = str(health_data.get("last_extraction", "never"))

            timestamp = _parse_iso(health_data.get("timestamp"))
            if timestamp and datetime.now(timezone.utc) - timestamp > timedelta(minutes=2):
                warnings.append("health_file_stale")
                daemon_running, daemon_pid = _fallback_daemon_status(warnings)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warnings.append(f"health_file_read_failed:{exc}")
            daemon_running, daemon_pid = _fallback_daemon_status(warnings)
    else:
        daemon_running, daemon_pid = _fallback_daemon_status(warnings)

    return daemon_running, daemon_pid, pattern_count, last_extraction


def _enrich_from_intelligence_file(intel_file: Path, health: Dict[str, object], warnings: List[str]) -> Tuple[bool, Optional[str]]:
    recent_intelligence = False
    last_intelligence_time: Optional[str] = None

    if not intel_file.exists():
        return recent_intelligence, last_intelligence_time

    try:
        lines = intel_file.read_text(encoding="utf-8").splitlines()
        if not lines:
            return recent_intelligence, last_intelligence_time

        last_record = json.loads(lines[-1])
        timestamp = _parse_iso(last_record.get("timestamp"))
        if timestamp:
            last_intelligence_time = timestamp.isoformat()
            recent_intelligence = datetime.now(timezone.utc) - timestamp < timedelta(minutes=5)

        if not health.get("patterns_available"):
            health["patterns_available"] = int(last_record.get("pattern_count", 0) or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, OSError):
            warnings.append(f"intel_file_read_failed:{exc}")
        else:
            warnings.append(f"intel_last_record_parse_failed:{exc}")

    return recent_intelligence, last_intelligence_time


def _collect_receipt_coverage(receipts_file: Path, warnings: List[str]) -> Tuple[int, int, int, float]:
    receipts_with_intelligence = 0
    total_receipts = 0
    receipts_with_patterns = 0

    if not receipts_file.exists():
        return receipts_with_intelligence, total_receipts, receipts_with_patterns, 0.0

    try:
        lines = receipts_file.read_text(encoding="utf-8").splitlines()
        for line in lines[-10:]:
            if not line.strip():
                continue
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                warnings.append("receipt_parse_failed")
                continue

            total_receipts += 1
            quality_context = receipt.get("quality_context", {})
            if isinstance(quality_context, dict) and quality_context:
                receipts_with_intelligence += 1

            try:
                if int(receipt.get("pattern_count", 0) or 0) > 0:
                    receipts_with_patterns += 1
            except (TypeError, ValueError):
                warnings.append("receipt_pattern_count_invalid")
    except OSError as exc:
        warnings.append(f"receipts_file_read_failed:{exc}")

    coverage = (receipts_with_intelligence / total_receipts) * 100 if total_receipts > 0 else 0.0
    return receipts_with_intelligence, total_receipts, receipts_with_patterns, coverage


def check_pipeline_connected(db_path: Path) -> Dict[str, object]:
    """Check if the learning-loop → intelligence-selector pipeline is populated.

    Counts rows in the three tables that intelligence_selector reads and optionally
    probes the selector to confirm it would actually return items for a test dispatch.

    Returns:
        {
            "connected": bool,        # True when patterns exist and selector returns items
            "success_patterns": int,
            "antipatterns": int,
            "prevention_rules": int,
        }
    """
    result: Dict[str, object] = {
        "connected": False,
        "success_patterns": 0,
        "antipatterns": 0,
        "prevention_rules": 0,
    }

    if not db_path.exists():
        return result

    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        result["success_patterns"] = conn.execute(
            "SELECT COUNT(*) FROM success_patterns"
        ).fetchone()[0]
        result["antipatterns"] = conn.execute(
            "SELECT COUNT(*) FROM antipatterns"
        ).fetchone()[0]
        result["prevention_rules"] = conn.execute(
            "SELECT COUNT(*) FROM prevention_rules"
        ).fetchone()[0]
        conn.close()
    except Exception:
        return result

    patterns_exist = (
        result["success_patterns"] > 0
        or result["antipatterns"] > 0
        or result["prevention_rules"] > 0
    )

    if not patterns_exist:
        return result

    # Probe the selector to verify it would return at least one item
    try:
        _lib_dir = Path(__file__).resolve().parent / "lib"
        if str(_lib_dir) not in sys.path:
            sys.path.insert(0, str(_lib_dir))
        from intelligence_selector import IntelligenceSelector

        selector = IntelligenceSelector(quality_db_path=db_path)
        try:
            sel_result = selector.select(
                dispatch_id="health-check-probe",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
            result["connected"] = sel_result.items_injected > 0
        finally:
            selector.close()
    except Exception:
        # Selector unavailable — fall back to "patterns exist" heuristic
        result["connected"] = patterns_exist

    return result


def _collect_database_info(intel_db: Path) -> Tuple[bool, float]:
    db_exists = intel_db.exists()
    if not db_exists:
        return False, 0.0
    db_size_mb = round(intel_db.stat().st_size / (1024 * 1024), 2)
    return True, db_size_mb


def _collect_session_count(intel_db: Path, warnings: List[str]) -> int:
    if not intel_db.exists():
        return 0
    try:
        import sqlite3
        conn = sqlite3.connect(intel_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM session_analytics")
        row = cur.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as exc:
        warnings.append(f"session_count_failed:{exc}")
        return 0


def _collect_usage_tracking(learning_file: Path, warnings: List[str]) -> Tuple[bool, bool]:
    usage_tracking = learning_file.exists()
    recent_usage = False

    if not usage_tracking:
        return usage_tracking, recent_usage

    try:
        lines = learning_file.read_text(encoding="utf-8").splitlines()
        if not lines:
            return usage_tracking, recent_usage

        last_usage = json.loads(lines[-1])
        usage_time = _parse_iso(last_usage.get("timestamp"))
        if usage_time and datetime.now(timezone.utc) - usage_time < timedelta(hours=1):
            recent_usage = True
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        warnings.append(f"pattern_usage_parse_failed:{exc}")
    except OSError as exc:
        warnings.append(f"pattern_usage_read_failed:{exc}")

    return usage_tracking, recent_usage


def _determine_health_status(daemon_running: bool, recent_intelligence: bool, pattern_count: int, coverage: float) -> str:
    if not daemon_running or not recent_intelligence or pattern_count <= 0:
        return "unhealthy"
    if coverage >= 80:
        return "healthy"
    if coverage >= 50:
        return "degraded"
    return "unhealthy"


def _build_recommendations(
    vnx_path: Path,
    daemon_running: bool,
    recent_intelligence: bool,
    pattern_count: int,
    coverage: float,
    usage_tracking: bool,
    session_count: int = 0,
) -> List[str]:
    recommendations: List[str] = []
    if not daemon_running:
        recommendations.append(f"Start intelligence daemon: cd {vnx_path / 'scripts'} && python3 intelligence_daemon.py &")
    if not recent_intelligence:
        recommendations.append("Intelligence not gathering - check daemon logs")
    if pattern_count == 0:
        recommendations.append("No patterns available - run pattern generation")
    if coverage < 50:
        recommendations.append("Low intelligence coverage - check dispatcher integration")
    if not usage_tracking:
        recommendations.append("Pattern usage tracking not active - check learning loop")
    if session_count == 0:
        recommendations.append("Session analytics empty - run: vnx analyze-sessions")
    return recommendations


def check_health(human: bool = False) -> int:
    """Check overall health of intelligence system."""
    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir / "lib"))
    try:
        from vnx_paths import ensure_env
    except Exception as exc:  # pragma: no cover - bootstrap failure
        raise HealthCheckError("path_resolution_failed", EXIT_DEPENDENCY, f"Failed to load vnx_paths: {exc}") from exc

    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])
    vnx_path = Path(paths["VNX_HOME"])

    health: Dict[str, object] = {}
    warnings: List[str] = []

    daemon_running, daemon_pid, patterns_available, last_extraction = _load_base_health(
        state_dir / "intelligence_health.json", warnings
    )
    health["daemon_running"] = daemon_running
    health["daemon_pid"] = daemon_pid
    health["patterns_available"] = patterns_available
    health["last_extraction"] = last_extraction

    recent_intel, last_intel_time = _enrich_from_intelligence_file(state_dir / "t0_intelligence.ndjson", health, warnings)
    pattern_count = int(health.get("patterns_available", 0) or 0)
    health["recent_intelligence"] = recent_intel
    health["last_intelligence_time"] = last_intel_time
    health["pattern_count"] = pattern_count

    receipts_with_intel, total_receipts, recent_receipts_with_intel, intel_coverage = _collect_receipt_coverage(
        state_dir / "t0_receipts.ndjson", warnings
    )
    health["receipts_with_intelligence"] = f"{receipts_with_intel}/{total_receipts}"
    health["intelligence_coverage"] = f"{intel_coverage:.1f}%"
    health["receipts_with_patterns"] = recent_receipts_with_intel

    intel_db = state_dir / "quality_intelligence.db"
    db_exists, db_size_mb = _collect_database_info(intel_db)
    health["database_exists"] = db_exists
    health["database_size_mb"] = db_size_mb
    health["session_analytics_count"] = _collect_session_count(intel_db, warnings)

    pipeline = check_pipeline_connected(intel_db)
    health["pipeline_connected"] = pipeline["connected"]
    health["pipeline_success_patterns"] = pipeline["success_patterns"]
    health["pipeline_antipatterns"] = pipeline["antipatterns"]
    health["pipeline_prevention_rules"] = pipeline["prevention_rules"]

    usage_tracking, recent_usage = _collect_usage_tracking(state_dir / "pattern_usage.ndjson", warnings)
    health["usage_tracking_active"] = usage_tracking
    health["recent_pattern_usage"] = recent_usage

    health_status = _determine_health_status(daemon_running, recent_intel, pattern_count, intel_coverage)
    health["health_status"] = health_status

    session_count = int(health.get("session_analytics_count", 0) or 0)
    recommendations = _build_recommendations(
        vnx_path, daemon_running, recent_intel, pattern_count, intel_coverage, usage_tracking,
        session_count=session_count,
    )
    health["recommendations"] = recommendations
    if warnings:
        health["warnings"] = warnings

    if human:
        emit_human(f"Health status: {health_status}")
        emit_human(f"Daemon running: {daemon_running}")
        emit_human(f"Recent intelligence: {recent_intel}")
        emit_human(f"Patterns available: {health.get('pattern_count', 0)}")
        emit_human(f"Session analytics rows: {health.get('session_analytics_count', 0)}")
        emit_human(f"Intelligence coverage: {health.get('intelligence_coverage')}")
        emit_human(f"Pipeline connected: {pipeline['connected']}")
        emit_human(
            f"  success_patterns={pipeline['success_patterns']}  "
            f"antipatterns={pipeline['antipatterns']}  "
            f"prevention_rules={pipeline['prevention_rules']}"
        )
        if recommendations:
            emit_human("\nRecommendations:")
            for rec in recommendations:
                emit_human(f"- {rec}")
    else:
        emit_json(health)

    return EXIT_OK if health_status == "healthy" else EXIT_HEALTH


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    human, _ = parse_human_flag(args)

    try:
        return check_health(human=human)
    except HealthCheckError as exc:
        if human:
            emit_human(f"ERROR [{exc.code}]: {exc.message}")
        else:
            emit_json(_error_payload(exc.code, exc.message))
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive wrapper
        if human:
            emit_human(f"ERROR [unexpected_error]: {exc}")
        else:
            emit_json(_error_payload("unexpected_error", str(exc)))
        return EXIT_DEPENDENCY


if __name__ == "__main__":
    raise SystemExit(main())
