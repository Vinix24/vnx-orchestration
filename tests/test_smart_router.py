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
    BlockedRecommendationError,
    RouteCandidate,
    RouteDecision,
    classify_task,
    decide,
    recommend,
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
# classify_task — specialist-role dominance (OI-1143)
# ---------------------------------------------------------------------------

class TestClassifySpecialistRoleDominance:
    """A role mapping to a non-default task class is an explicit signal and wins
    over instruction verb-guessing (OI-1143). Builder roles (mapping to the
    default class) keep instruction-first behavior, as does the no-role-resolved
    sentinel "identity_unresolved" (unmapped)."""

    def test_code_reviewer_role_classifies_review_instruction(self):
        # The exact OI-1143 measurement: this returned 01_code_generation because
        # "code-reviewer" was unmapped and the instruction trips no review regex
        # ("Review the diff" — "diff" is not in the pattern's object alternation).
        assert classify_task(
            "Review the diff on PR 1455 for correctness", role="code-reviewer",
        ) == "02_code_review"

    def test_code_reviewer_role_dominates_code_gen_verb(self):
        # "implement" trips the 01_code_generation regex, but a dispatch to a
        # code-reviewer is review work — the role signal dominates.
        assert classify_task(
            "Check whether they implement the retry contract correctly",
            role="code-reviewer",
        ) == "02_code_review"

    def test_debugger_role_dominates_code_gen_verb(self):
        assert classify_task(
            "Implement a fix once you find it", role="debugger",
        ) == "05_debugging"

    def test_system_architect_role_maps_to_design(self):
        assert classify_task(
            "Think through the module boundaries", role="system-architect",
        ) == "06_design"

    def test_builder_role_still_instruction_first(self):
        # backend-developer maps to the default class → carries no discriminating
        # signal; the instruction text keeps deciding (a real builder role, not
        # the identity_unresolved sentinel).
        assert classify_task(
            "debug the flaky lease test", role="backend-developer",
        ) == "05_debugging"


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

    def test_equal_score_null_cost_ranks_after_measured_cost(self):
        """Hybrid policy: a null/unknown cost ranks LAST within the capable band (never assumed
        free), so a same-score MEASURED-cost candidate sorts before the null-cost one."""
        c_null = self._make(score=9.5, cost=None)
        c_with_cost = self._make(score=9.5, cost=0.001)
        assert _cost_aware_sort_key(c_with_cost) < _cost_aware_sort_key(c_null)

    def test_incapable_candidates_always_trail_capable(self):
        """Incapable candidates (score <= 1.0) must sort after all capable candidates
        regardless of cost."""
        capable_low_score = self._make(score=1.1, cost=None)
        incapable_high_implied = self._make(score=1.0, cost=0.0)
        assert _cost_aware_sort_key(capable_low_score) < _cost_aware_sort_key(incapable_high_implied)

    def test_real_yaml_02_code_review_not_collapsed_to_deepseek(self, recommendations_yaml):
        """Hybrid policy: code_review must NOT collapse to deepseek-v4-flash (5.5, below the 7.0
        capability bar). Both opus (10.0, ~$0.225) and sonnet (9.5, ~$0.045) clear the bar; among
        the band the CHEAPEST wins, so sonnet is chosen — a strong model at a fraction of opus's cost.
        deepseek never wins (it is below the bar), which is the regression this guards.
        """
        decision = decide(
            "Code review: audit security",
            role="security-engineer",
            recommendations_path=recommendations_yaml,
        )
        assert decision.task_class == "02_code_review"
        assert decision.primary is not None
        assert decision.primary.model_id == "claude-sonnet-4-6"
        assert decision.primary.composite_score == 9.5
        assert decision.primary.model_id != "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Blocked-model load validation (OI-1255)
#
# routing_recommendations.yaml recommended glm-5 (base) in five places while
# provider_constraints.yaml `deprecated-glm-models` blocks every GLM except
# glm-5.2. decide() silently filtered those entries, so the drift was
# invisible. The loader now fails loud instead: a blocked model anywhere in
# the file raises BlockedRecommendationError.
# ---------------------------------------------------------------------------

