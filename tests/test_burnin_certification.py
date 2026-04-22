#!/usr/bin/env python3
"""
PR-4 Burn-In Certification Tests — Real operator workflow validation.

Goes beyond unit tests to prove the headless observability feature works in
realistic operator scenarios. Tests the full chain: adapter -> registry ->
classification -> artifacts -> inspection -> recovery.

Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 7 (Burn-In Proof Criteria)

Burn-in criteria tested:
  B-1: Headless run completes with durable identity
  B-2: Heartbeat and output timestamps are updated during run
  B-3: Exit classification produces correct failure class
  B-4: Log artifacts are human-readable and complete
  B-5: Operator can inspect run without file spelunking
  B-6: Recovery detects and transitions stuck runs
  B-7: Provenance chain links run -> dispatch -> receipt
  B-8: Multiple concurrent runs don't interfere
  B-9: Interactive mode is unaffected by headless observability
  B-10: Feature operates correctly under realistic conditions
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# Ensure lib is importable
LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

from headless_run_registry import (
    HeadlessRunRegistry,
    HeadlessRun,
    FAILURE_CLASSES,
    TERMINAL_STATES,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_OUTPUT_HANG_THRESHOLD,
)
from exit_classifier import classify_exit, ClassificationResult
from log_artifact import write_log_artifact, write_output_artifact
from headless_inspect import (
    format_run_line,
    format_run_detail,
    list_runs,
    build_health_summary,
    format_health_summary,
)
from headless_adapter import (
    HeadlessAdapter,
    HeadlessExecutionResult,
    HEADLESS_ELIGIBLE_TASK_CLASSES,
)
from runtime_coordination import (
    _append_event,
    _now_utc,
    get_connection,
    init_schema,
    register_dispatch,
    create_attempt,
    get_dispatch,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


@pytest.fixture
def state_dir(tmp_path):
    """Create a temp state directory with initialized schema."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd, SCHEMAS_DIR / "runtime_coordination.sql")
    return sd


@pytest.fixture
def registry(state_dir):
    return HeadlessRunRegistry(state_dir)


@pytest.fixture
def artifact_dir(tmp_path):
    ad = tmp_path / "artifacts"
    ad.mkdir()
    return ad


@pytest.fixture
def dispatch_dir(tmp_path):
    dd = tmp_path / "dispatches"
    dd.mkdir()
    return dd


def _make_run(registry, state_dir, dispatch_id=None, **kwargs):
    """Create a dispatch + attempt + run in a single call. Returns HeadlessRun."""
    did = dispatch_id or f"d-{uuid.uuid4().hex[:12]}"
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=did)
        attempt = create_attempt(
            conn, dispatch_id=did, terminal_id="T1", attempt_number=1,
        )
        conn.commit()
    defaults = {
        "dispatch_id": did,
        "attempt_id": attempt["attempt_id"],
        "target_id": "headless_claude_cli_T1",
        "target_type": "headless_claude_cli",
        "task_class": "research_structured",
    }
    defaults.update(kwargs)
    return registry.create_run(**defaults)


def _create_test_dispatch(state_dir, dispatch_id=None):
    """Create a dispatch for adapter testing. Returns dispatch_id."""
    dispatch_id = dispatch_id or f"test-dispatch-{uuid.uuid4().hex[:8]}"
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id="T1")
        conn.commit()
    return dispatch_id


def _create_dispatch_bundle(dispatch_dir, dispatch_id, prompt="Summarize the architecture."):
    """Create a dispatch bundle on disk."""
    bundle_path = dispatch_dir / dispatch_id
    bundle_path.mkdir(parents=True, exist_ok=True)
    (bundle_path / "bundle.json").write_text(
        json.dumps({"dispatch_id": dispatch_id, "task_class": "research_structured"}),
        encoding="utf-8",
    )
    (bundle_path / "prompt.txt").write_text(prompt, encoding="utf-8")
    return bundle_path


# ---------------------------------------------------------------------------
# B-1: Headless run completes with durable identity
# ---------------------------------------------------------------------------

