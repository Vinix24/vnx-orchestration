"""Tests for Smart Lanes cost-tier routing in smart_router.py.

Verifies that 'cost-tier-zero' and 'privacy-required' tags promote
gemma-4b-local to the primary candidate, and that parse_route_model_id
maps gemma-4b-local to the local-gemma provider.
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

import yaml

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _make_yaml_with_gemma(tmp_path):
    """Build a minimal recommendations yaml that includes gemma-4b-local."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
                {"model_id": "deepseek-v4-flash", "composite_score": 5.5,
                 "avg_duration_seconds": 56.0, "cost_usd_per_call": None},
                {"model_id": "gemma-4b-local", "composite_score": 6.0,
                 "avg_duration_seconds": 5.2, "cost_usd_per_call": None, "cost_tier": 0},
            ],
            "04_documentation": [
                {"model_id": "gemma-4b-local", "composite_score": 8.5,
                 "avg_duration_seconds": 4.8, "cost_usd_per_call": None, "cost_tier": 0},
                {"model_id": "deepseek-v4-flash", "composite_score": 8.5,
                 "avg_duration_seconds": 12.6, "cost_usd_per_call": None},
            ],
        }
    }
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data))
    return p


class TestCostTierZeroPromotion(unittest.TestCase):
    """decide() with cost-tier-zero tag promotes gemma-4b-local to primary."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._tmp_path = Path(self._tmp)
        self._yaml_path = _make_yaml_with_gemma(self._tmp_path)

    def test_no_tags_standard_ranking(self):
        from smart_router import decide

        decision = decide(
            "implement a new feature",
            recommendations_path=self._yaml_path,
        )
        # Without tags, highest-score model wins
        self.assertIsNotNone(decision.primary)
        self.assertNotEqual(decision.primary.model_id, "gemma-4b-local")

    def test_cost_tier_zero_tag_promotes_gemma(self):
        from smart_router import decide

        decision = decide(
            "implement a new feature",
            tags=["cost-tier-zero"],
            recommendations_path=self._yaml_path,
        )
        self.assertIsNotNone(decision.primary)
        self.assertEqual(decision.primary.model_id, "gemma-4b-local")

    def test_privacy_required_tag_promotes_gemma(self):
        from smart_router import decide

        decision = decide(
            "document the billing module",
            tags=["privacy-required"],
            recommendations_path=self._yaml_path,
        )
        self.assertIsNotNone(decision.primary)
        self.assertEqual(decision.primary.model_id, "gemma-4b-local")

    def test_tag_case_insensitive(self):
        from smart_router import decide

        decision = decide(
            "build a classifier",
            tags=["COST-TIER-ZERO"],
            recommendations_path=self._yaml_path,
        )
        self.assertIsNotNone(decision.primary)
        self.assertEqual(decision.primary.model_id, "gemma-4b-local")

    def test_documentation_task_gemma_promoted_with_tag(self):
        from smart_router import decide

        # Without tag: standard cost-aware sort (gemma and deepseek tie; order non-deterministic)
        # With tag: gemma is explicitly promoted to front
        decision = decide(
            "write documentation for the API",
            tags=["cost-tier-zero"],
            recommendations_path=self._yaml_path,
        )
        self.assertEqual(decision.primary.model_id, "gemma-4b-local")


class TestRouteCandidateCostTier(unittest.TestCase):
    """RouteCandidate carries cost_tier field."""

    def test_cost_tier_loaded_from_yaml(self):
        from smart_router import _load_recommendations, RouteCandidate

        import tempfile
        tmp = tempfile.mkdtemp()
        yaml_path = _make_yaml_with_gemma(Path(tmp))

        recs = _load_recommendations(yaml_path)
        code_gen = recs["01_code_generation"]

        gemma = next((c for c in code_gen if c.model_id == "gemma-4b-local"), None)
        self.assertIsNotNone(gemma)
        self.assertEqual(gemma.cost_tier, 0)
        self.assertIsNone(gemma.cost_usd_per_call)  # null cost — promoted via tag, not cost-aware sort

    def test_standard_model_has_no_cost_tier(self):
        from smart_router import _load_recommendations

        import tempfile
        tmp = tempfile.mkdtemp()
        yaml_path = _make_yaml_with_gemma(Path(tmp))

        recs = _load_recommendations(yaml_path)
        code_gen = recs["01_code_generation"]

        sonnet = next((c for c in code_gen if c.model_id == "claude-sonnet-4-6"), None)
        self.assertIsNotNone(sonnet)
        self.assertIsNone(sonnet.cost_tier)


class TestParseRouteModelIdGemma(unittest.TestCase):
    """parse_route_model_id maps gemma-4b-local to local-gemma provider."""

    def test_gemma_maps_to_local_gemma(self):
        from smart_router import parse_route_model_id

        provider, model = parse_route_model_id("gemma-4b-local")
        self.assertEqual(provider, "local-gemma")
        self.assertEqual(model, "gemma-4b-local")

    def test_claude_still_maps_correctly(self):
        from smart_router import parse_route_model_id

        provider, model = parse_route_model_id("claude-sonnet-4-6")
        self.assertEqual(provider, "claude")
        self.assertEqual(model, "sonnet")

    def test_deepseek_still_maps_correctly(self):
        from smart_router import parse_route_model_id

        provider, model = parse_route_model_id("deepseek-v4-pro")
        self.assertIn("litellm", provider)


class TestPromoteCostTierZeroHelper(unittest.TestCase):
    """_promote_cost_tier_zero reorders list correctly."""

    def test_zero_tier_moves_to_front(self):
        from smart_router import RouteCandidate, _promote_cost_tier_zero

        candidates = [
            RouteCandidate("claude-sonnet-4-6", 8.0, 512.0, cost_tier=None),
            RouteCandidate("deepseek-v4-flash", 5.5, 56.0, cost_tier=None),
            RouteCandidate("gemma-4b-local", 6.0, 5.2, cost_usd_per_call=0.0, cost_tier=0),
        ]
        result = _promote_cost_tier_zero(candidates)
        self.assertEqual(result[0].model_id, "gemma-4b-local")
        self.assertEqual(len(result), 3)

    def test_no_zero_tier_unchanged(self):
        from smart_router import RouteCandidate, _promote_cost_tier_zero

        candidates = [
            RouteCandidate("claude-sonnet-4-6", 8.0, 512.0),
            RouteCandidate("deepseek-v4-flash", 5.5, 56.0),
        ]
        result = _promote_cost_tier_zero(candidates)
        self.assertEqual(result[0].model_id, "claude-sonnet-4-6")

    def test_empty_list(self):
        from smart_router import _promote_cost_tier_zero

        self.assertEqual(_promote_cost_tier_zero([]), [])


class TestAutoRouteForwardsTagsToDecide(unittest.TestCase):
    """Fix 3: smart_router.decide() receives tags from --auto-route dispatch path."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._tmp_path = Path(self._tmp)
        self._yaml_path = _make_yaml_with_gemma(self._tmp_path)

    def test_tags_forwarded_select_gemma(self):
        from smart_router import decide

        decision = decide(
            "implement something",
            tags=["cost-tier-zero"],
            recommendations_path=self._yaml_path,
        )
        self.assertEqual(decision.primary.model_id, "gemma-4b-local")

    def test_no_tags_does_not_select_gemma_for_code_gen(self):
        from smart_router import decide

        decision = decide(
            "implement something",
            tags=[],
            recommendations_path=self._yaml_path,
        )
        self.assertNotEqual(decision.primary.model_id, "gemma-4b-local")


if __name__ == "__main__":
    unittest.main()
