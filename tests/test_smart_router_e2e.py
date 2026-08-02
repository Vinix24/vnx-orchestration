"""End-to-end tests for smart_router wiring in provider_dispatch.

Verifies that --auto-route produces a route_decisions.ndjson entry for all
provider paths (claude, kimi, gemini, codex, litellm). Covers the explicit
decide() + parse_route_model_id() + write_route_decision() pipeline that
replaced the bundled route() call.

Dispatch-ID: 20260517-fix-smart-router-enforcer
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from smart_router import (
    RouteCandidate,
    RouteDecision,
    decide,
    parse_route_model_id,
    write_route_decision,
)


@pytest.fixture
def recommendations_yaml(tmp_path):
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
                # kimi-k3 = the registered kimi_cli default; kimi-k2-0905 (the
                # retired moonshot litellm model) fails the kimi_cli registry
                # constraint check in provider_dispatch since 2026-07-21.
                {"model_id": "kimi-k3", "composite_score": 7.0,
                 "avg_duration_seconds": 200.0, "cost_usd_per_call": 0.02},
            ],
            "02_code_review": [
                {"model_id": "claude-opus-4-6", "composite_score": 10.0,
                 "avg_duration_seconds": 90.9, "cost_usd_per_call": None},
            ],
            "05_debugging": [
                {"model_id": "deepseek-v4-pro", "composite_score": 9.0,
                 "avg_duration_seconds": 180.0, "cost_usd_per_call": 0.01},
            ],
        },
    }
    yaml_path = tmp_path / "routing_recommendations.yaml"
    yaml_path.write_text(yaml.dump(data), encoding="utf-8")
    return yaml_path


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


class TestAutoRouteNdjsonPersistence:
    """Core blocker: --auto-route MUST produce a route_decisions.ndjson entry."""

    def test_claude_dispatch_writes_ndjson(self, recommendations_yaml, state_dir):
        decision = decide(
            instruction="implement the SubprocessAdapter",
            role="backend-developer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.primary is not None

        write_route_decision("dispatch-claude-001", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists(), "route_decisions.ndjson must be created"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "dispatch-claude-001"
        assert record["task_class"] == "01_code_generation"
        # Cost-aware routing: kimi-k3 has explicit cost=0.02 < sonnet's enriched ~0.045 → kimi wins.
        assert record["chosen_route"]["model_id"] == "kimi-k3"

    def test_kimi_route_writes_ndjson(self, recommendations_yaml, state_dir):
        decision = decide(
            instruction="implement the CLI adapter",
            role="backend-developer",
            recommendations_path=recommendations_yaml,
        )
        write_route_decision("dispatch-kimi-001", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists()
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "dispatch-kimi-001"
        assert "timestamp" in record

    def test_review_task_routes_to_opus(self, recommendations_yaml, state_dir):
        decision = decide(
            instruction="review the security changes in auth module",
            role="reviewer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "02_code_review"
        assert decision.primary.model_id == "claude-opus-4-6"

        provider, model = parse_route_model_id(decision.primary.model_id)
        assert provider == "claude"
        assert model == "opus"

        write_route_decision("dispatch-review-001", decision, state_dir=state_dir)
        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["chosen_route"]["model_id"] == "claude-opus-4-6"

    def test_debug_task_routes_to_deepseek(self, recommendations_yaml, state_dir, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        decision = decide(
            instruction="debug the failing test in subprocess_dispatch",
            role="debugger",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "05_debugging"
        assert decision.primary.model_id == "deepseek-v4-pro"

        provider, model = parse_route_model_id(decision.primary.model_id)
        assert provider == "litellm:deepseek:deepseek-v4-pro"
        assert model == "deepseek-v4-pro"

        write_route_decision("dispatch-debug-001", decision, state_dir=state_dir)
        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["chosen_route"]["model_id"] == "deepseek-v4-pro"


class TestProviderDispatchAutoRouteIntegration:
    """Integration test: provider_dispatch.main() with --auto-route writes NDJSON.

    PR-5 (2026-06-15) changed the contract: claude is not a provider-lane
    provider — the single-entry dispatch door owns all claude routing. Auto-route
    therefore dispatches provider-lane providers only (kimi, litellm, ...); an
    auto-route that decides a claude model is rejected at the door with EX_USAGE.
    These tests pin that contract — previously they seeded --provider claude and
    relied on the retired claude subprocess path (subprocess_dispatch.
    deliver_with_recovery).
    """

    def test_main_auto_route_writes_ndjson_for_kimi(self, recommendations_yaml, state_dir, monkeypatch):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setattr(
            "smart_router._RECOMMENDATIONS_PATH", recommendations_yaml
        )

        import smart_router as _sr  # noqa: PLC0415
        print(
            "VNX-DIAG pre-patch _RECOMMENDATIONS_PATH=",
            _sr._RECOMMENDATIONS_PATH,
            "| decide.__globals__ is smart_router.__dict__:",
            _sr.decide.__globals__ is _sr.__dict__,
            "| decide.__globals__[_RECOMMENDATIONS_PATH]:",
            _sr.decide.__globals__.get("_RECOMMENDATIONS_PATH"),
            file=sys.stderr,
        )
        _diag = _sr.decide(instruction="implement feature X")
        print(
            "VNX-DIAG decide(implement feature X) -> primary=",
            _diag.primary.model_id if _diag.primary else None,
            "| task_class=", _diag.task_class,
            "| constraints=", _diag.constraints_applied,
            file=sys.stderr,
        )

        captured_model = {}

        def _fake_dispatch(args, **kwargs):
            captured_model["model"] = args.model
            return 0

        with patch("provider_dispatch._dispatch_kimi", side_effect=_fake_dispatch):
            result = provider_dispatch.main([
                "--provider", "kimi",
                "--terminal-id", "T1",
                "--dispatch-id", "e2e-kimi-auto-route",
                "--instruction", "implement feature X",
                "--model", "sonnet",
                "--auto-route",
            ])

        assert result == 0
        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists(), "auto-route MUST produce route_decisions.ndjson entry"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "e2e-kimi-auto-route"
        assert record["task_class"] == "01_code_generation"
        # Cost-aware routing: kimi-k3 (explicit cost 0.02) beats claude-sonnet-4-6
        # (enriched ~0.045); the routed model must override the --model sonnet seed.
        assert record["chosen_route"]["model_id"] == "kimi-k3"
        assert captured_model["model"] == "kimi-k3"

    def test_main_auto_route_review_decides_claude_rejected_at_door(self, recommendations_yaml, state_dir, monkeypatch):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setattr(
            "smart_router._RECOMMENDATIONS_PATH", recommendations_yaml
        )

        with patch("provider_dispatch._dispatch_kimi", return_value=0), \
             patch("provider_dispatch._dispatch_litellm", return_value=0):
            result = provider_dispatch.main([
                "--provider", "kimi",
                "--terminal-id", "T1",
                "--dispatch-id", "e2e-review-route",
                "--instruction", "review the code changes for security",
                "--model", "sonnet",
                "--role", "reviewer",
                "--auto-route",
            ])

        # A review task auto-routes to claude-opus; claude is owned by the single-entry
        # door, so provider_dispatch must reject with EX_USAGE instead of dispatching.
        assert result == provider_dispatch._EX_USAGE

    def test_main_without_auto_route_no_ndjson(self, state_dir, monkeypatch):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

        with patch("provider_dispatch._dispatch_kimi", return_value=0):
            provider_dispatch.main([
                "--provider", "kimi",
                "--terminal-id", "T1",
                "--dispatch-id", "e2e-no-auto-route",
                "--instruction", "implement feature",
                "--model", "kimi-k3",
            ])

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert not ndjson_path.exists()

    def test_main_auto_route_fallback_on_missing_recommendations(self, tmp_path, monkeypatch):
        import provider_dispatch

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setattr(
            "smart_router._RECOMMENDATIONS_PATH",
            tmp_path / "nonexistent.yaml",
        )

        with patch("provider_dispatch._dispatch_kimi", return_value=0):
            result = provider_dispatch.main([
                "--provider", "kimi",
                "--terminal-id", "T1",
                "--dispatch-id", "e2e-fallback",
                "--instruction", "implement feature",
                "--model", "kimi-k3",
                "--auto-route",
            ])

        assert result == 0


class TestParseRouteModelIdAllProviders:
    """Verify parse_route_model_id returns correct (provider, model) for all routes."""

    def test_kimi_k2_returns_native_cli(self):
        provider, model = parse_route_model_id("kimi-k2-0905")
        assert provider == "kimi"
        assert model == "kimi-k2-0905"

    def test_kimi_k2_6_returns_native_cli(self):
        provider, model = parse_route_model_id("kimi-k2-6")
        assert provider == "kimi"
        assert model == "kimi-k2-6"

    def test_deepseek_returns_litellm_bridge(self):
        provider, model = parse_route_model_id("deepseek-v4-pro")
        assert provider.startswith("litellm:")
        assert "deepseek" in provider

    def test_glm_returns_litellm_zai(self):
        provider, model = parse_route_model_id("glm-5-1")
        assert provider == "litellm:zai"

    def test_claude_sonnet_returns_claude_provider(self):
        provider, model = parse_route_model_id("claude-sonnet-4-6")
        assert provider == "claude"
        assert model == "sonnet"