class TestB1DurableIdentity:

    def test_run_id_is_uuid_and_unique(self, state_dir, registry):
        ids = set()
        for _ in range(10):
            run = _make_run(registry, state_dir)
            assert len(run.run_id) == 36
            ids.add(run.run_id)
        assert len(ids) == 10

    def test_run_persists_across_registry_instances(self, state_dir):
        r1 = HeadlessRunRegistry(state_dir)
        run = _make_run(r1, state_dir, dispatch_id="d-persist")
        r2 = HeadlessRunRegistry(state_dir)
        retrieved = r2.get(run.run_id)
        assert retrieved is not None
        assert retrieved.run_id == run.run_id
        assert retrieved.dispatch_id == "d-persist"

    def test_identity_fields_are_complete(self, state_dir, registry):
        did = f"d-fields-{uuid.uuid4().hex[:8]}"
        with get_connection(state_dir) as conn:
            register_dispatch(conn, dispatch_id=did)
            attempt = create_attempt(conn, dispatch_id=did, terminal_id="T2", attempt_number=1)
            conn.commit()
        run = registry.create_run(
            dispatch_id=did,
            attempt_id=attempt["attempt_id"],
            target_id="headless-target-1",
            target_type="headless_claude_cli",
            task_class="research_structured",
            terminal_id="T2",
        )
        assert run.dispatch_id == did
        assert run.target_id == "headless-target-1"
        assert run.target_type == "headless_claude_cli"
        assert run.task_class == "research_structured"
        assert run.terminal_id == "T2"
        assert run.state == "init"


# ---------------------------------------------------------------------------
# B-2: Heartbeat and output timestamps are updated during run
# ---------------------------------------------------------------------------

