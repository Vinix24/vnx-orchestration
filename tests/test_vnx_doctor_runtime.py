#!/usr/bin/env python3
"""
Tests for VNX Doctor Runtime — PR-4 runtime health checks.

Covers:
  - Schema status: healthy, missing DB, missing tables, v2 migration incomplete
  - Lease health: all idle, expired leases, invalid states
  - Queue health: empty, healthy, stuck dispatches, dead-lettered
  - Incident pressure: no incidents, open, escalated, critical, halted
  - tmux profile: valid, missing, corrupt, structural issues
  - Lease/dispatch coherence: consistent, orphaned, conflicting
  - Recovery preflight: blockers from various failure combinations
  - Full report: healthy, degraded, blocked scenarios
  - Idempotency: repeated runs produce identical results
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add scripts/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_doctor_runtime import (
    FAIL,
    PASS,
    WARN,
    CheckResult,
    DoctorReport,
    check_incident_pressure,
    check_lease_dispatch_coherence,
    check_lease_health,
    check_queue_health,
    check_schema_status,
    check_tmux_profile,
    compute_recovery_preflight,
    run_runtime_checks,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Provide a temporary state directory with initialized schema."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    return sd


@pytest.fixture
def empty_state_dir(tmp_path):
    """Provide a temporary state directory with no database."""
    sd = tmp_path / "state"
    sd.mkdir()
    return sd


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _past_utc(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _future_utc(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _write_profile(state_dir: Path, data: dict) -> None:
    (state_dir / "session_profile.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _write_panes(state_dir: Path, data: dict) -> None:
    (state_dir / "panes.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _valid_profile_data(session_name: str = "vnx-test") -> dict:
    return {
        "schema_version": 1,
        "session_name": session_name,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
        "home_window": {
            "name": "main",
            "window_type": "home",
            "panes": [
                {"terminal_id": "T0", "role": "orchestrator", "pane_id": "%0", "work_dir": "/tmp/t0"},
                {"terminal_id": "T1", "role": "worker", "pane_id": "%1", "track": "A", "work_dir": "/tmp/t1"},
                {"terminal_id": "T2", "role": "worker", "pane_id": "%2", "track": "B", "work_dir": "/tmp/t2"},
                {"terminal_id": "T3", "role": "deep", "pane_id": "%3", "track": "C", "work_dir": "/tmp/t3"},
            ],
        },
        "dynamic_windows": [],
    }


# ---------------------------------------------------------------------------
# Schema status checks
# ---------------------------------------------------------------------------

class TestSchemaStatus:
    def test_healthy_schema(self, state_dir):
        result = check_schema_status(state_dir)
        assert result.status == PASS
        assert "all tables present" in result.message

    def test_missing_database(self, empty_state_dir):
        result = check_schema_status(empty_state_dir)
        assert result.status == FAIL
        assert "not found" in result.message

    def test_missing_v2_tables(self, tmp_path):
        """Schema with only v1 tables should warn about missing v2."""
        sd = tmp_path / "state"
        sd.mkdir()
        # Only apply v1 schema (no incident tables)
        schema_dir = Path(__file__).resolve().parent.parent / "schemas"
        v1_sql = (schema_dir / "runtime_coordination.sql").read_text()
        with get_connection(sd) as conn:
            conn.executescript(v1_sql)
            conn.commit()

        result = check_schema_status(sd)
        assert result.status == WARN
        assert "v2 migration" in result.message

    def test_schema_reports_version(self, state_dir):
        result = check_schema_status(state_dir)
        assert any("Schema version" in d for d in result.details)


# ---------------------------------------------------------------------------
# Lease health checks
# ---------------------------------------------------------------------------

class TestLeaseHealth:
    def test_all_idle(self, state_dir):
        result = check_lease_health(state_dir)
        assert result.status == PASS
        assert "healthy" in result.message

    def test_expired_lease(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('test-dispatch', 'running', 'T1', ?)",
                (_now_utc(),),
            )
            conn.execute(
                "UPDATE terminal_leases SET state = 'leased', "
                "dispatch_id = 'test-dispatch', "
                "expires_at = ? WHERE terminal_id = 'T1'",
                (_past_utc(300),),
            )
            conn.commit()

        result = check_lease_health(state_dir)
        assert result.status == WARN
        assert "T1" in result.message

    def test_expired_state_lease(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET state = 'expired' WHERE terminal_id = 'T2'"
            )
            conn.commit()

        result = check_lease_health(state_dir)
        assert result.status == WARN
        assert "T2" in result.message

    def test_leased_with_valid_ttl(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('test-dispatch', 'running', 'T1', ?)",
                (_now_utc(),),
            )
            conn.execute(
                "UPDATE terminal_leases SET state = 'leased', "
                "dispatch_id = 'test-dispatch', "
                "expires_at = ? WHERE terminal_id = 'T1'",
                (_future_utc(600),),
            )
            conn.commit()

        result = check_lease_health(state_dir)
        assert result.status == PASS

    def test_no_lease_rows(self, tmp_path):
        """Schema with empty terminal_leases should fail."""
        sd = tmp_path / "state"
        sd.mkdir()
        init_schema(sd)
        with get_connection(sd) as conn:
            conn.execute("DELETE FROM terminal_leases")
            conn.commit()

        result = check_lease_health(sd)
        assert result.status == FAIL
        assert "No terminal lease rows" in result.message


# ---------------------------------------------------------------------------
# Queue health checks
# ---------------------------------------------------------------------------

class TestQueueHealth:
    def test_empty_queue(self, state_dir):
        result = check_queue_health(state_dir)
        assert result.status == PASS
        assert "No dispatches" in result.message

    def test_healthy_dispatches(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-001', 'completed', 'T1', ?)",
                (_now_utc(),),
            )
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-002', 'running', 'T2', ?)",
                (_now_utc(),),
            )
            conn.commit()

        result = check_queue_health(state_dir)
        assert result.status == PASS
        assert "healthy" in result.message

    def test_stuck_dispatch(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-stuck', 'claimed', 'T1', ?)",
                (_past_utc(900),),
            )
            conn.commit()

        result = check_queue_health(state_dir)
        assert result.status == WARN
        assert "stuck" in result.message

    def test_dead_letter_dispatch(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-dead', 'dead_letter', 'T1', ?)",
                (_now_utc(),),
            )
            conn.commit()

        result = check_queue_health(state_dir)
        assert result.status == WARN
        assert "dead-lettered" in result.message

    def test_failed_delivery(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-fail', 'failed_delivery', 'T1', ?)",
                (_now_utc(),),
            )
            conn.commit()

        result = check_queue_health(state_dir)
        assert result.status == WARN
        assert "failed_delivery" in result.message


# ---------------------------------------------------------------------------
# Incident pressure checks
# ---------------------------------------------------------------------------

class TestIncidentPressure:
    def test_no_incident_table(self, tmp_path):
        """v1-only schema has no incident table — should pass gracefully."""
        sd = tmp_path / "state"
        sd.mkdir()
        schema_dir = Path(__file__).resolve().parent.parent / "schemas"
        v1_sql = (schema_dir / "runtime_coordination.sql").read_text()
        with get_connection(sd) as conn:
            conn.executescript(v1_sql)
            conn.commit()

        result = check_incident_pressure(sd)
        assert result.status == PASS
        assert "not present" in result.message

    def test_no_incidents(self, state_dir):
        result = check_incident_pressure(state_dir)
        assert result.status == PASS
        assert "No active incidents" in result.message

    def test_open_incidents_within_budget(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state) "
                "VALUES ('i-001', 'process_crash', 'warning', 'terminal', 'T1', 'open')"
            )
            conn.commit()

        result = check_incident_pressure(state_dir)
        assert result.status == PASS
        assert "within budget" in result.message

    def test_escalated_incidents(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state, escalated) "
                "VALUES ('i-esc', 'delivery_failure', 'error', 'dispatch', 'd-001', 'escalated', 1)"
            )
            conn.commit()

        result = check_incident_pressure(state_dir)
        assert result.status == WARN
        assert "escalated" in result.message

    def test_critical_incidents(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state) "
                "VALUES ('i-crit', 'repeated_failure_loop', 'critical', 'terminal', 'T2', 'open')"
            )
            conn.commit()

        result = check_incident_pressure(state_dir)
        assert result.status == FAIL
        assert "critical" in result.message

    def test_halted_recovery(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO retry_budgets "
                "(budget_key, entity_type, entity_id, incident_class, "
                "attempts_used, max_retries, auto_recovery_halted, created_at, updated_at) "
                "VALUES ('t:T1:pc', 'terminal', 'T1', 'process_crash', "
                "3, 3, 1, ?, ?)",
                (_now_utc(), _now_utc()),
            )
            conn.commit()

        result = check_incident_pressure(state_dir)
        assert result.status == FAIL
        assert "halted" in result.message

    def test_exhausted_budget(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO retry_budgets "
                "(budget_key, entity_type, entity_id, incident_class, "
                "attempts_used, max_retries, auto_recovery_halted, created_at, updated_at) "
                "VALUES ('t:T1:df', 'terminal', 'T1', 'delivery_failure', "
                "5, 5, 0, ?, ?)",
                (_now_utc(), _now_utc()),
            )
            conn.commit()

        result = check_incident_pressure(state_dir)
        assert result.status == WARN
        assert "exhausted" in result.message


# ---------------------------------------------------------------------------
# tmux profile checks
# ---------------------------------------------------------------------------

class TestTmuxProfile:
    def test_missing_profile(self, state_dir):
        result = check_tmux_profile(state_dir)
        assert result.status == WARN
        assert "not found" in result.message

    def test_valid_profile(self, state_dir):
        _write_profile(state_dir, _valid_profile_data())
        result = check_tmux_profile(state_dir)
        assert result.status == PASS
        assert "valid" in result.message

    def test_corrupt_profile(self, state_dir):
        (state_dir / "session_profile.json").write_text("not json{{{", encoding="utf-8")
        result = check_tmux_profile(state_dir)
        assert result.status == FAIL
        assert "corrupt" in result.message

    def test_missing_home_terminals(self, state_dir):
        data = _valid_profile_data()
        # Remove T2 and T3
        data["home_window"]["panes"] = data["home_window"]["panes"][:2]
        _write_profile(state_dir, data)
        result = check_tmux_profile(state_dir)
        assert result.status == FAIL
        assert "Missing home" in result.message

    def test_missing_work_dir(self, state_dir):
        data = _valid_profile_data()
        data["home_window"]["panes"][0]["work_dir"] = ""
        _write_profile(state_dir, data)
        result = check_tmux_profile(state_dir)
        assert result.status == WARN
        assert "work_dir" in result.message or "identity anchor" in result.message

    def test_profile_panes_drift(self, state_dir):
        _write_profile(state_dir, _valid_profile_data())
        # Write panes.json missing T3
        _write_panes(state_dir, {
            "panes": {"T0": "%0", "T1": "%1", "T2": "%2"},
        })
        result = check_tmux_profile(state_dir)
        assert result.status == WARN
        assert "drift" in result.message.lower() or "T3" in str(result.details)

    def test_profile_with_matching_panes(self, state_dir):
        _write_profile(state_dir, _valid_profile_data())
        _write_panes(state_dir, {
            "panes": {"T0": "%0", "T1": "%1", "T2": "%2", "T3": "%3"},
        })
        result = check_tmux_profile(state_dir)
        assert result.status == PASS

    def test_missing_session_name(self, state_dir):
        data = _valid_profile_data()
        data["session_name"] = ""
        _write_profile(state_dir, data)
        result = check_tmux_profile(state_dir)
        assert result.status == WARN


# ---------------------------------------------------------------------------
# Lease/dispatch coherence checks
# ---------------------------------------------------------------------------

class TestLeaseDispatchCoherence:
    def test_consistent(self, state_dir):
        result = check_lease_dispatch_coherence(state_dir)
        assert result.status == PASS

    def test_orphaned_lease(self, state_dir):
        import sqlite3
        # Use a raw connection with FK off to simulate orphan state
        db_path = state_dir / "runtime_coordination.db"
        raw_conn = sqlite3.connect(str(db_path))
        raw_conn.execute("PRAGMA foreign_keys = OFF")
        raw_conn.execute(
            "UPDATE terminal_leases SET state = 'leased', "
            "dispatch_id = 'nonexistent-dispatch' WHERE terminal_id = 'T1'"
        )
        raw_conn.commit()
        raw_conn.close()

        result = check_lease_dispatch_coherence(state_dir)
        assert result.status == WARN
        assert "coherence" in result.message

    def test_lease_to_completed_dispatch(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-done', 'completed', 'T1', ?)",
                (_now_utc(),),
            )
            conn.execute(
                "UPDATE terminal_leases SET state = 'leased', "
                "dispatch_id = 'd-done' WHERE terminal_id = 'T1'"
            )
            conn.commit()

        result = check_lease_dispatch_coherence(state_dir)
        assert result.status == WARN
        assert "coherence" in result.message

    def test_lease_to_active_dispatch(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-run', 'running', 'T2', ?)",
                (_now_utc(),),
            )
            conn.execute(
                "UPDATE terminal_leases SET state = 'leased', "
                "dispatch_id = 'd-run' WHERE terminal_id = 'T2'"
            )
            conn.commit()

        result = check_lease_dispatch_coherence(state_dir)
        assert result.status == PASS


# ---------------------------------------------------------------------------
# Recovery preflight
# ---------------------------------------------------------------------------

class TestRecoveryPreflight:
    def test_no_blockers_on_healthy(self, state_dir):
        report = run_runtime_checks(state_dir)
        blockers = [b for b in report.recovery_preflight if b.startswith("BLOCKER")]
        assert len(blockers) == 0

    def test_schema_fail_blocks_recovery(self, empty_state_dir):
        report = run_runtime_checks(empty_state_dir)
        blockers = [b for b in report.recovery_preflight if b.startswith("BLOCKER")]
        assert any("database" in b.lower() for b in blockers)

    def test_critical_incidents_block_recovery(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state) "
                "VALUES ('i-crit', 'repeated_failure_loop', 'critical', 'terminal', 'T2', 'open')"
            )
            conn.commit()

        report = run_runtime_checks(state_dir)
        blockers = [b for b in report.recovery_preflight if b.startswith("BLOCKER")]
        assert len(blockers) > 0

    def test_stuck_dispatches_inform(self, state_dir):
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-stuck', 'claimed', 'T1', ?)",
                (_past_utc(900),),
            )
            conn.commit()

        report = run_runtime_checks(state_dir)
        info_items = [b for b in report.recovery_preflight if b.startswith("INFO")]
        assert len(info_items) > 0


# ---------------------------------------------------------------------------
# Full report scenarios
# ---------------------------------------------------------------------------

class TestFullReport:
    def test_healthy_runtime(self, state_dir):
        report = run_runtime_checks(state_dir)
        assert report.overall_status == PASS or report.overall_status == WARN
        # tmux profile is WARN when missing which is expected in tests
        assert report.fail_count == 0
        assert report.generated_at

    def test_degraded_runtime(self, state_dir):
        """Multiple warning conditions."""
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE terminal_leases SET state = 'expired' WHERE terminal_id = 'T1'"
            )
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, terminal_id, updated_at) "
                "VALUES ('d-stuck', 'claimed', 'T2', ?)",
                (_past_utc(900),),
            )
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state, escalated) "
                "VALUES ('i-esc', 'delivery_failure', 'error', 'dispatch', 'd-001', 'escalated', 1)"
            )
            conn.commit()

        report = run_runtime_checks(state_dir)
        assert report.warn_count >= 3
        assert report.overall_status == WARN

    def test_blocked_runtime(self, state_dir):
        """Critical incident + halted recovery = FAIL."""
        with get_connection(state_dir) as conn:
            conn.execute(
                "INSERT INTO incident_log "
                "(incident_id, incident_class, severity, entity_type, entity_id, state) "
                "VALUES ('i-crit', 'repeated_failure_loop', 'critical', 'terminal', 'T2', 'open')"
            )
            conn.execute(
                "INSERT INTO retry_budgets "
                "(budget_key, entity_type, entity_id, incident_class, "
                "attempts_used, max_retries, auto_recovery_halted, created_at, updated_at) "
                "VALUES ('t:T2:rfl', 'terminal', 'T2', 'repeated_failure_loop', "
                "0, 0, 1, ?, ?)",
                (_now_utc(), _now_utc()),
            )
            conn.commit()

        report = run_runtime_checks(state_dir)
        assert report.overall_status == FAIL
        assert report.fail_count >= 1
        blockers = [b for b in report.recovery_preflight if b.startswith("BLOCKER")]
        assert len(blockers) > 0

    def test_missing_database(self, empty_state_dir):
        report = run_runtime_checks(empty_state_dir)
        assert report.overall_status == FAIL
        # Schema fail should skip deeper checks
        schema_check = report.checks[0]
        assert schema_check.name == "schema_status"
        assert schema_check.status == FAIL

    def test_idempotent(self, state_dir):
        """Running checks twice produces identical results."""
        report1 = run_runtime_checks(state_dir)
        report2 = run_runtime_checks(state_dir)
        # Compare check statuses and messages
        assert len(report1.checks) == len(report2.checks)
        for c1, c2 in zip(report1.checks, report2.checks):
            assert c1.name == c2.name
            assert c1.status == c2.status
            assert c1.message == c2.message

    def test_report_to_dict(self, state_dir):
        report = run_runtime_checks(state_dir)
        d = report.to_dict()
        assert "overall_status" in d
        assert "checks" in d
        assert "recovery_preflight" in d
        assert isinstance(d["checks"], list)
        assert all("name" in c and "status" in c for c in d["checks"])
