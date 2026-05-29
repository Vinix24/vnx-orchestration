"""Tests for G6/G7/G8: smart-router multiprovider complete.

G6: auto-route selecting a non-Claude model dispatches to the right provider
    via provider_dispatch, not a Claude fallback.
G7: smart_router and routing_policy are not double-applied — _select_dispatch_path
    is suppressed when smart_router already ran.
G8: constraint-violating candidates are filtered during routing (never recommended).

Dispatch-ID: 20260529-161912-smartrouter-complete
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from smart_router import decide, parse_route_model_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def recs_deepseek_wins(tmp_path):
    """deepseek ranked first (capable tier, explicit low cost)."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "deepseek-v4-pro", "composite_score": 9.0,
                 "avg_duration_seconds": 212.0, "cost_usd_per_call": 0.001},
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
            ],
        },
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def recs_kimi_wins(tmp_path):
    """kimi CLI ranked first (capable tier, explicit low cost)."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "kimi-k2-0905", "composite_score": 9.0,
                 "avg_duration_seconds": 200.0, "cost_usd_per_call": 0.001},
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
            ],
        },
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def recs_deepseek_only(tmp_path):
    """Only deepseek — filtering it leaves no candidates."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "deepseek-v4-pro", "composite_score": 9.0,
                 "avg_duration_seconds": 212.0, "cost_usd_per_call": 0.001},
            ],
        },
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# G8: constraint filtering in smart_router.decide()
# ---------------------------------------------------------------------------

class TestG8ConstraintFiltering:
    """Constraint-violating candidates must be filtered before recommendation."""

    def test_deepseek_filtered_when_no_api_key(self, recs_deepseek_wins, monkeypatch):
        """deepseek ranked first but blocked when DEEPSEEK_API_KEY absent → claude wins."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        decision = decide(
            instruction="implement feature",
            role="backend-developer",
            recommendations_path=recs_deepseek_wins,
        )

        assert decision.primary is not None
        assert decision.primary.model_id == "claude-sonnet-4-6"
        assert "deepseek-harness-subscription-blocked" in decision.constraints_applied

    def test_deepseek_allowed_when_api_key_set(self, recs_deepseek_wins, monkeypatch):
        """deepseek passes constraint check when DEEPSEEK_API_KEY is set."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

        decision = decide(
            instruction="implement feature",
            role="backend-developer",
            recommendations_path=recs_deepseek_wins,
        )

        assert decision.primary is not None
        assert decision.primary.model_id == "deepseek-v4-pro"
        assert not decision.constraints_applied

    def test_kimi_cli_not_filtered(self, recs_kimi_wins):
        """kimi CLI lane (provider=kimi, not moonshot) must never be filtered."""
        decision = decide(
            instruction="implement feature",
            role="backend-developer",
            recommendations_path=recs_kimi_wins,
        )

        assert decision.primary is not None
        assert decision.primary.model_id == "kimi-k2-0905"
        assert not decision.constraints_applied

    def test_all_filtered_primary_is_none(self, recs_deepseek_only, monkeypatch):
        """All candidates blocked → primary is None; no crash, no silent fallback."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        decision = decide(
            instruction="implement feature",
            recommendations_path=recs_deepseek_only,
        )

        assert decision.primary is None
        assert "deepseek-harness-subscription-blocked" in decision.constraints_applied

    def test_constraints_applied_recorded_in_decision(self, recs_deepseek_wins, monkeypatch):
        """Filtered constraint IDs appear in RouteDecision.constraints_applied."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        decision = decide(
            instruction="implement feature",
            recommendations_path=recs_deepseek_wins,
        )

        assert isinstance(decision.constraints_applied, list)
        assert len(decision.constraints_applied) >= 1
        assert "deepseek-harness-subscription-blocked" in decision.constraints_applied


# ---------------------------------------------------------------------------
# G7: single coherent decision path
# ---------------------------------------------------------------------------