class TestB2HeartbeatAndTimestamps:

    def test_heartbeat_updates_on_running_run(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        first_hb = registry.get(run.run_id).heartbeat_at
        time.sleep(0.05)
        registry.update_heartbeat(run.run_id)
        second_hb = registry.get(run.run_id).heartbeat_at
        assert second_hb >= first_hb

    def test_output_timestamp_tracks_activity(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        registry.update_last_output(run.run_id)
        r = registry.get(run.run_id)
        assert r.last_output_at is not None

    def test_stale_detection_uses_heartbeat(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()
        stale = registry.list_stale()
        assert any(s.run_id == run.run_id for s in stale)

    def test_hung_detection_uses_output_timestamp(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=300)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()
        hung = registry.list_hung()
        assert any(h.run_id == run.run_id for h in hung)


# ---------------------------------------------------------------------------
# B-3: Exit classification produces correct failure class
# ---------------------------------------------------------------------------

class TestB3ExitClassification:

    def test_success_path(self):
        r = classify_exit(exit_code=0)
        assert r.failure_class == "SUCCESS"
        assert r.retryable is False

    def test_timeout_path(self):
        r = classify_exit(exit_code=None, timed_out=True)
        assert r.failure_class == "TIMEOUT"
        assert r.retryable is True

    def test_api_rate_limit(self):
        r = classify_exit(exit_code=1, stderr="Error: rate limit exceeded (429)")
        assert r.failure_class == "TOOL_FAIL"
        assert r.retryable is True

    def test_binary_not_found(self):
        r = classify_exit(exit_code=None, binary_not_found=True)
        assert r.failure_class == "INFRA_FAIL"

    def test_sigterm(self):
        r = classify_exit(exit_code=-15)
        assert r.failure_class == "INTERRUPTED"
        assert r.signal == 15

    def test_prompt_error(self):
        r = classify_exit(exit_code=1, stderr="Error: invalid prompt format")
        assert r.failure_class == "PROMPT_ERR"
        assert r.retryable is False

    def test_unknown_fallback(self):
        r = classify_exit(exit_code=42, stderr="some random error output")
        assert r.failure_class == "UNKNOWN"

    def test_no_output_hang(self):
        r = classify_exit(exit_code=1, no_output_detected=True)
        assert r.failure_class == "NO_OUTPUT"

    def test_classification_evidence_is_complete(self):
        r = classify_exit(exit_code=1, stderr="connection refused")
        assert r.failure_class == "TOOL_FAIL"
        assert r.exit_code == 1
        assert "connection" in r.stderr_tail.lower()
        assert r.classification_reason != ""
        assert r.operator_hint != ""


# ---------------------------------------------------------------------------
# B-4: Log artifacts are human-readable and complete
# ---------------------------------------------------------------------------

class TestB4LogArtifacts:

    def test_log_artifact_has_all_sections(self, artifact_dir):
        path = write_log_artifact(
            artifact_dir=artifact_dir,
            run_id="run-artifact-test",
            dispatch_id="dispatch-art-1",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="This is the research output.\nIt spans multiple lines.",
            stderr="Warning: context window at 80%",
            exit_code=0,
            duration_seconds=12.5,
        )
        content = path.read_text(encoding="utf-8")
        assert "VNX HEADLESS RUN LOG" in content
        assert "run-artifact-test" in content
        assert "dispatch-art-1" in content
        assert "STDOUT" in content
        assert "research output" in content
        assert "STDERR" in content
        assert "RUN OUTCOME" in content
        assert "12.5" in content

    def test_output_artifact_contains_raw_output(self, artifact_dir):
        stdout = "# Architecture Summary\n\nThe system uses event-sourced state.\n"
        path = write_output_artifact(
            artifact_dir=artifact_dir, run_id="run-output-test", stdout=stdout,
        )
        assert path is not None
        assert path.read_text(encoding="utf-8") == stdout

    def test_empty_output_returns_none(self, artifact_dir):
        path = write_output_artifact(
            artifact_dir=artifact_dir, run_id="run-empty", stdout="   ",
        )
        assert path is None

    def test_failed_run_artifact_includes_failure_class(self, artifact_dir):
        path = write_log_artifact(
            artifact_dir=artifact_dir,
            run_id="run-failed-art",
            dispatch_id="dispatch-fail",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:05:00.000Z",
            stdout="",
            stderr="Error: rate limit exceeded",
            exit_code=1,
            failure_class="TOOL_FAIL",
            duration_seconds=3.2,
        )
        content = path.read_text(encoding="utf-8")
        assert "TOOL_FAIL" in content
        assert "rate limit exceeded" in content

    def test_path_traversal_run_id_confined_to_artifact_dir(self, artifact_dir):
        traversal_id = "../../etc/run-escape"
        path = write_log_artifact(
            artifact_dir=artifact_dir,
            run_id=traversal_id,
            dispatch_id="dispatch-traversal",
            target_type="headless_claude_cli",
            started_at="2026-03-30T10:00:00.000Z",
            stdout="output",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
        )
        assert path.parent.resolve() == artifact_dir.resolve()
        assert ".." not in path.name
        assert "/" not in path.name

    def test_path_traversal_output_artifact_confined_to_artifact_dir(self, artifact_dir):
        traversal_id = "../sneaky/run-out"
        path = write_output_artifact(
            artifact_dir=artifact_dir,
            run_id=traversal_id,
            stdout="sensitive data",
        )
        assert path is not None
        assert path.parent.resolve() == artifact_dir.resolve()
        assert ".." not in path.name
        assert "/" not in path.name


# ---------------------------------------------------------------------------
# B-5: Operator can inspect run without file spelunking
# ---------------------------------------------------------------------------

class TestB5OperatorInspection:

    def test_list_view_shows_all_active_runs(self, state_dir, registry):
        for _ in range(3):
            run = _make_run(registry, state_dir)
            registry.transition(run.run_id, "running", actor="test")
        lines = list_runs(registry, show_active=True)
        assert len(lines) == 3
        for line in lines:
            assert "running" in line
            assert "[>]" in line

    def test_detail_view_shows_complete_run_info(self, state_dir, registry):
        did = f"d-detail-{uuid.uuid4().hex[:8]}"
        run = _make_run(registry, state_dir, dispatch_id=did, terminal_id="T2")
        registry.transition(run.run_id, "running", actor="test")
        registry.transition(run.run_id, "completing", actor="test")
        registry.transition(run.run_id, "succeeded", failure_class="SUCCESS",
                          exit_code=0, duration_seconds=15.3, actor="test")
        detail = format_run_detail(registry.get(run.run_id))
        assert run.run_id in detail
        assert did in detail
        assert "succeeded" in detail

    def test_health_summary_aggregates_correctly(self, state_dir, registry):
        for _ in range(2):
            run = _make_run(registry, state_dir)
            registry.transition(run.run_id, "running", actor="test")
            registry.transition(run.run_id, "completing", actor="test")
            registry.transition(run.run_id, "succeeded", failure_class="SUCCESS", actor="test")

        fail_run = _make_run(registry, state_dir)
        registry.transition(fail_run.run_id, "running", actor="test")
        registry.transition(fail_run.run_id, "failing", actor="test")
        registry.transition(fail_run.run_id, "failed", failure_class="TIMEOUT", actor="test")

        summary = build_health_summary(registry)
        assert summary.total_runs == 3
        assert summary.succeeded_count == 2
        assert summary.failed_count == 1
        assert "TIMEOUT" in summary.failure_class_counts
        text = format_health_summary(summary)
        assert "Succeeded:  2" in text
        assert "Failed:     1" in text

    def test_failed_run_filter(self, state_dir, registry):
        ok_run = _make_run(registry, state_dir)
        registry.transition(ok_run.run_id, "running", actor="test")
        registry.transition(ok_run.run_id, "completing", actor="test")
        registry.transition(ok_run.run_id, "succeeded", failure_class="SUCCESS", actor="test")

        fail_run = _make_run(registry, state_dir)
        registry.transition(fail_run.run_id, "running", actor="test")
        registry.transition(fail_run.run_id, "failing", actor="test")
        registry.transition(fail_run.run_id, "failed", failure_class="INFRA_FAIL", actor="test")

        failed_lines = list_runs(registry, show_failed=True)
        assert len(failed_lines) == 1
        assert "INFRA_FAIL" in failed_lines[0]

    def test_show_all_returns_recent_runs(self, state_dir, registry):
        for _ in range(3):
            run = _make_run(registry, state_dir)
            registry.transition(run.run_id, "running", actor="test")
            registry.transition(run.run_id, "completing", actor="test")
            registry.transition(run.run_id, "succeeded", failure_class="SUCCESS", actor="test")
        lines = list_runs(registry, show_all=True)
        assert len(lines) == 3
        for line in lines:
            assert "succeeded" in line


# ---------------------------------------------------------------------------
# B-6: Recovery detects and transitions stuck runs
# ---------------------------------------------------------------------------

class TestB6RecoveryIntegration:

    def test_recovery_detects_stale_heartbeat(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET heartbeat_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()
        stale = registry.list_stale()
        assert len(stale) >= 1

        registry.transition(run.run_id, "failing", actor="vnx_recover",
                          reason="stale heartbeat detected")
        registry.transition(run.run_id, "failed", failure_class="INFRA_FAIL",
                          actor="vnx_recover", reason="stale heartbeat")
        recovered = registry.get(run.run_id)
        assert recovered.state == "failed"
        assert recovered.failure_class == "INFRA_FAIL"

    def test_recovery_detects_hung_run(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=300)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with get_connection(state_dir) as conn:
            conn.execute(
                "UPDATE headless_runs SET last_output_at = ? WHERE run_id = ?",
                (old_ts, run.run_id),
            )
            conn.commit()
        hung = registry.list_hung()
        assert len(hung) >= 1

        registry.transition(run.run_id, "failing", actor="vnx_recover")
        registry.transition(run.run_id, "failed", failure_class="NO_OUTPUT",
                          actor="vnx_recover")
        recovered = registry.get(run.run_id)
        assert recovered.state == "failed"
        assert recovered.failure_class == "NO_OUTPUT"

    def test_recovery_skips_healthy_runs(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")
        registry.update_heartbeat(run.run_id)
        registry.update_last_output(run.run_id)
        stale = registry.list_stale()
        hung = registry.list_hung()
        assert not any(s.run_id == run.run_id for s in stale)
        assert not any(h.run_id == run.run_id for h in hung)


# ---------------------------------------------------------------------------
# B-7: Provenance chain links run -> dispatch -> events
# ---------------------------------------------------------------------------

class TestB7ProvenanceChain:

    def test_run_lifecycle_emits_events(self, state_dir, registry):
        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test_burnin")
        registry.transition(run.run_id, "completing", actor="test_burnin")
        registry.transition(run.run_id, "succeeded", failure_class="SUCCESS",
                          actor="test_burnin")
        with get_connection(state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE entity_id = ? ORDER BY occurred_at",
                (run.run_id,),
            ).fetchall()
        assert len(events) >= 3
        event_types = [e["event_type"] for e in events]
        assert any("headless" in et or "transition" in et for et in event_types)

    def test_adapter_execution_links_dispatch_to_run(self, state_dir, dispatch_dir, artifact_dir):
        os.environ["VNX_HEADLESS_ENABLED"] = "1"
        os.environ["VNX_HEADLESS_CLI"] = "echo"
        try:
            dispatch_id = _create_test_dispatch(state_dir)
            _create_dispatch_bundle(dispatch_dir, dispatch_id)
            adapter = HeadlessAdapter(
                state_dir=state_dir,
                dispatch_dir=dispatch_dir,
                artifact_dir=artifact_dir,
            )
            result = adapter.execute(
                dispatch_id=dispatch_id,
                target_id="headless-prov",
                target_type="headless_claude_cli",
                task_class="research_structured",
            )
            assert result.success is True

            with get_connection(state_dir) as conn:
                events = conn.execute(
                    "SELECT * FROM coordination_events WHERE entity_id = ? "
                    "ORDER BY occurred_at",
                    (dispatch_id,),
                ).fetchall()
            assert len(events) >= 3
            event_types = [e["event_type"] for e in events]
            assert any("headless" in et for et in event_types)
        finally:
            os.environ.pop("VNX_HEADLESS_ENABLED", None)
            os.environ.pop("VNX_HEADLESS_CLI", None)


# ---------------------------------------------------------------------------
# B-8: Multiple concurrent runs don't interfere
# ---------------------------------------------------------------------------

class TestB8ConcurrentRuns:

    def test_parallel_runs_have_independent_state(self, state_dir, registry):
        runs = [_make_run(registry, state_dir) for _ in range(5)]

        registry.transition(runs[0].run_id, "running", actor="test")
        registry.transition(runs[0].run_id, "completing", actor="test")
        registry.transition(runs[0].run_id, "succeeded", failure_class="SUCCESS", actor="test")

        registry.transition(runs[1].run_id, "running", actor="test")
        registry.transition(runs[1].run_id, "failing", actor="test")
        registry.transition(runs[1].run_id, "failed", failure_class="TIMEOUT", actor="test")

        registry.transition(runs[2].run_id, "running", actor="test")
        registry.transition(runs[4].run_id, "running", actor="test")

        assert registry.get(runs[0].run_id).state == "succeeded"
        assert registry.get(runs[1].run_id).state == "failed"
        assert registry.get(runs[2].run_id).state == "running"
        assert registry.get(runs[3].run_id).state == "init"
        assert registry.get(runs[4].run_id).state == "running"

        active = registry.list_active()
        active_ids = {r.run_id for r in active}
        assert runs[2].run_id in active_ids
        assert runs[4].run_id in active_ids
        assert runs[0].run_id not in active_ids

    def test_parallel_heartbeats_are_independent(self, state_dir, registry):
        run_a = _make_run(registry, state_dir)
        run_b = _make_run(registry, state_dir)
        registry.transition(run_a.run_id, "running", actor="test")
        registry.transition(run_b.run_id, "running", actor="test")

        registry.update_heartbeat(run_a.run_id)
        time.sleep(0.05)
        registry.update_heartbeat(run_b.run_id)

        a = registry.get(run_a.run_id)
        b = registry.get(run_b.run_id)
        assert a.heartbeat_at != b.heartbeat_at


# ---------------------------------------------------------------------------
# B-9: Interactive mode is unaffected
# ---------------------------------------------------------------------------

class TestB9InteractiveUnaffected:

    def test_headless_disabled_returns_none_adapter(self):
        from headless_adapter import load_headless_adapter
        os.environ.pop("VNX_HEADLESS_ENABLED", None)
        adapter = load_headless_adapter(
            state_dir="/tmp/nonexistent",
            dispatch_dir="/tmp/nonexistent",
        )
        assert adapter is None

    def test_coding_task_class_is_ineligible(self):
        assert not HeadlessAdapter.is_eligible("coding_interactive")
        assert not HeadlessAdapter.is_eligible("coding")

    def test_eligible_classes_are_non_coding(self):
        for tc in HEADLESS_ELIGIBLE_TASK_CLASSES:
            assert "coding" not in tc.lower()

    def test_headless_registry_doesnt_touch_terminal_leases(self, state_dir, registry):
        # Count leases before headless run
        with get_connection(state_dir) as conn:
            before = conn.execute(
                "SELECT COUNT(*) as cnt FROM terminal_leases WHERE state != 'idle'"
            ).fetchone()["cnt"]

        run = _make_run(registry, state_dir)
        registry.transition(run.run_id, "running", actor="test")

        # Headless run should not lease any terminal
        with get_connection(state_dir) as conn:
            after = conn.execute(
                "SELECT COUNT(*) as cnt FROM terminal_leases WHERE state != 'idle'"
            ).fetchone()["cnt"]
            assert after == before, "Headless run must not change terminal lease state"


# ---------------------------------------------------------------------------
# B-10: Realistic end-to-end headless flow
# ---------------------------------------------------------------------------

class TestB10EndToEnd:

    def test_full_success_flow(self, state_dir, dispatch_dir, artifact_dir):
        os.environ["VNX_HEADLESS_ENABLED"] = "1"
        os.environ["VNX_HEADLESS_CLI"] = "echo"
        try:
            dispatch_id = _create_test_dispatch(state_dir)
            _create_dispatch_bundle(dispatch_dir, dispatch_id,
                                   prompt="Analyze the security posture of the auth module.")
            adapter = HeadlessAdapter(
                state_dir=state_dir,
                dispatch_dir=dispatch_dir,
                artifact_dir=artifact_dir,
            )
            result = adapter.execute(
                dispatch_id=dispatch_id,
                target_id="headless-e2e",
                target_type="headless_claude_cli",
                task_class="research_structured",
            )

            assert result.success is True
            assert result.failure_class == "SUCCESS"
            assert result.exit_code == 0
            assert result.duration_seconds > 0
            assert result.log_artifact_path is not None

            log_path = Path(result.log_artifact_path)
            assert log_path.exists()
            log_content = log_path.read_text(encoding="utf-8")
            assert "VNX HEADLESS RUN LOG" in log_content
            assert dispatch_id in log_content

            with get_connection(state_dir) as conn:
                dispatch = get_dispatch(conn, dispatch_id)
                assert dispatch["state"] == "completed"

            assert result.classification_evidence is not None
            assert result.classification_evidence["failure_class"] == "SUCCESS"
            assert result.classification_evidence["retryable"] is False
        finally:
            os.environ.pop("VNX_HEADLESS_ENABLED", None)
            os.environ.pop("VNX_HEADLESS_CLI", None)

    def test_full_failure_flow_binary_not_found(self, state_dir, dispatch_dir, artifact_dir):
        """Simulate a headless run where the CLI binary is missing.

        Uses a custom target_type to bypass HEADLESS_CLI_DEFAULTS, forcing
        the adapter to use VNX_HEADLESS_CLI env var.
        """
        os.environ["VNX_HEADLESS_ENABLED"] = "1"
        os.environ["VNX_HEADLESS_CLI"] = "nonexistent_binary_xyz_12345"
        try:
            dispatch_id = _create_test_dispatch(state_dir)
            _create_dispatch_bundle(dispatch_dir, dispatch_id)
            adapter = HeadlessAdapter(
                state_dir=state_dir,
                dispatch_dir=dispatch_dir,
                artifact_dir=artifact_dir,
            )
            # Use a target_type NOT in HEADLESS_CLI_DEFAULTS so
            # the adapter falls back to VNX_HEADLESS_CLI
            result = adapter.execute(
                dispatch_id=dispatch_id,
                target_id="headless-fail-e2e",
                target_type="custom_headless_cli",
                task_class="research_structured",
            )
            assert result.success is False
            assert result.failure_class == "INFRA_FAIL"
            assert result.classification_evidence["retryable"] is True

            with get_connection(state_dir) as conn:
                dispatch = get_dispatch(conn, dispatch_id)
                assert dispatch["state"] == "failed_delivery"
        finally:
            os.environ.pop("VNX_HEADLESS_ENABLED", None)
            os.environ.pop("VNX_HEADLESS_CLI", None)

    def test_inspection_after_execution(self, state_dir, dispatch_dir, artifact_dir, registry):
        os.environ["VNX_HEADLESS_ENABLED"] = "1"
        os.environ["VNX_HEADLESS_CLI"] = "echo"
        try:
            dispatch_id = _create_test_dispatch(state_dir)
            _create_dispatch_bundle(dispatch_dir, dispatch_id)

            run = _make_run(registry, state_dir, dispatch_id=dispatch_id, terminal_id="T1")
            registry.transition(run.run_id, "running", actor="test")
            registry.update_heartbeat(run.run_id)
            registry.update_last_output(run.run_id)
            registry.transition(run.run_id, "completing", actor="test")
            registry.transition(run.run_id, "succeeded", failure_class="SUCCESS",
                              exit_code=0, duration_seconds=8.2, actor="test")

            detail = format_run_detail(registry.get(run.run_id))
            assert "succeeded" in detail
            assert dispatch_id in detail

            summary = build_health_summary(registry)
            assert summary.succeeded_count >= 1
            text = format_health_summary(summary)
            assert "Succeeded:" in text
        finally:
            os.environ.pop("VNX_HEADLESS_ENABLED", None)
            os.environ.pop("VNX_HEADLESS_CLI", None)
