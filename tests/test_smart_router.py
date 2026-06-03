"""Tests for smart_router — task classifier + recommendation lookup.

Covers all 7 task classes from routing_recommendations.yaml, role-based fallback,
ambiguous inputs, missing recommendations, and the full decide() flow.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from smart_router import (
    RouteCandidate,
    RouteDecision,
    classify_task,
    decide,
    recommend,
    _compute_quality_tier,
    _cost_aware_sort_key,
    _load_recommendations,
    TASK_CLASSES,
    ROLE_TO_TASK_CLASS,
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
            "03_refactoring": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.5,
                 "avg_duration_seconds": 209.0, "cost_usd_per_call": None},
            ],
            "04_documentation": [
                {"model_id": "deepseek-v4-flash", "composite_score": 8.5,
                 "avg_duration_seconds": 12.6, "cost_usd_per_call": None},
            ],
            "05_debugging": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 7.5,
                 "avg_duration_seconds": 148.8, "cost_usd_per_call": None},
            ],
            "06_design": [
                {"model_id": "claude-haiku-4-5", "composite_score": 9.5,
                 "avg_duration_seconds": 151.9, "cost_usd_per_call": None},
                {"model_id": "claude-opus-4-6", "composite_score": 9.0,
                 "avg_duration_seconds": 273.3, "cost_usd_per_call": None},
            ],
            "07_translation": [
                {"model_id": "deepseek-v4-flash", "composite_score": 8.5,
                 "avg_duration_seconds": 4.25, "cost_usd_per_call": None},
            ],
        }
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


@pytest.fixture
def empty_recommendations_yaml(tmp_path):
    """YAML with routing_by_task but no entries for any class."""
    data = {"routing_by_task": {}}
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# classify_task — 7 task classes
# ---------------------------------------------------------------------------

class TestClassifyCodeGeneration:

    @pytest.mark.parametrize("instruction", [
        "Implement the smart router module",
        "Create new endpoint for user registration",
        "Add support for WebSocket connections",
        "Build the migration script",
        "Scaffold the provider adapter",
        "Generate code for the CLI parser",
        "Write a module for cost tracking",
    ])
    def test_code_generation_instructions(self, instruction):
        assert classify_task(instruction) == "01_code_generation"


class TestClassifyCodeReview:

    @pytest.mark.parametrize("instruction", [
        "Review the PR for security issues",
        "Audit the authentication module",
        "Run a code review on the dispatch logic",
        "Check code quality of the router",
        "Perform static analysis on the adapter",
        "Gate check: lint + type-check before merge",
    ])
    def test_code_review_instructions(self, instruction):
        assert classify_task(instruction) == "02_code_review"


class TestClassifyRefactoring:

    @pytest.mark.parametrize("instruction", [
        "Refactor the dispatch router into smaller functions",
        "Split module intelligence_selector.py per source",
        "Extract function from the monolithic handler",
        "Rename the legacy adapter class",
        "Consolidate duplicate error handlers",
        "Clean up the dead code in cost_tracker",
    ])
    def test_refactoring_instructions(self, instruction):
        assert classify_task(instruction) == "03_refactoring"


class TestClassifyDocumentation:

    @pytest.mark.parametrize("instruction", [
        "Document the API endpoints",
        "Write docs for the new router module",
        "Update the README with installation instructions",
        "Add docstrings to the provider registry",
        "Write an ADR for the routing decision",
        "Update changelog for v0.6.0",
    ])
    def test_documentation_instructions(self, instruction):
        assert classify_task(instruction) == "04_documentation"


class TestClassifyDebugging:

    @pytest.mark.parametrize("instruction", [
        "Debug the failing gate check",
        "Fix bug in the cost tracker parsing",
        "Diagnose the flaky test in CI",
        "Troubleshoot the broken WebSocket connection",
        "Investigate the regression in dispatch timing",
        "Root cause analysis for the NDJSON corruption",
    ])
    def test_debugging_instructions(self, instruction):
        assert classify_task(instruction) == "05_debugging"


class TestClassifyDesign:

    @pytest.mark.parametrize("instruction", [
        "Design the new routing architecture",
        "Plan the migration from tmux to headless",
        "Write an RFC for the feedback loop system",
        "Create a technical spec for the cost router",
        "System design for multi-tenant dispatch",
        "API design for the external webhook integration",
    ])
    def test_design_instructions(self, instruction):
        assert classify_task(instruction) == "06_design"


class TestClassifyTranslation:

    @pytest.mark.parametrize("instruction", [
        "Translate the UI strings to Dutch",
        "Add i18n support for error messages",
        "Localize the dashboard for German users",
        "Port to Python from the existing TypeScript module",
        "Convert to YAML from the JSON config",
    ])
    def test_translation_instructions(self, instruction):
        assert classify_task(instruction) == "07_translation"


# ---------------------------------------------------------------------------
# classify_task — role fallback
# ---------------------------------------------------------------------------

class TestClassifyRoleFallback:

    def test_role_backend_developer_falls_back_to_code_gen(self):
        assert classify_task("do the thing", role="backend-developer") == "01_code_generation"

    def test_role_reviewer_falls_back_to_code_review(self):
        assert classify_task("check this", role="reviewer") == "02_code_review"

    def test_role_architect_falls_back_to_design(self):
        assert classify_task("think about this", role="architect") == "06_design"

    def test_role_debugger_falls_back_to_debugging(self):
        assert classify_task("look at the logs", role="debugger") == "05_debugging"

    def test_role_technical_writer_falls_back_to_documentation(self):
        assert classify_task("handle the docs", role="technical-writer") == "04_documentation"

    def test_instruction_takes_priority_over_role(self):
        assert classify_task("Refactor the module", role="backend-developer") == "03_refactoring"


# ---------------------------------------------------------------------------
# classify_task — edge cases
# ---------------------------------------------------------------------------

class TestClassifyEdgeCases:

    def test_empty_instruction_no_role_defaults_to_code_gen(self):
        assert classify_task("") == "01_code_generation"

    def test_none_instruction_defaults_to_code_gen(self):
        assert classify_task(None) == "01_code_generation"

    def test_ambiguous_instruction_uses_first_match(self):
        result = classify_task("Implement and review the new adapter")
        assert result == "01_code_generation"

    def test_unknown_role_defaults_to_code_gen(self):
        assert classify_task("do something", role="underwater-welder") == "01_code_generation"

    def test_role_with_leading_slash(self):
        assert classify_task("do the thing", role="/backend-developer") == "01_code_generation"

    def test_role_case_insensitive(self):
        assert classify_task("do the thing", role="Backend-Developer") == "01_code_generation"

    def test_dispatch_paths_accepted_but_unused(self):
        result = classify_task("do the thing", dispatch_paths=["scripts/lib/foo.py"])
        assert result == "01_code_generation"


# ---------------------------------------------------------------------------
# recommend
# ---------------------------------------------------------------------------

class TestRecommend:

    def test_returns_candidates_sorted_by_score(self, recommendations_yaml):
        candidates = recommend("01_code_generation", recommendations_path=recommendations_yaml)
        assert len(candidates) == 2
        assert candidates[0].composite_score >= candidates[1].composite_score

    def test_candidates_are_route_candidate_type(self, recommendations_yaml):
        candidates = recommend("02_code_review", recommendations_path=recommendations_yaml)
        assert all(isinstance(c, RouteCandidate) for c in candidates)

    def test_returns_empty_for_unknown_task_class(self, recommendations_yaml):
        candidates = recommend("99_nonexistent", recommendations_path=recommendations_yaml)
        assert candidates == []

    def test_returns_empty_for_empty_yaml(self, empty_recommendations_yaml):
        candidates = recommend("01_code_generation", recommendations_path=empty_recommendations_yaml)
        assert candidates == []

    def test_all_7_task_classes_have_recommendations(self):
        """Verify the real routing_recommendations.yaml covers all 7 classes."""
        recs = _load_recommendations()
        for tc in TASK_CLASSES:
            assert tc in recs, f"Missing recommendations for {tc}"
            assert len(recs[tc]) > 0, f"Empty recommendations for {tc}"

    def test_raises_on_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            recommend("01_code_generation", recommendations_path=missing)

    def test_raises_on_malformed_yaml(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not_routing_by_task: true", encoding="utf-8")
        with pytest.raises(ValueError, match="routing_by_task"):
            recommend("01_code_generation", recommendations_path=bad)


# ---------------------------------------------------------------------------
# decide (full flow)
# ---------------------------------------------------------------------------

class TestDecide:

    def test_returns_route_decision(self, recommendations_yaml):
        decision = decide(
            "Implement the cost tracker",
            recommendations_path=recommendations_yaml,
        )
        assert isinstance(decision, RouteDecision)
        assert decision.task_class == "01_code_generation"
        assert decision.primary is not None
        assert decision.primary.model_id == "claude-sonnet-4-6"
        assert decision.fallback is not None
        assert decision.fallback.model_id == "claude-opus-4-6"

    def test_decision_with_single_candidate(self, recommendations_yaml):
        decision = decide(
            "Refactor the handler into three modules",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "03_refactoring"
        assert decision.primary is not None
        assert decision.fallback is None

    def test_decision_for_design_via_role(self, recommendations_yaml):
        decision = decide(
            "think about the system",
            role="architect",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "06_design"
        assert decision.primary.model_id == "claude-haiku-4-5"

    def test_decision_with_no_recommendations(self, empty_recommendations_yaml):
        decision = decide(
            "Debug the broken gate",
            recommendations_path=empty_recommendations_yaml,
        )
        assert decision.task_class == "05_debugging"
        assert decision.primary is None
        assert decision.fallback is None
        assert "no recommendations" in decision.reason

    def test_reason_contains_task_class(self, recommendations_yaml):
        decision = decide(
            "Review the security audit results",
            recommendations_path=recommendations_yaml,
        )
        assert "02_code_review" in decision.reason

    def test_dispatch_paths_forwarded(self, recommendations_yaml):
        decision = decide(
            "update the tests",
            dispatch_paths=["tests/"],
            recommendations_path=recommendations_yaml,
        )
        assert isinstance(decision, RouteDecision)


# ---------------------------------------------------------------------------
# RouteCandidate / RouteDecision dataclass sanity
# ---------------------------------------------------------------------------

class TestDataclasses:

    def test_route_candidate_fields(self):
        c = RouteCandidate(
            model_id="test-model",
            composite_score=8.5,
            avg_duration_seconds=100.0,
            cost_usd_per_call=0.05,
        )
        assert c.model_id == "test-model"
        assert c.composite_score == 8.5
        assert c.avg_duration_seconds == 100.0
        assert c.cost_usd_per_call == 0.05

    def test_route_candidate_cost_defaults_to_none(self):
        c = RouteCandidate(model_id="m", composite_score=1.0, avg_duration_seconds=1.0)
        assert c.cost_usd_per_call is None

    def test_route_decision_constraints_defaults_to_empty(self):
        d = RouteDecision(
            task_class="01_code_generation",
            primary=None,
            fallback=None,
            reason="test",
        )
        assert d.constraints_applied == []
        assert d.cost_estimate is None


# ---------------------------------------------------------------------------
# _cost_aware_sort_key — null-cost handling (PR-SR-FIX-1)
# ---------------------------------------------------------------------------

class TestNullCostSortKey:

    def _make(self, score: float, cost=None, cost_tier=None) -> RouteCandidate:
        return RouteCandidate(
            model_id=f"model-score-{score}",
            composite_score=score,
            avg_duration_seconds=10.0,
            cost_usd_per_call=cost,
            cost_tier=cost_tier,
        )

    def test_null_cost_high_score_wins_over_measured_cost_lower_score(self):
        """A null-cost candidate with higher score beats a cost-bearing lower-score one.

        Before the fix, null-cost sorted as float('inf'), so any measured-cost
        candidate would win even if its score was much lower.
        """
        high_score_null = self._make(score=9.5, cost=None)
        low_score_cheap = self._make(score=5.5, cost=0.00126)
        assert _cost_aware_sort_key(high_score_null) < _cost_aware_sort_key(low_score_cheap)

    def test_equal_score_lower_cost_wins(self):
        """When two capable candidates have the same score, lower cost wins as tiebreaker."""
        c_cheap = self._make(score=9.5, cost=0.001)
        c_expensive = self._make(score=9.5, cost=0.050)
        assert _cost_aware_sort_key(c_cheap) < _cost_aware_sort_key(c_expensive)

    def test_equal_score_null_cost_treated_as_zero_beats_positive_cost(self):
        """Null cost is treated as 0, so a null-cost candidate ties on score but
        sorts before a positive-cost candidate of the same score."""
        c_null = self._make(score=9.5, cost=None)
        c_with_cost = self._make(score=9.5, cost=0.001)
        assert _cost_aware_sort_key(c_null) < _cost_aware_sort_key(c_with_cost)

    def test_incapable_candidates_always_trail_capable(self):
        """Incapable candidates (score <= 1.0) must sort after all capable candidates
        regardless of cost."""
        capable_low_score = self._make(score=1.1, cost=None)
        incapable_high_implied = self._make(score=1.0, cost=0.0)
        assert _cost_aware_sort_key(capable_low_score) < _cost_aware_sort_key(incapable_high_implied)

    def test_real_yaml_02_code_review_not_collapsed_to_deepseek(self, recommendations_yaml):
        """Regression: decide() for code_review must return claude-opus-4-6, not deepseek-v4-flash.

        The recommendations_yaml fixture has 02_code_review with claude-opus-4-6 at
        composite_score=10.0 and claude-sonnet-4-6 at 9.5 — both null-cost. Before the
        null-cost fix, any model with a measured cost would collapse the null-cost majority.
        """
        decision = decide(
            "Code review: audit security",
            role="security-engineer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "02_code_review"
        assert decision.primary is not None
        assert decision.primary.model_id == "claude-opus-4-6"
        assert decision.primary.composite_score == 10.0


# ---------------------------------------------------------------------------
# quality_tier — computation, filtering, and receipt stamping (PR-SR-FIX-3)
# ---------------------------------------------------------------------------

class TestQualityTier:

    def test_quality_tier_computation_from_score(self):
        """Boundary cases: 4.9→1, 5.0→2, 7.4→2, 7.5→3."""
        assert _compute_quality_tier(4.9, None) == 1
        assert _compute_quality_tier(5.0, None) == 2
        assert _compute_quality_tier(7.4, None) == 2
        assert _compute_quality_tier(7.5, None) == 3

    def test_cost_tier_zero_still_tier_1(self):
        """cost_tier=0 (local/free) forces quality_tier=1 regardless of score."""
        assert _compute_quality_tier(6.0, 0) == 1
        assert _compute_quality_tier(9.0, 0) == 1
        assert _compute_quality_tier(1.0, 0) == 1

    def test_min_tier_filter_excludes_lower(self, tmp_path):
        """min_quality_tier=2 removes tier-1 candidates, keeps tier-2 and tier-3."""
        data = {
            "routing_by_task": {
                "01_code_generation": {
                    "min_quality_tier": 2,
                    "candidates": [
                        {"model_id": "tier3-model", "composite_score": 8.0, "avg_duration_seconds": 100.0},
                        {"model_id": "tier2-model", "composite_score": 6.0, "avg_duration_seconds": 50.0},
                        {"model_id": "tier1-model", "composite_score": 3.0, "avg_duration_seconds": 10.0},
                    ],
                }
            }
        }
        p = tmp_path / "recs.yaml"
        p.write_text(yaml.dump(data), encoding="utf-8")
        candidates = recommend("01_code_generation", recommendations_path=p)
        model_ids = [c.model_id for c in candidates]
        assert "tier1-model" not in model_ids
        assert "tier2-model" in model_ids
        assert "tier3-model" in model_ids

    def test_max_tier_filter_caps_premium(self, tmp_path):
        """max_quality_tier=2 removes tier-3 candidates, keeps tier-1 and tier-2."""
        data = {
            "routing_by_task": {
                "01_code_generation": {
                    "max_quality_tier": 2,
                    "candidates": [
                        {"model_id": "tier3-model", "composite_score": 8.0, "avg_duration_seconds": 100.0},
                        {"model_id": "tier2-model", "composite_score": 6.0, "avg_duration_seconds": 50.0},
                        {"model_id": "tier1-model", "composite_score": 3.0, "avg_duration_seconds": 10.0},
                    ],
                }
            }
        }
        p = tmp_path / "recs.yaml"
        p.write_text(yaml.dump(data), encoding="utf-8")
        candidates = recommend("01_code_generation", recommendations_path=p)
        model_ids = [c.model_id for c in candidates]
        assert "tier3-model" not in model_ids
        assert "tier2-model" in model_ids

    def test_no_filter_no_regression(self, recommendations_yaml):
        """Without min/max set, sort output is identical to pre-FIX-3 _cost_aware_sort_key."""
        candidates = recommend("01_code_generation", recommendations_path=recommendations_yaml)
        expected_order = sorted(candidates, key=_cost_aware_sort_key)
        assert [c.model_id for c in candidates] == [c.model_id for c in expected_order]

    def test_recommend_returns_tier_on_candidate(self):
        """recommend() populates quality_tier (1, 2, or 3) on every returned candidate."""
        candidates = recommend("01_code_generation")
        assert len(candidates) > 0
        for c in candidates:
            assert c.quality_tier in (1, 2, 3), (
                f"{c.model_id} has unexpected quality_tier={c.quality_tier}"
            )
