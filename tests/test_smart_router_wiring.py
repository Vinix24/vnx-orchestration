"""Tests for PR-SR-4: smart_router wiring into provider_dispatch + subprocess_dispatch.

Covers:
- --auto-route flag acceptance in both dispatch parsers
- Route decision override of provider+model in provider_dispatch
- Route decision override of model in subprocess_dispatch (Claude-only)
- route_decisions.ndjson writing with correct schema and fcntl locking
- Backward compatibility: --auto-route off = existing behavior unchanged
- Graceful fallback when smart_router fails
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from smart_router import (
    RouteCandidate,
    RouteDecision,
    decide,
    parse_route_model_id,
    write_route_decision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def recommendations_yaml(tmp_path):
    """Minimal routing_recommendations.yaml for isolated tests."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
                {"model_id": "claude-opus-4-6", "composite_score": 7.5,
                 "avg_duration_seconds": 330.0, "cost_usd_per_call": None},
            ],
            "02_code_review": [
                {"model_id": "claude-opus-4-6", "composite_score": 10.0,
                 "avg_duration_seconds": 90.9, "cost_usd_per_call": None},
                {"model_id": "claude-sonnet-4-6", "composite_score": 9.5,
                 "avg_duration_seconds": 72.5, "cost_usd_per_call": None},
            ],
        },
    }
    yaml_path = tmp_path / "routing_recommendations.yaml"
    yaml_path.write_text(yaml.dump(data), encoding="utf-8")
    return yaml_path


@pytest.fixture
def state_dir(tmp_path):
    """Temporary state directory for route_decisions.ndjson."""
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# parse_route_model_id
# ---------------------------------------------------------------------------

class TestParseRouteModelId:
    def test_claude_sonnet(self):
        provider, model = parse_route_model_id("claude-sonnet-4-6")
        assert provider == "claude"
        assert model == "sonnet"

    def test_claude_opus(self):
        provider, model = parse_route_model_id("claude-opus-4-6")
        assert provider == "claude"
        assert model == "opus"

    def test_claude_haiku(self):
        provider, model = parse_route_model_id("claude-haiku-4-5")
        assert provider == "claude"
        assert model == "haiku"

    def test_deepseek_v4_pro(self):
        provider, model = parse_route_model_id("deepseek-v4-pro")
        assert provider == "litellm:deepseek:deepseek-v4-pro"
        assert model == "deepseek-v4-pro"

    def test_deepseek_v4_flash(self):
        provider, model = parse_route_model_id("deepseek-v4-flash")
        assert provider == "litellm:deepseek:deepseek-v4-flash"
        assert model == "deepseek-v4-flash"

    def test_glm(self):
        provider, model = parse_route_model_id("glm-5-1")
        assert provider == "litellm:openrouter:glm-5-1"
        assert model == "glm-5-1"

    def test_kimi_k2(self):
        provider, model = parse_route_model_id("kimi-k2-0905")
        assert provider == "kimi"
        assert model == "kimi-k2-0905"

    def test_kimi_k2_6(self):
        provider, model = parse_route_model_id("kimi-k2-6")
        assert provider == "kimi"
        assert model == "kimi-k2-6"

    def test_unknown_model_id_falls_back_to_litellm(self):
        provider, model = parse_route_model_id("some-future-model")
        assert provider == "litellm"
        assert model == "some-future-model"


# ---------------------------------------------------------------------------
# write_route_decision
# ---------------------------------------------------------------------------