class TestG7SingleDecisionPath:
    """_select_dispatch_path short-circuits when auto_route_applied=True (G7)."""

    def test_routing_policy_skipped_when_auto_route_applied(self):
        """With VNX_ROUTING_POLICY_ENABLED=1 AND auto_route_applied=True → policy skipped."""
        from subprocess_dispatch import _select_dispatch_path

        provider, model = _select_dispatch_path(
            task_class="01_code_generation",
            complexity="medium",
            current_model="sonnet",
            env={"VNX_ROUTING_POLICY_ENABLED": "1"},
            auto_route_applied=True,
        )
        assert provider is None
        assert model == "sonnet"

    def test_routing_policy_no_op_without_flag(self):
        """Without VNX_ROUTING_POLICY_ENABLED the function is always a no-op."""
        from subprocess_dispatch import _select_dispatch_path

        provider, model = _select_dispatch_path(
            task_class="01_code_generation",
            complexity="medium",
            current_model="sonnet",
            env={},
            auto_route_applied=False,
        )
        assert provider is None
        assert model == "sonnet"

    def test_auto_route_applied_blocks_even_with_policy_enabled(self):
        """auto_route_applied=True takes precedence over VNX_ROUTING_POLICY_ENABLED."""
        from subprocess_dispatch import _select_dispatch_path

        # Both flags set: smart_router decision must win.
        provider, model = _select_dispatch_path(
            task_class="03_refactoring",
            complexity="high",
            current_model="haiku",
            env={"VNX_ROUTING_POLICY_ENABLED": "1", "VNX_USE_CHEAP_LANE": "1"},
            auto_route_applied=True,
        )
        assert provider is None
        assert model == "haiku"


# ---------------------------------------------------------------------------
# G6: non-Claude auto-route dispatches to provider_dispatch, not Claude
# ---------------------------------------------------------------------------

class TestG6NonClaudeDispatch:
    """auto-route → non-Claude model → dispatches via provider_dispatch, not deliver_with_recovery."""

    def test_kimi_auto_route_calls_dispatch_kimi(self, recs_kimi_wins, state_dir, monkeypatch):
        """When smart_router selects kimi, provider_dispatch._dispatch_kimi is called."""
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setattr("smart_router._RECOMMENDATIONS_PATH", recs_kimi_wins)

        deliver_calls: list = []

        with patch("subprocess_dispatch.deliver_with_recovery",
                   side_effect=lambda **kw: deliver_calls.append(kw) or True):
            with patch("provider_dispatch._dispatch_kimi", return_value=0) as mock_kimi:
                result = provider_dispatch.main([
                    "--provider", "claude",
                    "--terminal-id", "T1",
                    "--dispatch-id", "g6-kimi-test",
                    "--instruction", "implement new feature",
                    "--model", "sonnet",
                    "--auto-route",
                ])

        assert mock_kimi.called, "_dispatch_kimi must be called for kimi route"
        assert not deliver_calls, "deliver_with_recovery must NOT be called for non-Claude route"
        assert result == 0

    def test_non_claude_auto_route_no_claude_fallback(self, recs_kimi_wins, state_dir, monkeypatch):
        """G6 regression guard: deliver_with_recovery never invoked on non-Claude path."""
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setattr("smart_router._RECOMMENDATIONS_PATH", recs_kimi_wins)

        deliver_was_called: list = []

        with patch("subprocess_dispatch.deliver_with_recovery",
                   side_effect=lambda **kw: deliver_was_called.append(True) or True):
            with patch("provider_dispatch._dispatch_kimi", return_value=0):
                provider_dispatch.main([
                    "--provider", "claude",
                    "--terminal-id", "T1",
                    "--dispatch-id", "g6-regression",
                    "--instruction", "implement feature",
                    "--model", "sonnet",
                    "--auto-route",
                ])

        assert not deliver_was_called


class TestG6CheapLaneArgv:
    """_build_cheap_lane_argv produces correct argv for non-Claude providers."""

    def test_kimi_provider_in_argv(self):
        import argparse
        from subprocess_dispatch import _build_cheap_lane_argv

        args = argparse.Namespace(
            terminal_id="T1",
            dispatch_id="d-001",
            instruction="implement feature",
            model="kimi-k2-0905",
            role="backend-developer",
            max_retries=3,
            gate="",
            no_auto_commit=False,
            dispatch_paths="",
            pr_id=None,
        )
        argv = _build_cheap_lane_argv(args, "kimi")
        assert "--provider" in argv
        assert argv[argv.index("--provider") + 1] == "kimi"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "kimi-k2-0905"

    def test_litellm_deepseek_provider_in_argv(self):
        import argparse
        from subprocess_dispatch import _build_cheap_lane_argv

        args = argparse.Namespace(
            terminal_id="T1",
            dispatch_id="d-002",
            instruction="debug failing test",
            model="deepseek-v4-pro",
            role="debugger",
            max_retries=3,
            gate="",
            no_auto_commit=False,
            dispatch_paths="",
            pr_id=None,
        )
        argv = _build_cheap_lane_argv(args, "litellm:deepseek:deepseek-v4-pro")
        assert "--provider" in argv
        assert argv[argv.index("--provider") + 1] == "litellm:deepseek:deepseek-v4-pro"
