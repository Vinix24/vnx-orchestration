#!/usr/bin/env python3
"""tests/test_subprocess_cheap_lane.py — Cheap-lane delegation in subprocess_dispatch.

Proves that when routing_policy decides a non-Claude (cheap) lane:
  1. _select_dispatch_path returns (lane, current_model) — NOT (None, claude_fallback).
  2. _build_cheap_lane_argv constructs correct provider_dispatch argv.
  3. _execute_cheap_lane_dispatch calls provider_dispatch.main() and NEVER
     calls deliver_with_recovery (the Claude path).

The critical regression: the OLD code fell back to the first Claude model in
fallback_chain when the lane was non-Claude.  These tests assert that behaviour
is GONE: cheap_lane_provider is the non-Claude lane string, not None, and
the actual dispatch delegates to provider_dispatch — not Claude.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import (
    _ROLE_FALLBACK,
    _build_cheap_lane_argv,
    _execute_cheap_lane_dispatch,
    _select_dispatch_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(**kwargs: str) -> dict:
    return dict(kwargs)


def _enabled(**extra: str) -> dict:
    return {"VNX_ROUTING_POLICY_ENABLED": "1", **extra}


# ---------------------------------------------------------------------------
# Routing disabled — guards short-circuit before decide_lane is called
# ---------------------------------------------------------------------------


class TestSelectDispatchPathDisabled:
    def test_no_env_var_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_env(),  # VNX_ROUTING_POLICY_ENABLED absent
        )
        assert cheap is None
        assert model == "sonnet"

    def test_env_var_zero_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_env(VNX_ROUTING_POLICY_ENABLED="0"),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_empty_task_class_returns_defaults(self):
        cheap, model = _select_dispatch_path(
            task_class="",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_auto_route_applied_skips_routing(self):
        """When smart_router already ran, routing_policy must not override it."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="haiku",
            auto_route_applied=True,
            env=_enabled(),
        )
        assert cheap is None
        assert model == "haiku"  # unchanged — smart_router decision preserved


# ---------------------------------------------------------------------------
# Claude lanes — cheap_lane_provider is None; effective_model is updated
# ---------------------------------------------------------------------------