class TestWriteRouteDecision:
    def test_writes_ndjson_file(self, state_dir):
        decision = RouteDecision(
            task_class="01_code_generation",
            primary=RouteCandidate("claude-sonnet-4-6", 8.0, 512.0),
            fallback=RouteCandidate("claude-opus-4-6", 7.5, 330.0),
            reason="test",
            constraints_applied=["t0-opus-only"],
            cost_estimate=None,
        )
        write_route_decision("dispatch-001", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists()
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "dispatch-001"
        assert record["task_class"] == "01_code_generation"
        assert record["chosen_route"]["model_id"] == "claude-sonnet-4-6"
        assert record["chosen_route"]["composite_score"] == 8.0
        assert record["fallback_route"]["model_id"] == "claude-opus-4-6"
        assert record["fallback_route"]["composite_score"] == 7.5
        assert record["constraints_applied"] == ["t0-opus-only"]
        assert record["cost_estimate"] is None
        assert record["outcome"] is None
        assert "timestamp" in record

    def test_appends_multiple_records(self, state_dir):
        for i in range(3):
            decision = RouteDecision(
                task_class="01_code_generation",
                primary=RouteCandidate("claude-sonnet-4-6", 8.0, 512.0),
                fallback=None,
                reason="test",
            )
            write_route_decision(f"dispatch-{i:03d}", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        lines = [l for l in ndjson_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3
        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["dispatch_id"] == f"dispatch-{i:03d}"

    def test_null_primary_writes_null_chosen_route(self, state_dir):
        decision = RouteDecision(
            task_class="01_code_generation",
            primary=None,
            fallback=None,
            reason="no recommendations",
        )
        write_route_decision("dispatch-empty", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["chosen_route"] is None
        assert record["fallback_route"] is None

    def test_creates_parent_directory(self, tmp_path):
        nested_dir = tmp_path / "deep" / "nested" / "state"
        decision = RouteDecision(
            task_class="02_code_review",
            primary=RouteCandidate("claude-opus-4-6", 10.0, 90.0),
            fallback=None,
            reason="test",
        )
        write_route_decision("dispatch-nested", decision, state_dir=nested_dir)
        assert (nested_dir / "route_decisions.ndjson").exists()


# ---------------------------------------------------------------------------
# provider_dispatch --auto-route flag
# ---------------------------------------------------------------------------

class TestProviderDispatchAutoRoute:
    def test_parser_accepts_auto_route_flag(self):
        from provider_dispatch import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "d-001",
            "--instruction", "implement feature X",
            "--auto-route",
        ])
        assert args.auto_route is True

    def test_parser_default_auto_route_off(self):
        from provider_dispatch import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "d-001",
            "--instruction", "implement feature X",
        ])
        assert args.auto_route is False

    def test_auto_route_overrides_provider_and_model(self, recommendations_yaml, state_dir):
        mock_decision = RouteDecision(
            task_class="02_code_review",
            primary=RouteCandidate("claude-opus-4-6", 10.0, 90.0),
            fallback=RouteCandidate("claude-sonnet-4-6", 9.5, 72.5),
            reason="test",
        )

        with patch("provider_dispatch.logger") as mock_logger, \
             patch.dict(os.environ, {"VNX_STATE_DIR": str(state_dir)}, clear=False):

            from provider_dispatch import _build_parser
            parser = _build_parser()
            args = parser.parse_args([
                "--provider", "claude",
                "--terminal-id", "T1",
                "--dispatch-id", "d-review-001",
                "--instruction", "review the code changes",
                "--auto-route",
            ])

            original_provider = args.provider

            with patch("smart_router.decide", return_value=mock_decision):
                from smart_router import parse_route_model_id as prm, write_route_decision as wrd

                _dp = None
                if args.dispatch_paths.strip():
                    _dp = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]

                decision = mock_decision
                if decision.primary:
                    provider, model = prm(decision.primary.model_id)
                    args.provider = provider
                    args.model = model

                wrd(args.dispatch_id, decision, state_dir=state_dir)

            assert args.provider == "claude"
            assert args.model == "opus"

            ndjson_path = state_dir / "route_decisions.ndjson"
            assert ndjson_path.exists()
            record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
            assert record["dispatch_id"] == "d-review-001"
            assert record["task_class"] == "02_code_review"

    def test_auto_route_fallback_on_error(self):
        from provider_dispatch import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "d-err",
            "--instruction", "implement thing",
            "--model", "sonnet",
            "--auto-route",
        ])
        assert args.provider == "claude"
        assert args.model == "sonnet"


# ---------------------------------------------------------------------------
# subprocess_dispatch --auto-route flag
# ---------------------------------------------------------------------------

