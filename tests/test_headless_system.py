#!/usr/bin/env python3
"""
Tests for PR-1: Headless CLI Target Registry And Dispatch Adapter.

Covers:
  - Execution target registry: CRUD, health, capabilities, one-per-terminal
  - Dispatch router: routing invariants R-1 through R-8, fallbacks
  - Headless adapter: eligibility, subprocess execution, attempt/receipt recording
  - Interactive fallback when headless is unavailable
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add scripts/lib to path
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from runtime_coordination import init_schema, get_connection
from execution_target_registry import (
    ExecutionTargetRegistry,
    TargetRecord,
    TargetExistsError,
    TargetNotFoundError,
    TerminalOccupiedError,
    InvalidTargetTypeError,
    InvalidHealthStateError,
    InvalidCapabilityError,
    VALID_TARGET_TYPES,
    VALID_TASK_CLASSES,
    VALID_HEALTH_STATES,
)
from dispatch_router import (
    DispatchRouter,
    RoutingDecision,
    SKILL_TO_TASK_CLASS,
)
from headless_adapter import (
    HeadlessAdapter,
    HeadlessDisabledError,
    HeadlessIneligibleError,
    HEADLESS_ELIGIBLE_TASK_CLASSES,
)


class _DBTestCase(unittest.TestCase):
    """Base class that sets up a temp DB with all schema migrations."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self._tmpdir) / "state"
        self.state_dir.mkdir()
        self.dispatch_dir = Path(self._tmpdir) / "dispatches"
        self.dispatch_dir.mkdir()

        schemas_dir = Path(__file__).resolve().parent.parent / "schemas"
        init_schema(self.state_dir, schemas_dir / "runtime_coordination.sql")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_bundle(self, dispatch_id: str, prompt: str = "test prompt", **meta):
        bundle_dir = self.dispatch_dir / dispatch_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = {
            "dispatch_id": dispatch_id,
            "bundle_version": 1,
            "terminal_id": meta.get("terminal_id"),
            "track": meta.get("track"),
            "pr_ref": meta.get("pr_ref"),
            "gate": meta.get("gate"),
            "priority": meta.get("priority", "P2"),
            "expected_outputs": [],
            "intelligence_refs": [],
            "target_profile": meta.get("target_profile", {}),
            "metadata": meta.get("metadata", {}),
        }
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle))
        (bundle_dir / "prompt.txt").write_text(prompt)

    def _register_dispatch(self, dispatch_id: str, state: str = "queued", **kwargs):
        with get_connection(self.state_dir) as conn:
            from runtime_coordination import register_dispatch
            register_dispatch(conn, dispatch_id=dispatch_id, **kwargs)
            if state != "queued":
                from runtime_coordination import transition_dispatch
                if state == "claimed":
                    transition_dispatch(conn, dispatch_id=dispatch_id, to_state="claimed", actor="test")
            conn.commit()


# ============================================================================
# EXECUTION TARGET REGISTRY TESTS
# ============================================================================