def _single_candidate_yaml(tmp_path, model_id, task_class="01_code_generation"):
    data = {
        "routing_by_task": {
            task_class: [
                {"model_id": model_id, "composite_score": 8.0,
                 "avg_duration_seconds": 100.0, "cost_usd_per_call": None},
            ],
        }
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


class TestBlockedModelValidation:

    @pytest.mark.parametrize("blocked", ["glm-5", "glm-5.1", "glm-4.5", "glm-4.6", "glm-5-1"])
    def test_blocked_glm_model_fails_loud(self, tmp_path, blocked):
        """A deprecated GLM variant in the file must raise, not be silently dropped."""
        rec_path = _single_candidate_yaml(tmp_path, blocked)
        with pytest.raises(BlockedRecommendationError, match="deprecated-glm-models"):
            recommend("01_code_generation", recommendations_path=rec_path)

    def test_error_names_model_task_class_and_source(self, tmp_path):
        rec_path = _single_candidate_yaml(tmp_path, "glm-5", task_class="05_debugging")
        with pytest.raises(BlockedRecommendationError) as excinfo:
            _load_recommendations(rec_path)
        msg = str(excinfo.value)
        assert "glm-5" in msg
        assert "05_debugging" in msg
        assert str(rec_path) in msg
        assert "deprecated-glm-models" in msg

    def test_decide_also_fails_loud(self, tmp_path):
        """decide() goes through the same loader, so it raises the same error."""
        rec_path = _single_candidate_yaml(tmp_path, "glm-5")
        with pytest.raises(BlockedRecommendationError):
            decide("implement a new module", recommendations_path=rec_path)

    def test_glm_5_2_remains_a_valid_candidate(self, tmp_path):
        """The one allowed GLM version loads and ranks normally."""
        rec_path = _single_candidate_yaml(tmp_path, "glm-5.2")
        candidates = recommend("01_code_generation", recommendations_path=rec_path)
        assert [c.model_id for c in candidates] == ["glm-5.2"]
        decision = decide("implement a new module", recommendations_path=rec_path)
        assert decision.primary is not None
        assert decision.primary.model_id == "glm-5.2"
        assert decision.constraints_applied == []

    def test_non_glm_candidates_unaffected(self, tmp_path):
        """Non-GLM models keep passing validation, incl. env-keyed deepseek lanes
        (deepseek-harness-subscription-blocked is runtime-route policy, not model
        identity, so it must NOT fail the load even with no DEEPSEEK_API_KEY)."""
        data = {
            "routing_by_task": {
                "01_code_generation": [
                    {"model_id": "claude-sonnet-5", "composite_score": 9.0,
                     "avg_duration_seconds": 100.0, "cost_usd_per_call": None},
                    {"model_id": "deepseek-v4-flash", "composite_score": 8.0,
                     "avg_duration_seconds": 100.0, "cost_usd_per_call": None},
                    {"model_id": "kimi-k2-7-code", "composite_score": 7.0,
                     "avg_duration_seconds": 100.0, "cost_usd_per_call": None},
                    {"model_id": "codex-gpt-5-5", "composite_score": 6.0,
                     "avg_duration_seconds": 100.0, "cost_usd_per_call": None},
                ],
            }
        }
        rec_path = tmp_path / "routing_recommendations.yaml"
        rec_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        candidates = recommend("01_code_generation", recommendations_path=rec_path)
        assert len(candidates) == 4

    def test_real_yaml_has_no_blocked_candidates(self):
        """The shipped file passes its own validation and names no deprecated GLM."""
        recs = _load_recommendations()  # raises BlockedRecommendationError if violated
        all_ids = [c.model_id for cands in recs.values() for c in cands]
        assert "glm-5" not in all_ids
        assert "glm-5.1" not in all_ids
        assert "glm-5-1" not in all_ids
        assert "glm-4.5" not in all_ids
        assert "glm-4.6" not in all_ids
        # The allowed GLM is still recommended (canonical registry spelling).
        assert "glm-5.2" in all_ids

    def test_real_yaml_glm_5_2_survives_decide_constraint_filter(self):
        """End-to-end: a GLM-capable task class can surface glm-5.2 from decide().

        Before the canonical-spelling fix, the dash-form glm-5-2 entries were
        caught by the same deprecated-glm-models filter as the genuinely
        blocked glm-5, so GLM could never be recommended at all.
        """
        from smart_router import _filter_by_constraints

        recs = _load_recommendations()
        glm_classes = [
            tc for tc, cands in recs.items()
            if any(c.model_id == "glm-5.2" for c in cands)
        ]
        assert glm_classes, "expected at least one task class recommending glm-5.2"
        for tc in glm_classes:
            allowed, _applied = _filter_by_constraints(recs[tc], env={})
            assert any(c.model_id == "glm-5.2" for c in allowed), (
                f"glm-5.2 must survive the constraint filter for {tc}"
            )