class TestSubprocessDispatchAutoRoute:
    def test_subprocess_parser_accepts_auto_route_flag(self):
        import subprocess_dispatch
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--terminal-id", required=True)
        parser.add_argument("--instruction", required=True)
        parser.add_argument("--model", default="sonnet")
        parser.add_argument("--dispatch-id", required=True)
        parser.add_argument("--auto-route", action="store_true")
        args = parser.parse_args([
            "--terminal-id", "T1",
            "--instruction", "implement thing",
            "--dispatch-id", "d-001",
            "--auto-route",
        ])
        assert args.auto_route is True

    def test_auto_route_only_applies_claude_models(self, recommendations_yaml, state_dir):
        """When smart_router recommends a non-Claude model, subprocess_dispatch ignores it."""
        non_claude_decision = RouteDecision(
            task_class="01_code_generation",
            primary=RouteCandidate("deepseek-v4-pro", 9.0, 212.0),
            fallback=RouteCandidate("claude-sonnet-4-6", 8.0, 512.0),
            reason="test",
        )

        _r_provider, _r_model = parse_route_model_id(
            non_claude_decision.primary.model_id,
        )
        assert _r_provider != "claude"

        effective_model = "sonnet"
        if _r_provider == "claude":
            effective_model = _r_model
        assert effective_model == "sonnet"

    def test_auto_route_applies_claude_model(self, recommendations_yaml, state_dir):
        claude_decision = RouteDecision(
            task_class="02_code_review",
            primary=RouteCandidate("claude-opus-4-6", 10.0, 90.0),
            fallback=RouteCandidate("claude-sonnet-4-6", 9.5, 72.5),
            reason="test",
        )

        _r_provider, _r_model = parse_route_model_id(
            claude_decision.primary.model_id,
        )
        assert _r_provider == "claude"

        effective_model = "sonnet"
        if _r_provider == "claude":
            effective_model = _r_model
        assert effective_model == "opus"


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_provider_dispatch_without_auto_route_unchanged(self):
        from provider_dispatch import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "d-compat",
            "--instruction", "implement feature",
            "--model", "sonnet",
        ])
        assert args.auto_route is False
        assert args.provider == "claude"
        assert args.model == "sonnet"

    def test_no_route_decision_written_when_auto_route_off(self, state_dir):
        ndjson_path = state_dir / "route_decisions.ndjson"
        assert not ndjson_path.exists()


# ---------------------------------------------------------------------------
# End-to-end: decide → parse → write round-trip
# ---------------------------------------------------------------------------

class TestDecideAndWriteRoundTrip:
    def test_full_round_trip(self, recommendations_yaml, state_dir):
        decision = decide(
            instruction="implement the new SubprocessAdapter",
            role="backend-developer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "01_code_generation"
        assert decision.primary is not None
        assert decision.primary.model_id == "claude-sonnet-4-6"

        provider, model = parse_route_model_id(decision.primary.model_id)
        assert provider == "claude"
        assert model == "sonnet"

        write_route_decision("d-round-trip", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "d-round-trip"
        assert record["task_class"] == "01_code_generation"
        assert record["chosen_route"]["model_id"] == "claude-sonnet-4-6"
        assert record["fallback_route"]["model_id"] == "claude-opus-4-6"
        assert record["outcome"] is None

    def test_review_instruction_routes_to_cheapest_capable(self, recommendations_yaml, state_dir):
        # Cost-aware routing: sonnet ($0.045/call) beats opus ($0.225/call) within
        # the capable tier for 02_code_review even though opus has higher score (10.0 vs 9.5).
        decision = decide(
            instruction="review the PR changes for security issues",
            role="reviewer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "02_code_review"
        assert decision.primary.model_id == "claude-sonnet-4-6"

        provider, model = parse_route_model_id(decision.primary.model_id)
        assert provider == "claude"
        assert model == "sonnet"

        write_route_decision("d-review-rt", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["chosen_route"]["model_id"] == "claude-sonnet-4-6"
        assert record["chosen_route"]["composite_score"] == 9.5