class TestTargetRegistration(_DBTestCase):

    def test_register_interactive_target(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        # Remove seeded target from schema v4 so we can test fresh registration
        for t in reg.list_all():
            reg.remove(t.target_id)
        target = reg.register(
            "interactive_tmux_claude_T1",
            "interactive_tmux_claude",
            terminal_id="T1",
            capabilities=["coding_interactive", "research_structured"],
            health="healthy",
            model="sonnet",
        )
        self.assertEqual(target.target_id, "interactive_tmux_claude_T1")
        self.assertEqual(target.target_type, "interactive_tmux_claude")
        self.assertEqual(target.terminal_id, "T1")
        self.assertEqual(target.capabilities, ["coding_interactive", "research_structured"])
        self.assertEqual(target.health, "healthy")
        self.assertTrue(target.is_interactive)
        self.assertFalse(target.is_headless)
        self.assertTrue(target.is_routing_eligible)

    def test_register_headless_target(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        target = reg.register(
            "headless_claude_cli_T2",
            "headless_claude_cli",
            terminal_id="T2",
            capabilities=["research_structured", "docs_synthesis"],
            health="healthy",
            model="sonnet",
        )
        self.assertTrue(target.is_headless)
        self.assertFalse(target.is_interactive)
        self.assertTrue(target.supports_task_class("research_structured"))
        self.assertFalse(target.supports_task_class("coding_interactive"))

    def test_duplicate_target_id_raises(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", terminal_id="T1")
        with self.assertRaises(TargetExistsError):
            reg.register("t1", "interactive_tmux_claude", terminal_id="T1")

    def test_one_active_per_terminal(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", terminal_id="T1", health="healthy")
        with self.assertRaises(TerminalOccupiedError):
            reg.register("t1b", "headless_claude_cli", terminal_id="T1", health="healthy")

    def test_offline_targets_dont_block_terminal(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", terminal_id="T1", health="offline")
        target2 = reg.register("t1b", "headless_claude_cli", terminal_id="T1", health="healthy")
        self.assertEqual(target2.health, "healthy")

    def test_channel_adapter_no_terminal(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        from execution_target_registry import TargetRegistryError
        with self.assertRaises(TargetRegistryError):
            reg.register("ch1", "channel_adapter", terminal_id="T1")

    def test_channel_adapter_null_terminal(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        target = reg.register(
            "ch1", "channel_adapter",
            capabilities=["channel_response"],
            health="healthy",
        )
        self.assertIsNone(target.terminal_id)

    def test_invalid_target_type(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(InvalidTargetTypeError):
            reg.register("bad", "not_a_type")

    def test_invalid_health(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(InvalidHealthStateError):
            reg.register("bad", "interactive_tmux_claude", health="broken")

    def test_invalid_capability(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(InvalidCapabilityError):
            reg.register("bad", "interactive_tmux_claude", capabilities=["not_real"])


class TestTargetDeregisterAndRemove(_DBTestCase):

    def test_deregister_sets_offline(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", health="healthy")
        reg.deregister("t1")
        target = reg.get("t1")
        self.assertEqual(target.health, "offline")

    def test_deregister_not_found(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(TargetNotFoundError):
            reg.deregister("nonexistent")

    def test_remove_deletes_row(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude")
        reg.remove("t1")
        self.assertIsNone(reg.get("t1"))

    def test_remove_not_found(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(TargetNotFoundError):
            reg.remove("nonexistent")


class TestTargetHealth(_DBTestCase):

    def test_update_health(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", terminal_id="T1", health="offline")
        updated = reg.update_health("t1", "healthy")
        self.assertEqual(updated.health, "healthy")
        self.assertIsNotNone(updated.health_checked_at)

    def test_health_transition_blocks_occupied_terminal(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1a", "interactive_tmux_claude", terminal_id="T1", health="healthy")
        reg.register("t1b", "headless_claude_cli", terminal_id="T1", health="offline")
        with self.assertRaises(TerminalOccupiedError):
            reg.update_health("t1b", "healthy")

    def test_update_health_not_found(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        with self.assertRaises(TargetNotFoundError):
            reg.update_health("nonexistent", "healthy")


class TestTargetQueries(_DBTestCase):

    def _setup_targets(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        # Remove seeded targets so we control the test environment
        for t in reg.list_all():
            reg.remove(t.target_id)

        reg.register("it1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive", "research_structured"], health="healthy")
        reg.register("it2", "interactive_tmux_claude", terminal_id="T2",
                      capabilities=["coding_interactive", "research_structured", "docs_synthesis"],
                      health="degraded")
        reg.register("ht3", "headless_claude_cli", terminal_id="T3",
                      capabilities=["research_structured", "docs_synthesis"], health="healthy")
        reg.register("offline", "headless_codex_cli",
                      capabilities=["research_structured"], health="offline")
        return reg

    def test_list_all(self):
        reg = self._setup_targets()
        targets = reg.list_all()
        self.assertEqual(len(targets), 4)

    def test_list_by_type(self):
        reg = self._setup_targets()
        interactive = reg.list_by_type("interactive_tmux_claude")
        self.assertEqual(len(interactive), 2)

    def test_list_by_terminal(self):
        reg = self._setup_targets()
        t1 = reg.list_by_terminal("T1")
        self.assertEqual(len(t1), 1)
        self.assertEqual(t1[0].target_id, "it1")

    def test_list_routing_eligible(self):
        reg = self._setup_targets()
        eligible = reg.list_routing_eligible("research_structured")
        self.assertEqual(len(eligible), 3)
        # Healthy targets first
        self.assertEqual(eligible[0].health, "healthy")

    def test_routing_eligible_excludes_offline(self):
        reg = self._setup_targets()
        eligible = reg.list_routing_eligible("research_structured")
        target_ids = [t.target_id for t in eligible]
        self.assertNotIn("offline", target_ids)

    def test_routing_eligible_with_terminal(self):
        reg = self._setup_targets()
        eligible = reg.list_routing_eligible("coding_interactive", terminal_id="T1")
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].target_id, "it1")

    def test_list_headless_targets(self):
        reg = self._setup_targets()
        headless = reg.list_headless_targets(healthy_only=True)
        self.assertEqual(len(headless), 1)
        self.assertEqual(headless[0].target_id, "ht3")

    def test_list_headless_targets_all(self):
        reg = self._setup_targets()
        headless = reg.list_headless_targets(healthy_only=False)
        self.assertEqual(len(headless), 2)


class TestCoordinationEvents(_DBTestCase):

    def test_register_emits_event(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude")
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'target_registered'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["entity_id"], "t1")

    def test_health_change_emits_event(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        reg.register("t1", "interactive_tmux_claude", health="offline")
        reg.update_health("t1", "healthy")
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'target_health_changed'"
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_state"], "offline")
        self.assertEqual(events[0]["to_state"], "healthy")


# ============================================================================
# DISPATCH ROUTER TESTS
# ============================================================================

class TestTaskClassResolution(unittest.TestCase):

    def test_explicit_override(self):
        tc = DispatchRouter.resolve_task_class(
            skill="backend-developer", explicit_task_class="docs_synthesis"
        )
        self.assertEqual(tc, "docs_synthesis")

    def test_skill_mapping(self):
        self.assertEqual(DispatchRouter.resolve_task_class(skill="architect"), "research_structured")
        self.assertEqual(DispatchRouter.resolve_task_class(skill="backend-developer"), "coding_interactive")
        self.assertEqual(DispatchRouter.resolve_task_class(skill="excel-reporter"), "docs_synthesis")

    def test_skill_with_slash_prefix(self):
        self.assertEqual(DispatchRouter.resolve_task_class(skill="/reviewer"), "research_structured")

    def test_unknown_skill_defaults_to_coding(self):
        self.assertEqual(DispatchRouter.resolve_task_class(skill="unknown-skill"), "coding_interactive")

    def test_no_skill_defaults_to_coding(self):
        self.assertEqual(DispatchRouter.resolve_task_class(), "coding_interactive")


class TestRoutingInvariants(_DBTestCase):

    def _setup_mixed_targets(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)

        reg.register("it1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive", "research_structured", "docs_synthesis", "ops_watchdog"],
                      health="healthy", model="sonnet")
        reg.register("ht2", "headless_claude_cli", terminal_id="T2",
                      capabilities=["research_structured", "docs_synthesis"],
                      health="healthy", model="sonnet")
        return DispatchRouter(self.state_dir)

    def test_r1_coding_routes_interactive(self):
        """R-1: coding_interactive MUST route to interactive_tmux_*"""
        router = self._setup_mixed_targets()
        with patch.dict(os.environ, {"VNX_HEADLESS_ROUTING": "1"}):
            decision = router.route("d1", "coding_interactive")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_type, "interactive_tmux_claude")

    def test_r1_coding_never_headless(self):
        """R-1: coding_interactive never routes to headless even if only headless available"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("ht1", "headless_claude_cli",
                      capabilities=["coding_interactive", "research_structured"],
                      health="healthy")
        router = DispatchRouter(self.state_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ROUTING": "1"}):
            decision = router.route("d1", "coding_interactive")
        # Should NOT route to headless for coding
        self.assertFalse(decision.routed)
        self.assertTrue(decision.queued)

    def test_r2_research_routes_headless_when_enabled(self):
        """R-2: research_structured MAY route to headless when available"""
        router = self._setup_mixed_targets()
        with patch.dict(os.environ, {"VNX_HEADLESS_ROUTING": "1"}):
            decision = router.route("d1", "research_structured")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_id, "ht2")
        self.assertFalse(decision.fallback_used)

    def test_r2_research_falls_back_to_interactive(self):
        """R-2: research_structured falls back to interactive when no headless"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("it1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["research_structured"], health="healthy")
        router = DispatchRouter(self.state_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ROUTING": "1"}):
            decision = router.route("d1", "research_structured")
        self.assertTrue(decision.routed)
        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.selected_target_type, "interactive_tmux_claude")

    def test_r2_headless_routing_disabled_uses_interactive(self):
        """When VNX_HEADLESS_ROUTING=0, research routes interactive"""
        router = self._setup_mixed_targets()
        with patch.dict(os.environ, {"VNX_HEADLESS_ROUTING": "0"}):
            decision = router.route("d1", "research_structured")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_type, "interactive_tmux_claude")

    def test_r3_channel_response_requires_inbox(self):
        """R-3: channel_response without channel_origin is rejected"""
        router = self._setup_mixed_targets()
        decision = router.route("d1", "channel_response")
        self.assertFalse(decision.routed)
        self.assertIn("R-3", decision.escalation_reason)

    def test_r3_channel_response_with_origin(self):
        """R-3: channel_response with channel_origin passes inbox check"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("ch1", "channel_adapter",
                      capabilities=["channel_response"], health="healthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "channel_response", channel_origin="slack-ch1")
        self.assertTrue(decision.routed)

    def test_r4_ops_prefers_interactive(self):
        """R-4: ops_watchdog prefers interactive"""
        router = self._setup_mixed_targets()
        decision = router.route("d1", "ops_watchdog")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_type, "interactive_tmux_claude")

    def test_r5_capability_mismatch_blocks_routing(self):
        """R-5: Cannot route to target without declared capability"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("it1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive"], health="healthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "docs_synthesis")
        self.assertFalse(decision.routed)

    def test_r6_unhealthy_excluded(self):
        """R-6: Unhealthy targets are excluded from routing"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("it1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive"], health="unhealthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "coding_interactive")
        self.assertFalse(decision.routed)

    def test_r7_routing_emits_event(self):
        """R-7: All routing decisions emit coordination_events"""
        router = self._setup_mixed_targets()
        router.route("d1", "coding_interactive")
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT * FROM coordination_events WHERE event_type = 'routing_decision'"
            ).fetchall()
        self.assertGreaterEqual(len(events), 1)

    def test_r8_override_respects_health(self):
        """R-8: T0 override still respects R-6 (health check)"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("t1", "interactive_tmux_claude",
                      capabilities=["coding_interactive"], health="unhealthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "coding_interactive", target_id_override="t1")
        self.assertFalse(decision.routed)
        self.assertIn("R-6", decision.escalation_reason)

    def test_r8_override_respects_capability(self):
        """R-8: T0 override still respects R-5 (capability check)"""
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("t1", "interactive_tmux_claude",
                      capabilities=["coding_interactive"], health="healthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "docs_synthesis", target_id_override="t1")
        self.assertFalse(decision.routed)
        self.assertIn("R-5", decision.escalation_reason)

    def test_r8_valid_override(self):
        """R-8: Valid T0 override routes successfully"""
        router = self._setup_mixed_targets()
        decision = router.route("d1", "research_structured", target_id_override="ht2")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_id, "ht2")


class TestRoutingFallbacks(_DBTestCase):

    def test_no_targets_coding_queues_with_escalation(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "coding_interactive")
        self.assertFalse(decision.routed)
        self.assertTrue(decision.queued)
        self.assertIn("escalation", decision.escalation_reason.lower())

    def test_all_unhealthy_escalates(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("t1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive"], health="unhealthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "coding_interactive")
        self.assertFalse(decision.routed)
        self.assertTrue(decision.queued)

    def test_healthy_preferred_over_degraded(self):
        reg = ExecutionTargetRegistry(self.state_dir)
        for t in reg.list_all():
            reg.remove(t.target_id)
        reg.register("degraded1", "interactive_tmux_claude", terminal_id="T1",
                      capabilities=["coding_interactive"], health="degraded")
        reg.register("healthy1", "interactive_tmux_claude", terminal_id="T2",
                      capabilities=["coding_interactive"], health="healthy")
        router = DispatchRouter(self.state_dir)
        decision = router.route("d1", "coding_interactive")
        self.assertTrue(decision.routed)
        self.assertEqual(decision.selected_target_id, "healthy1")


# ============================================================================
# HEADLESS ADAPTER TESTS
# ============================================================================

class TestHeadlessEligibility(_DBTestCase):

    def test_eligible_task_classes(self):
        self.assertTrue(HeadlessAdapter.is_eligible("research_structured"))
        self.assertTrue(HeadlessAdapter.is_eligible("docs_synthesis"))

    def test_ineligible_task_classes(self):
        self.assertFalse(HeadlessAdapter.is_eligible("coding_interactive"))
        self.assertFalse(HeadlessAdapter.is_eligible("ops_watchdog"))
        self.assertFalse(HeadlessAdapter.is_eligible("channel_response"))

    def test_validate_disabled_raises(self):
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ENABLED": "0"}):
            with self.assertRaises(HeadlessDisabledError):
                adapter.validate_eligibility("d1", "research_structured")

    def test_validate_coding_raises(self):
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ENABLED": "1"}):
            with self.assertRaises(HeadlessIneligibleError):
                adapter.validate_eligibility("d1", "coding_interactive")

    def test_validate_none_task_class_raises(self):
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ENABLED": "1"}):
            with self.assertRaises(HeadlessIneligibleError):
                adapter.validate_eligibility("d1", None)


class TestHeadlessExecution(_DBTestCase):

    def _setup_for_execution(self, dispatch_id="d-headless-1"):
        self._write_bundle(dispatch_id, prompt="Analyze the architecture of module X")
        self._register_dispatch(dispatch_id)
        return HeadlessAdapter(self.state_dir, self.dispatch_dir)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_successful_execution(self, mock_run, mock_which, mock_enabled):
        adapter = self._setup_for_execution()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Analysis complete: module X uses layered architecture",
            stderr="",
        )
        result = adapter.execute(
            "d-headless-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.dispatch_id, "d-headless-1")
        self.assertIsNotNone(result.attempt_id)
        self.assertGreater(len(result.stdout), 0)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_failed_execution(self, mock_run, mock_which, mock_enabled):
        adapter = self._setup_for_execution("d-fail-1")
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: context limit exceeded",
        )
        result = adapter.execute(
            "d-fail-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertFalse(result.success)
        self.assertIn("code 1", result.failure_reason)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_timeout_execution(self, mock_run, mock_which, mock_enabled):
        import subprocess as sp
        adapter = self._setup_for_execution("d-timeout-1")
        mock_run.side_effect = sp.TimeoutExpired(cmd="claude", timeout=600)
        result = adapter.execute(
            "d-timeout-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        self.assertFalse(result.success)
        self.assertIn("timed out", result.failure_reason)

    @patch("headless_adapter.headless_enabled", return_value=True)
    def test_missing_binary(self, mock_enabled):
        adapter = self._setup_for_execution("d-nobin-1")
        with patch("shutil.which", return_value=None):
            result = adapter.execute(
                "d-nobin-1", "headless_claude_cli_T2", "headless_claude_cli",
                task_class="research_structured",
            )
        self.assertFalse(result.success)
        self.assertIn("not found", result.failure_reason)

    def test_missing_bundle(self):
        self._register_dispatch("d-nobundle-1")
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        with patch.dict(os.environ, {"VNX_HEADLESS_ENABLED": "1"}):
            result = adapter.execute(
                "d-nobundle-1", "headless_claude_cli_T2", "headless_claude_cli",
                task_class="research_structured",
            )
        self.assertFalse(result.success)
        self.assertIn("Bundle not found", result.failure_reason)


class TestHeadlessAttemptRecording(_DBTestCase):

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_attempt_recorded_in_db(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-attempt-1", prompt="test prompt")
        self._register_dispatch("d-attempt-1")
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        adapter.execute(
            "d-attempt-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            attempts = conn.execute(
                "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?",
                ("d-attempt-1",),
            ).fetchall()
        self.assertGreaterEqual(len(attempts), 1)

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_dispatch_transitions_to_completed(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-complete-1", prompt="test")
        self._register_dispatch("d-complete-1")
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")
        adapter.execute(
            "d-complete-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT state FROM dispatches WHERE dispatch_id = ?",
                ("d-complete-1",),
            ).fetchone()
        self.assertEqual(dispatch["state"], "completed")

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_dispatch_transitions_to_failed_on_error(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-fail-2", prompt="test")
        self._register_dispatch("d-fail-2")
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        adapter.execute(
            "d-fail-2", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            dispatch = conn.execute(
                "SELECT state FROM dispatches WHERE dispatch_id = ?",
                ("d-fail-2",),
            ).fetchone()
        self.assertEqual(dispatch["state"], "failed_delivery")

    @patch("headless_adapter.headless_enabled", return_value=True)
    @patch("shutil.which", return_value="/usr/bin/echo")
    @patch("subprocess.run")
    def test_headless_events_emitted(self, mock_run, mock_which, mock_enabled):
        self._write_bundle("d-events-1", prompt="test")
        self._register_dispatch("d-events-1")
        adapter = HeadlessAdapter(self.state_dir, self.dispatch_dir)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        adapter.execute(
            "d-events-1", "headless_claude_cli_T2", "headless_claude_cli",
            task_class="research_structured",
        )
        with get_connection(self.state_dir) as conn:
            events = conn.execute(
                "SELECT event_type FROM coordination_events WHERE entity_id = ?",
                ("d-events-1",),
            ).fetchall()
        event_types = [e["event_type"] for e in events]
        self.assertIn("headless_subprocess_start", event_types)
        self.assertIn("headless_execution_completed", event_types)


if __name__ == "__main__":
    unittest.main()