class TestSelectDispatchPathClaudeLane:
    def test_claude_sonnet_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/sonnet-4-6",
            rule_name="refactor-code-default",
            rationale="test",
            fallback_chain=[],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="medium",
                current_model="opus",  # should be overridden
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"

    def test_claude_haiku_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/haiku-4-5",
            rule_name="simple-cleanup",
            rationale="test",
            fallback_chain=["claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="lint-narrow",
                complexity="low",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "haiku"

    def test_claude_opus_lane_updates_model(self):
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="claude/opus",
            rule_name="research-deep",
            rationale="test",
            fallback_chain=[],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="research",
                complexity="high",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "opus"


# ---------------------------------------------------------------------------
# Non-Claude (cheap) lanes — THE critical regression guard
# ---------------------------------------------------------------------------


class TestSelectDispatchPathCheapLane:
    """Non-Claude lanes must return (lane, current_model), never (None, claude_fallback).

    The OLD code found the first Claude model in fallback_chain and set _effective_model
    to it, silently routing the dispatch through Claude instead of the chosen provider.
    These tests prove that behaviour is eliminated.
    """

    def test_litellm_moonshot_lane_not_claude_fallback(self):
        """code-review → kimi/moonshot: cheap_lane_provider set, Claude NOT used."""
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="litellm:moonshot:kimi-k2-0905-default",
            rule_name="review-analysis",
            rationale="test",
            # fallback_chain contains Claude — old code would have used this as _effective_model!
            fallback_chain=["litellm:deepseek:deepseek-v4-pro", "claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="code-review",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )

        # cheap_lane_provider MUST be the lane — not None (old fallback was None + Claude model)
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        # effective_model is unchanged — provider_dispatch resolves its own model
        assert model == "sonnet"

    def test_litellm_deepseek_lane_not_claude_fallback(self):
        """DeepSeek lane: cheap_lane_provider set, Claude model NOT used."""
        from routing_policy import RoutingDecision

        decision = RoutingDecision(
            lane="litellm:deepseek:deepseek-v4-pro",
            rule_name="cost-optimized-code",
            rationale="test",
            fallback_chain=["litellm:moonshot:kimi-k2-0905-default", "claude/sonnet-4-6"],
        )
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )

        assert cheap == "litellm:deepseek:deepseek-v4-pro"
        assert model == "sonnet"  # NOT overridden to a Claude model

    def test_current_model_preserved_unchanged_for_various_inputs(self):
        """Whatever current_model is, it passes through unmodified when lane is cheap."""
        from routing_policy import RoutingDecision

        for original_model in ("sonnet", "opus", "haiku", "claude-sonnet-4-6"):
            decision = RoutingDecision(
                lane="litellm:moonshot:kimi-k2-0905-default",
                rule_name="review-analysis",
                rationale="test",
                fallback_chain=["claude/sonnet-4-6"],
            )
            with patch("routing_policy.decide_lane", return_value=decision):
                cheap, model = _select_dispatch_path(
                    task_class="code-review",
                    complexity="medium",
                    current_model=original_model,
                    env=_enabled(),
                )
            assert cheap is not None, f"cheap_lane_provider unset for current_model={original_model!r}"
            assert model == original_model, (
                f"current_model={original_model!r} was unexpectedly changed to {model!r}"
            )

    def test_cheap_lane_provider_is_full_lane_string(self):
        """cheap_lane_provider equals the full lane string, ready for --provider arg."""
        from routing_policy import RoutingDecision

        lane = "litellm:moonshot:kimi-k2-0905-default"
        decision = RoutingDecision(lane=lane, rule_name="r", rationale="t", fallback_chain=[])
        with patch("routing_policy.decide_lane", return_value=decision):
            cheap, _ = _select_dispatch_path(
                task_class="analysis",
                complexity="low",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap == lane  # passed verbatim to provider_dispatch --provider


# ---------------------------------------------------------------------------
# Error handling — any failure falls back to (None, current_model)
# ---------------------------------------------------------------------------


class TestSelectDispatchPathErrors:
    def test_decide_lane_file_not_found_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=FileNotFoundError("no policy")):
            cheap, model = _select_dispatch_path(
                task_class="code-review",
                complexity="medium",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"

    def test_decide_lane_value_error_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=ValueError("bad yaml")):
            cheap, model = _select_dispatch_path(
                task_class="refactor",
                complexity="high",
                current_model="opus",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "opus"

    def test_decide_lane_runtime_error_falls_back(self):
        with patch("routing_policy.decide_lane", side_effect=RuntimeError("unexpected")):
            cheap, model = _select_dispatch_path(
                task_class="research",
                complexity="high",
                current_model="sonnet",
                env=_enabled(),
            )
        assert cheap is None
        assert model == "sonnet"


# ---------------------------------------------------------------------------
# Production policy smoke tests — against the real routing_policy.yaml
# ---------------------------------------------------------------------------


class TestSelectDispatchPathProductionPolicy:
    """Exercises the real routing_policy.yaml to catch yaml drift."""

    def test_code_review_routes_to_cheap_moonshot_lane(self):
        """code-review → litellm:moonshot (cheap); must NOT produce Claude."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        assert model == "sonnet"  # unchanged

    def test_refactor_medium_routes_to_claude_sonnet(self):
        """refactor + medium → claude/sonnet-4-6 (cheap is None, model overridden)."""
        cheap, model = _select_dispatch_path(
            task_class="refactor",
            complexity="medium",
            current_model="opus",  # should be overridden
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"

    def test_lint_narrow_low_routes_to_claude_haiku(self):
        """lint-narrow + low → claude/haiku-4-5 (cheap is None)."""
        cheap, model = _select_dispatch_path(
            task_class="lint-narrow",
            complexity="low",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "haiku"

    def test_analysis_routes_to_cheap_moonshot_lane(self):
        """analysis → litellm:moonshot (cheap); must NOT produce Claude."""
        cheap, model = _select_dispatch_path(
            task_class="analysis",
            complexity="medium",
            current_model="sonnet",
            env=_enabled(),
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        assert model == "sonnet"

    def test_unknown_task_class_returns_claude_default(self):
        """Unknown task_class hits default lane (claude/sonnet-4-6)."""
        cheap, model = _select_dispatch_path(
            task_class="unknown-task-xyz",
            complexity="medium",
            current_model="haiku",
            env=_enabled(),
        )
        assert cheap is None
        assert model == "sonnet"  # default_lane mapped to sonnet


# ---------------------------------------------------------------------------
# _build_cheap_lane_argv — argv construction for provider_dispatch delegation
# ---------------------------------------------------------------------------


def _make_args(**kwargs: object) -> argparse.Namespace:
    """Return a minimal Namespace suitable for _build_cheap_lane_argv / _execute_cheap_lane_dispatch."""
    defaults: dict = dict(
        terminal_id="T1",
        instruction="do the thing",
        model="sonnet",
        dispatch_id="d-test-001",
        role="backend-developer",
        max_retries=3,
        gate="g42",
        no_auto_commit=False,
        dispatch_paths="",
        pr_id=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestBuildCheapLaneArgv:
    """_build_cheap_lane_argv constructs the correct provider_dispatch argv."""

    def test_first_two_args_are_provider_and_lane(self):
        """--provider must be argv[0] so provider_dispatch parse() sees it first."""
        lane = "litellm:moonshot:kimi-k2-0905-default"
        argv = _build_cheap_lane_argv(_make_args(), lane)
        assert argv[0] == "--provider"
        assert argv[1] == lane

    def test_required_fields_present(self):
        lane = "litellm:moonshot:kimi-k2-0905-default"
        argv = _build_cheap_lane_argv(_make_args(), lane)
        for flag, value in (
            ("--provider", lane),
            ("--terminal-id", "T1"),
            ("--dispatch-id", "d-test-001"),
            ("--model", "sonnet"),
            ("--role", "backend-developer"),
            ("--max-retries", "3"),
            ("--gate", "g42"),
        ):
            assert flag in argv, f"flag {flag!r} missing"
            idx = argv.index(flag)
            assert argv[idx + 1] == value, f"{flag} value mismatch: expected {value!r}, got {argv[idx+1]!r}"

    def test_no_auto_commit_appended_when_true(self):
        argv = _build_cheap_lane_argv(
            _make_args(no_auto_commit=True),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--no-auto-commit" in argv

    def test_no_auto_commit_absent_when_false(self):
        argv = _build_cheap_lane_argv(
            _make_args(no_auto_commit=False),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--no-auto-commit" not in argv

    def test_dispatch_paths_forwarded(self):
        argv = _build_cheap_lane_argv(
            _make_args(dispatch_paths="scripts/,tests/"),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--dispatch-paths" in argv
        idx = argv.index("--dispatch-paths")
        assert argv[idx + 1] == "scripts/,tests/"

    def test_dispatch_paths_absent_when_empty(self):
        argv = _build_cheap_lane_argv(
            _make_args(dispatch_paths=""),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--dispatch-paths" not in argv

    def test_pr_id_forwarded_when_set(self):
        argv = _build_cheap_lane_argv(
            _make_args(pr_id="PR-644"),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--pr-id" in argv
        idx = argv.index("--pr-id")
        assert argv[idx + 1] == "PR-644"

    def test_pr_id_absent_when_none(self):
        argv = _build_cheap_lane_argv(
            _make_args(pr_id=None),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        assert "--pr-id" not in argv

    def test_role_fallback_when_role_is_none(self):
        argv = _build_cheap_lane_argv(
            _make_args(role=None),
            "litellm:deepseek:deepseek-v4-pro",
        )
        idx = argv.index("--role")
        assert argv[idx + 1] == _ROLE_FALLBACK

    def test_instruction_forwarded_verbatim(self):
        instruction = "refactor the payment module\nwith multi-line content"
        argv = _build_cheap_lane_argv(
            _make_args(instruction=instruction),
            "litellm:moonshot:kimi-k2-0905-default",
        )
        idx = argv.index("--instruction")
        assert argv[idx + 1] == instruction

    def test_lane_string_is_forwarded_as_provider_value(self):
        """The full lane string (litellm:sub:model) is passed verbatim to --provider."""
        for lane in (
            "litellm:moonshot:kimi-k2-0905-default",
            "litellm:deepseek:deepseek-v4-pro",
            "litellm:zai:glm-5.1-default",
            "kimi",
        ):
            argv = _build_cheap_lane_argv(_make_args(), lane)
            assert argv[1] == lane, f"lane {lane!r} not forwarded verbatim"


# ---------------------------------------------------------------------------
# _execute_cheap_lane_dispatch — THE critical regression guard
# ---------------------------------------------------------------------------


class TestExecuteCheapLaneDispatch:
    """Proves that delegation to provider_dispatch.main() is correct and that
    deliver_with_recovery (the Claude path) is NEVER invoked on a cheap lane.

    Regression scenario: old __main__ block fell through to deliver_with_recovery
    after logging the cheap lane, silently routing via Claude instead of the
    intended provider.  These tests assert that regression is eliminated.
    """

    def test_provider_dispatch_main_called(self):
        """_execute_cheap_lane_dispatch calls provider_dispatch.main()."""
        import provider_dispatch as pd_mod

        received_argv: list[list[str]] = []

        def mock_pd_main(argv: list[str]) -> int:
            received_argv.append(list(argv))
            return 0

        with patch.object(pd_mod, "main", mock_pd_main):
            rc = _execute_cheap_lane_dispatch(
                _make_args(),
                "litellm:moonshot:kimi-k2-0905-default",
            )

        assert rc == 0
        assert len(received_argv) == 1
        assert "--provider" in received_argv[0]
        assert "litellm:moonshot:kimi-k2-0905-default" in received_argv[0]

    def test_exit_code_propagated_from_provider_dispatch(self):
        """Return value from provider_dispatch.main() is passed back as-is."""
        import provider_dispatch as pd_mod

        for expected in (0, 1, 2):
            with patch.object(pd_mod, "main", return_value=expected):
                rc = _execute_cheap_lane_dispatch(
                    _make_args(),
                    "litellm:moonshot:kimi-k2-0905-default",
                )
            assert rc == expected, f"Expected exit code {expected}, got {rc}"

    def test_deliver_with_recovery_not_called_for_cheap_lane(self):
        """deliver_with_recovery (Claude) must NOT be called on a cheap lane.

        This is the primary regression guard.  Any code path that invokes
        deliver_with_recovery on a non-Claude lane silently routes through
        Claude instead of the chosen provider.
        """
        import provider_dispatch as pd_mod
        from subprocess_dispatch_internals import recovery as recovery_mod

        dwr_calls: list = []

        def mock_dwr(*args: object, **kwargs: object) -> bool:
            dwr_calls.append((args, kwargs))
            return True

        with (
            patch.object(pd_mod, "main", return_value=0),
            patch.object(recovery_mod, "deliver_with_recovery", mock_dwr),
        ):
            _execute_cheap_lane_dispatch(
                _make_args(),
                "litellm:moonshot:kimi-k2-0905-default",
            )

        assert dwr_calls == [], (
            f"deliver_with_recovery (Claude) was called {len(dwr_calls)} time(s) for a "
            "cheap lane dispatch — this is the Claude fallback regression."
        )

    def test_provider_argv_contains_correct_terminal_and_dispatch_ids(self):
        """Forwarded argv must carry the original terminal_id and dispatch_id."""
        import provider_dispatch as pd_mod

        received: list[list[str]] = []

        with patch.object(pd_mod, "main", lambda argv: received.append(list(argv)) or 0):
            _execute_cheap_lane_dispatch(
                _make_args(terminal_id="T2", dispatch_id="d-cl2-9999"),
                "litellm:deepseek:deepseek-v4-pro",
            )

        argv = received[0]
        tid_idx = argv.index("--terminal-id")
        did_idx = argv.index("--dispatch-id")
        assert argv[tid_idx + 1] == "T2"
        assert argv[did_idx + 1] == "d-cl2-9999"

    def test_cheap_lane_never_falls_back_to_claude_end_to_end(self):
        """End-to-end: routing decision + delegation — Claude is never invoked.

        Simulates the full flow as __main__ executes it:
          1. _select_dispatch_path returns a non-Claude lane
             (with Claude in fallback_chain — the old code used that chain).
          2. _execute_cheap_lane_dispatch delegates to provider_dispatch.main().
          3. deliver_with_recovery is never reached.

        This is the definitive proof that the cheap-lane regression is fixed.
        """
        from routing_policy import RoutingDecision
        import provider_dispatch as pd_mod
        from subprocess_dispatch_internals import recovery as recovery_mod

        # Lane with Claude in fallback_chain — old code would have resolved this
        cheap_decision = RoutingDecision(
            lane="litellm:moonshot:kimi-k2-0905-default",
            rule_name="code-review-cheap",
            rationale="test: route to moonshot",
            fallback_chain=["litellm:deepseek:deepseek-v4-pro", "claude/sonnet-4-6"],
        )

        dwr_calls: list = []
        pd_calls: list[list[str]] = []

        with patch("routing_policy.decide_lane", return_value=cheap_decision):
            cheap_provider, effective_model = _select_dispatch_path(
                task_class="code-review",
                complexity="medium",
                current_model="sonnet",
                env={"VNX_ROUTING_POLICY_ENABLED": "1"},
            )

        # Routing phase: non-Claude provider selected, current_model unchanged
        assert cheap_provider == "litellm:moonshot:kimi-k2-0905-default"
        assert effective_model == "sonnet"

        # Dispatch phase: delegate to provider_dispatch — Claude must not run
        with (
            patch.object(pd_mod, "main", lambda argv: pd_calls.append(list(argv)) or 0),
            patch.object(recovery_mod, "deliver_with_recovery", lambda *a, **kw: dwr_calls.append(1) or True),
        ):
            rc = _execute_cheap_lane_dispatch(
                _make_args(instruction="review the code", dispatch_id="d-e2e-cl2"),
                cheap_provider,
            )

        # provider_dispatch.main() was called with the correct --provider
        assert len(pd_calls) == 1, f"provider_dispatch.main called {len(pd_calls)} times, expected 1"
        pd_argv = pd_calls[0]
        assert "--provider" in pd_argv
        assert "litellm:moonshot:kimi-k2-0905-default" in pd_argv

        # deliver_with_recovery (Claude) was never invoked
        assert dwr_calls == [], (
            "deliver_with_recovery (Claude) was invoked for a non-Claude lane — "
            "this is the cheap-lane regression that must not exist."
        )

        assert rc == 0
