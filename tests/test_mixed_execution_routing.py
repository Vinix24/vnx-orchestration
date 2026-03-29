#!/usr/bin/env python3
"""
Integration tests for PR-5: Mixed Execution Routing Cutover and FP-C Certification.

Covers:
  - MixedExecutionRouter routing decisions
  - Cutover controls and rollback
  - Intelligence injection in live dispatch path
  - Channel event -> inbox -> dispatch -> routing lifecycle
  - FP-C certification runner
  - Interactive coding default enforcement (G-R2)
  - Feature flag behavior
"""

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest

# Add scripts/lib to path for imports
LIB_DIR = Path(__file__).parent.parent / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

from runtime_coordination import get_connection, init_schema
from execution_target_registry import (
    ExecutionTargetRegistry,
    HEADLESS_TARGET_TYPES,
    INTERACTIVE_TARGET_TYPES,
)
from dispatch_router import DispatchRouter, RoutingDecision
from intelligence_selector import IntelligenceSelector, MAX_ITEMS_PER_INJECTION
from inbound_inbox import InboundInbox
from recommendation_tracker import RecommendationTracker
from mixed_execution_router import (
    MixedExecutionRouter,
    MixedRoutingResult,
    mixed_execution_enabled,
    cutover_config_from_env,
    load_mixed_router,
)
from fpc_certification import FPCCertificationRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_env(tmp_path):
    """Create a temporary state directory with schema and seeded targets."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    output_dir = tmp_path / "headless_output"
    output_dir.mkdir()

    # Initialize schema (init_schema applies base + all migrations including v4)
    init_schema(state_dir)

    # Seed targets
    registry = ExecutionTargetRegistry(state_dir)
    registry.register(
        target_id="test_interactive_T1",
        target_type="interactive_tmux_claude",
        terminal_id="T1",
        capabilities=["coding_interactive", "research_structured", "docs_synthesis", "ops_watchdog"],
        health="healthy",
        model="sonnet",
    )
    registry.register(
        target_id="test_interactive_T2",
        target_type="interactive_tmux_claude",
        terminal_id="T2",
        capabilities=["coding_interactive", "research_structured", "docs_synthesis"],
        health="healthy",
        model="sonnet",
    )
    registry.register(
        target_id="test_headless_claude",
        target_type="headless_claude_cli",
        terminal_id=None,
        capabilities=["research_structured", "docs_synthesis"],
        health="healthy",
        model="sonnet",
    )
    registry.register(
        target_id="test_channel_adapter",
        target_type="channel_adapter",
        terminal_id=None,
        capabilities=["channel_response"],
        health="healthy",
    )

    return {
        "state_dir": state_dir,
        "dispatch_dir": dispatch_dir,
        "output_dir": output_dir,
    }


@pytest.fixture
def router(state_env):
    """Return a MixedExecutionRouter."""
    return MixedExecutionRouter(
        state_dir=state_env["state_dir"],
        dispatch_dir=state_env["dispatch_dir"],
        output_dir=state_env["output_dir"],
    )


def _register_dispatch(state_dir, dispatch_id, terminal_id="T1"):
    """Helper to register a dispatch in the DB."""
    from runtime_coordination import register_dispatch
    with get_connection(state_dir) as conn:
        register_dispatch(
            conn,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            track="C",
            priority="P1",
            bundle_path=f"/tmp/{dispatch_id}",
            actor="test",
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Test: Cutover disabled (default) returns interactive-only
# ---------------------------------------------------------------------------

class TestCutoverDisabled:
    """When VNX_MIXED_EXECUTION=0 (default), all routing is interactive."""

    def test_cutover_disabled_returns_interactive(self, router, state_env):
        with mock.patch.dict(os.environ, {"VNX_MIXED_EXECUTION": "0"}):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="research_structured")

            assert result.execution_mode == "interactive"
            assert result.cutover_active is False
            assert result.rollback_available is True
            assert result.routing_decision is None  # No routing when cutover disabled

    def test_load_mixed_router_returns_none_when_disabled(self, state_env):
        with mock.patch.dict(os.environ, {"VNX_MIXED_EXECUTION": "0"}):
            r = load_mixed_router(
                state_env["state_dir"],
                state_env["dispatch_dir"],
            )
            assert r is None


# ---------------------------------------------------------------------------
# Test: Cutover enabled, coding stays interactive (G-R2)
# ---------------------------------------------------------------------------

class TestCodingStaysInteractive:
    """G-R2: Coding tasks always route to interactive targets."""

    def test_coding_interactive_never_headless(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
            "VNX_HEADLESS_ENABLED": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="coding_interactive")

            assert result.routed
            assert result.execution_mode == "interactive"
            assert result.routing_decision.selected_target_type in INTERACTIVE_TARGET_TYPES

    def test_backend_developer_skill_stays_interactive(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, skill_name="backend-developer")

            assert result.task_class == "coding_interactive"
            assert result.routed
            assert result.execution_mode == "interactive"


# ---------------------------------------------------------------------------
# Test: Headless routing for eligible task classes
# ---------------------------------------------------------------------------

class TestHeadlessRouting:
    """Research and docs task classes route headless when enabled."""

    def test_research_structured_routes_headless(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
            "VNX_HEADLESS_ENABLED": "0",  # Don't actually execute
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="research_structured")

            assert result.routed
            # With headless routing enabled but execution disabled,
            # the target is selected headless but execution doesn't happen
            assert result.routing_decision.selected_target_type in HEADLESS_TARGET_TYPES

    def test_docs_synthesis_routes_headless(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
            "VNX_HEADLESS_ENABLED": "0",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="docs_synthesis")

            assert result.routed
            assert result.routing_decision.selected_target_type in HEADLESS_TARGET_TYPES

    def test_headless_routing_disabled_falls_back(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "0",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="research_structured")

            assert result.routed
            assert result.routing_decision.selected_target_type in INTERACTIVE_TARGET_TYPES


# ---------------------------------------------------------------------------
# Test: Rollback controls
# ---------------------------------------------------------------------------

class TestRollbackControls:
    """VNX_HEADLESS_ROUTING=0 and VNX_MIXED_EXECUTION=0 roll back cleanly."""

    def test_rollback_to_interactive_instructions(self, router):
        instructions = router.rollback_to_interactive()

        assert instructions["action"] == "rollback_to_interactive"
        assert instructions["instructions"]["VNX_MIXED_EXECUTION"] == "0"
        assert instructions["instructions"]["VNX_HEADLESS_ROUTING"] == "0"
        assert instructions["reversible"] is True

    def test_cutover_status(self, router):
        status = router.cutover_status()

        assert "cutover_config" in status
        assert "execution_targets" in status
        assert "rollback_available" in status
        assert status["rollback_available"] is True

    def test_rollback_mid_session(self, router, state_env):
        """Verify that flipping the flag mid-session changes routing behavior."""
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
        }):
            did1 = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did1)
            result1 = router.route_dispatch(did1, task_class="research_structured")
            assert result1.routing_decision.selected_target_type in HEADLESS_TARGET_TYPES

        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "0",
        }):
            did2 = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did2)
            result2 = router.route_dispatch(did2, task_class="research_structured")
            assert result2.routing_decision.selected_target_type in INTERACTIVE_TARGET_TYPES


# ---------------------------------------------------------------------------
# Test: Intelligence injection in dispatch path
# ---------------------------------------------------------------------------

class TestIntelligenceInjection:
    """Intelligence payload is visible in routing results."""

    def test_intelligence_payload_present(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_INTELLIGENCE_INJECTION": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(
                did,
                task_class="research_structured",
                skill_name="architect",
                track="C",
                gate="gate_test",
            )

            # Intelligence payload should be present (even if empty items)
            assert result.intelligence_payload is not None
            assert "injection_point" in result.intelligence_payload

    def test_intelligence_disabled(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "0",
            "VNX_INTELLIGENCE_INJECTION": "0",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="coding_interactive")

            assert result.intelligence_payload is None

    def test_intelligence_in_evidence_trail(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_INTELLIGENCE_INJECTION": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="research_structured")

            evidence = result.to_evidence_dict()
            assert "intelligence" in evidence or result.intelligence_payload is None


# ---------------------------------------------------------------------------
# Test: Channel event intake
# ---------------------------------------------------------------------------

class TestChannelEventIntake:
    """Channel events flow through inbox to dispatch routing."""

    def test_channel_event_receive_and_route(self, router, state_env):
        with mock.patch.dict(os.environ, {"VNX_MIXED_EXECUTION": "1"}):
            result = router.route_channel_event(
                channel_id="test_channel",
                payload={"type": "research_request", "content": "analyze"},
                routing_hints={"task_class": "channel_response"},
                dispatch_id_generator=lambda: f"ch_dispatch_{uuid.uuid4().hex[:8]}",
            )

            # Should route (or queue if channel adapter available)
            assert result.task_class == "channel_response"
            assert len(result.evidence_trail) > 0

    def test_duplicate_channel_event_rejected(self, router, state_env):
        payload = {"type": "task", "content": "dedup test", "unique_id": "test123"}

        with mock.patch.dict(os.environ, {"VNX_MIXED_EXECUTION": "1"}):
            result1 = router.route_channel_event(
                channel_id="dedup_channel",
                payload=payload,
                dispatch_id_generator=lambda: f"ch_{uuid.uuid4().hex[:8]}",
            )

            result2 = router.route_channel_event(
                channel_id="dedup_channel",
                payload=payload,
                dispatch_id_generator=lambda: f"ch_{uuid.uuid4().hex[:8]}",
            )

            assert result2.error is not None
            assert "Duplicate" in result2.error


# ---------------------------------------------------------------------------
# Test: T0 override routing
# ---------------------------------------------------------------------------

class TestT0Override:
    """T0 can override routing to specific targets."""

    def test_override_to_healthy_target(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(
                did,
                task_class="research_structured",
                target_id_override="test_interactive_T1",
            )

            assert result.routed
            assert result.routing_decision.selected_target_id == "test_interactive_T1"

    def test_override_to_unhealthy_target_rejected(self, router, state_env):
        # Register an unhealthy target
        registry = ExecutionTargetRegistry(state_env["state_dir"])
        registry.register(
            target_id="test_unhealthy",
            target_type="interactive_tmux_claude",
            terminal_id=None,
            capabilities=["research_structured"],
            health="unhealthy",
        )

        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(
                did,
                task_class="research_structured",
                target_id_override="test_unhealthy",
            )

            assert not result.routed
            assert "R-6" in (result.error or "")


# ---------------------------------------------------------------------------
# Test: Evidence trail completeness
# ---------------------------------------------------------------------------

class TestEvidenceTrail:
    """Every routing decision produces a reviewable evidence trail."""

    def test_evidence_trail_has_steps(self, router, state_env):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_INTELLIGENCE_INJECTION": "1",
        }):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="coding_interactive")

            steps = [e["step"] for e in result.evidence_trail]
            assert "cutover_check" in steps
            assert "task_class_resolution" in steps
            assert "intelligence_injection" in steps
            assert "routing_decision" in steps

    def test_evidence_dict_serializable(self, router, state_env):
        with mock.patch.dict(os.environ, {"VNX_MIXED_EXECUTION": "1"}):
            did = f"test_{uuid.uuid4().hex[:8]}"
            _register_dispatch(state_env["state_dir"], did)
            result = router.route_dispatch(did, task_class="coding_interactive")

            evidence = result.to_evidence_dict()
            serialized = json.dumps(evidence)
            assert len(serialized) > 0


# ---------------------------------------------------------------------------
# Test: Feature flag configuration
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_cutover_config_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            # Set only the ones we need, clear others
            for key in ["VNX_MIXED_EXECUTION", "VNX_HEADLESS_ROUTING",
                        "VNX_HEADLESS_ENABLED", "VNX_BROKER_SHADOW",
                        "VNX_INTELLIGENCE_INJECTION"]:
                os.environ.pop(key, None)

            config = cutover_config_from_env()
            assert config["mixed_execution"] is False
            assert config["headless_routing"] is False
            assert config["headless_enabled"] is False

    def test_cutover_config_all_enabled(self):
        with mock.patch.dict(os.environ, {
            "VNX_MIXED_EXECUTION": "1",
            "VNX_HEADLESS_ROUTING": "1",
            "VNX_HEADLESS_ENABLED": "1",
            "VNX_BROKER_SHADOW": "0",
            "VNX_INTELLIGENCE_INJECTION": "1",
        }):
            config = cutover_config_from_env()
            assert config["mixed_execution"] is True
            assert config["headless_routing"] is True
            assert config["headless_enabled"] is True
            assert config["broker_shadow"] is False
            assert config["intelligence_injection"] is True


# ---------------------------------------------------------------------------
# Test: FP-C Certification Runner
# ---------------------------------------------------------------------------

class TestFPCCertification:
    """The certification runner validates the full matrix."""

    def test_certification_runs_without_error(self):
        runner = FPCCertificationRunner()
        report = runner.run()

        assert report.generated_at != ""
        assert len(report.rows) > 0
        assert report.pass_count > 0

    def test_certification_produces_json_report(self):
        runner = FPCCertificationRunner()
        report = runner.run()
        report_dict = report.to_dict()

        assert "summary" in report_dict
        assert "rows" in report_dict
        assert "residual_risks" in report_dict
        assert report_dict["summary"]["total"] == len(report.rows)

        # Verify JSON serialization
        serialized = json.dumps(report_dict, indent=2)
        assert len(serialized) > 0

    def test_certification_has_no_failures(self):
        runner = FPCCertificationRunner()
        report = runner.run()

        if report.fail_count > 0:
            failures = [r for r in report.rows if r.status == "fail"]
            failure_details = "\n".join(
                f"  {r.row_id}: {r.scenario} — {r.evidence}" for r in failures
            )
            pytest.fail(f"FP-C certification has {report.fail_count} failures:\n{failure_details}")

    def test_certification_covers_all_sections(self):
        runner = FPCCertificationRunner()
        report = runner.run()

        sections = {r.section for r in report.rows}
        # Sections 1-7 from the certification matrix
        for s in ["1", "2", "3", "4", "5", "6", "7"]:
            assert s in sections, f"Section {s} missing from certification"

    def test_certification_residual_risks_documented(self):
        runner = FPCCertificationRunner()
        report = runner.run()

        assert len(report.residual_risks) > 0
        assert any("headless" in r.lower() for r in report.residual_risks)


# ---------------------------------------------------------------------------
# Test: MixedRoutingResult properties
# ---------------------------------------------------------------------------

class TestMixedRoutingResult:
    def test_default_result(self):
        result = MixedRoutingResult(
            dispatch_id="test",
            task_class="coding_interactive",
        )
        assert not result.routed
        assert not result.headless_executed
        assert not result.headless_succeeded
        assert result.execution_mode == "interactive"
        assert result.rollback_available is True

    def test_evidence_dict_structure(self):
        result = MixedRoutingResult(
            dispatch_id="test",
            task_class="coding_interactive",
            execution_mode="interactive",
            cutover_active=True,
        )
        d = result.to_evidence_dict()
        assert d["dispatch_id"] == "test"
        assert d["task_class"] == "coding_interactive"
        assert d["cutover_active"] is True
