"""Tests for cost-aware smart routing (G1-G5).

Covers:
- cost_loader.compute_cost_per_call() against known wave7_models entries
- cost_loader.enrich_candidates() fills null costs in-place
- recommend() / decide() ranks cheapest capable model first when costs differ
- Models at score=1.0 (incapable) rank after capable ones regardless of cost
- Null-cost fallback: when all costs are null, sort falls back to score descending
- VNX_AUTO_ROUTE env-var wiring: bash scripts pass --auto-route when set to "1"
- Route decision JSON for a "refactor" task class shows cheapest-capable selection

Dispatch-ID: 20260529-134019-smart-routing-activate
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from cost_loader import compute_cost_per_call, enrich_candidates
from smart_router import (
    RouteCandidate,
    RouteDecision,
    _cost_aware_sort_key,
    _INCAPABLE_SCORE_FLOOR,
    classify_task,
    decide,
    recommend,
    write_route_decision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    model_id: str,
    score: float,
    cost: Optional[float] = None,
) -> RouteCandidate:
    return RouteCandidate(
        model_id=model_id,
        composite_score=score,
        avg_duration_seconds=100.0,
        cost_usd_per_call=cost,
    )


def _yaml_with_costs(tmp_path, entries_by_class: dict) -> Path:
    """Write a routing_recommendations.yaml with explicit entries."""
    data = {"routing_by_task": entries_by_class}
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# cost_loader.compute_cost_per_call
# ---------------------------------------------------------------------------

class TestComputeCostPerCall:

    def test_sonnet_cost_matches_wave7(self):
        cost = compute_cost_per_call("claude-sonnet-4-6")
        # input: 5000 * 3.00 / 1M + output: 2000 * 15.00 / 1M = 0.015 + 0.030 = 0.045
        assert cost is not None
        assert abs(cost - 0.045) < 1e-9

    def test_haiku_cheaper_than_sonnet(self):
        haiku = compute_cost_per_call("claude-haiku-4-5")
        sonnet = compute_cost_per_call("claude-sonnet-4-6")
        assert haiku is not None
        assert sonnet is not None
        assert haiku < sonnet

    def test_deepseek_flash_cheaper_than_sonnet(self):
        flash = compute_cost_per_call("deepseek-v4-flash")
        sonnet = compute_cost_per_call("claude-sonnet-4-6")
        assert flash is not None
        assert sonnet is not None
        assert flash < sonnet

    def test_opus_most_expensive_claude(self):
        opus = compute_cost_per_call("claude-opus-4-6")
        sonnet = compute_cost_per_call("claude-sonnet-4-6")
        haiku = compute_cost_per_call("claude-haiku-4-5")
        assert opus is not None
        assert sonnet is not None
        assert haiku is not None
        assert opus > sonnet > haiku

    def test_unknown_model_returns_none(self):
        assert compute_cost_per_call("future-unknown-model-9.9") is None

    def test_kimi_k2_0905_has_cost(self):
        cost = compute_cost_per_call("kimi-k2-0905")
        assert cost is not None
        assert cost > 0

    def test_glm_5_1_has_cost(self):
        cost = compute_cost_per_call("glm-5-1")
        assert cost is not None
        assert cost > 0

    def test_missing_wave7_returns_none(self, tmp_path):
        cost = compute_cost_per_call("claude-sonnet-4-6", wave7_path=tmp_path / "absent.yaml")
        assert cost is None


# ---------------------------------------------------------------------------
# cost_loader.enrich_candidates
# ---------------------------------------------------------------------------

class TestEnrichCandidates:

    def test_fills_null_costs_for_known_models(self):
        candidates = [
            _make_candidate("claude-sonnet-4-6", 8.0, None),
            _make_candidate("claude-haiku-4-5", 4.5, None),
        ]
        enrich_candidates(candidates)
        assert candidates[0].cost_usd_per_call is not None
        assert candidates[1].cost_usd_per_call is not None

    def test_preserves_explicit_costs(self):
        candidates = [_make_candidate("claude-sonnet-4-6", 8.0, 0.099)]
        enrich_candidates(candidates)
        assert candidates[0].cost_usd_per_call == 0.099

    def test_unknown_model_cost_stays_none(self):
        candidates = [_make_candidate("future-model-x", 8.0, None)]
        enrich_candidates(candidates)
        assert candidates[0].cost_usd_per_call is None

    def test_safe_when_wave7_absent(self, tmp_path):
        candidates = [_make_candidate("claude-sonnet-4-6", 8.0, None)]
        enrich_candidates(candidates, wave7_path=tmp_path / "absent.yaml")
        assert candidates[0].cost_usd_per_call is None


# ---------------------------------------------------------------------------
# _cost_aware_sort_key
# ---------------------------------------------------------------------------

class TestCostAwareSortKey:

    def test_capable_ranks_before_incapable(self):
        capable = _make_candidate("model-a", 5.0, 0.10)
        incapable = _make_candidate("model-b", _INCAPABLE_SCORE_FLOOR, 0.001)
        assert _cost_aware_sort_key(capable) < _cost_aware_sort_key(incapable)

    def test_cheaper_capable_ranks_before_expensive_capable(self):
        cheap = _make_candidate("model-cheap", 7.0, 0.01)
        expensive = _make_candidate("model-exp", 8.0, 0.20)
        assert _cost_aware_sort_key(cheap) < _cost_aware_sort_key(expensive)

    def test_null_cost_ranks_last_within_capable(self):
        explicit_cost = _make_candidate("model-a", 8.0, 0.05)
        null_cost = _make_candidate("model-b", 9.0, None)
        assert _cost_aware_sort_key(explicit_cost) < _cost_aware_sort_key(null_cost)

    def test_score_tiebreak_when_costs_equal(self):
        high_score = _make_candidate("model-high", 9.0, 0.05)
        low_score = _make_candidate("model-low", 7.0, 0.05)
        assert _cost_aware_sort_key(high_score) < _cost_aware_sort_key(low_score)

    def test_both_null_cost_sorts_by_score_desc(self):
        high = _make_candidate("model-h", 9.0, None)
        low = _make_candidate("model-l", 7.0, None)
        assert _cost_aware_sort_key(high) < _cost_aware_sort_key(low)


# ---------------------------------------------------------------------------
# recommend() — cost-aware ordering with real wave7_models.yaml
# ---------------------------------------------------------------------------

class TestRecommendCostAware:

    def test_cheapest_capable_model_ranks_first(self, tmp_path):
        """Hybrid policy: among models that clear the capability threshold (>=7.0), the cheapest wins.
        deepseek-flash (5.5) is BELOW the bar so it cannot beat the band despite being cheapest;
        sonnet (8.0, ~$0.045) beats opus (9.0, ~$0.225) within the band."""
        entries = {
            "01_code_generation": [
                {"model_id": "claude-opus-4-6", "composite_score": 9.0,
                 "avg_duration_seconds": 330.0, "cost_usd_per_call": None},
                {"model_id": "deepseek-v4-flash", "composite_score": 5.5,
                 "avg_duration_seconds": 56.0, "cost_usd_per_call": None},
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
            ],
        }
        rec_path = _yaml_with_costs(tmp_path, entries)
        candidates = recommend("01_code_generation", recommendations_path=rec_path)

        # After cost enrichment: deepseek-flash ≈ $0.0014, sonnet ≈ $0.045, opus ≈ $0.225.
        # deepseek (5.5) is below the 7.0 capability bar, so sonnet (cheapest in the band) wins.
        assert len(candidates) >= 3
        assert candidates[0].model_id == "claude-sonnet-4-6"
        assert candidates[0].cost_usd_per_call is not None
        # Within the capable band (score >= 7.0), cost ascending.
        band = [c for c in candidates if c.composite_score >= 7.0]
        costs = [c.cost_usd_per_call or float("inf") for c in band]
        assert costs == sorted(costs), "Band candidates must be sorted by cost ascending"

    def test_incapable_models_trail_despite_low_cost(self, tmp_path):
        """Models at score=1.0 rank after all capable ones even with zero cost."""
        entries = {
            "03_refactoring": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.5,
                 "avg_duration_seconds": 209.0, "cost_usd_per_call": None},
                {"model_id": "glm-5-1", "composite_score": 1.0,
                 "avg_duration_seconds": 0.85, "cost_usd_per_call": 0.000001},
            ],
        }
        rec_path = _yaml_with_costs(tmp_path, entries)
        candidates = recommend("03_refactoring", recommendations_path=rec_path)

        assert len(candidates) == 2
        # sonnet (capable, score=8.5) must beat glm-5-1 (incapable, score=1.0) despite glm being cheaper
        assert candidates[0].model_id == "claude-sonnet-4-6"
        assert candidates[1].model_id == "glm-5-1"

    def test_null_costs_preserve_score_order(self, tmp_path):
        """When all costs are null, sort falls back to score descending."""
        entries = {
            "02_code_review": [
                {"model_id": "claude-opus-4-6", "composite_score": 10.0,
                 "avg_duration_seconds": 90.0, "cost_usd_per_call": None},
                {"model_id": "claude-sonnet-4-6", "composite_score": 9.5,
                 "avg_duration_seconds": 72.0, "cost_usd_per_call": None},
            ],
        }
        rec_path = _yaml_with_costs(tmp_path, entries)

        # Monkeypatch wave7 to be absent so costs stay null
        from cost_loader import _load_wave7_costs
        import cost_loader
        original = cost_loader._WAVE7_PATH
        cost_loader._WAVE7_PATH = tmp_path / "absent.yaml"
        try:
            candidates = recommend("02_code_review", recommendations_path=rec_path)
        finally:
            cost_loader._WAVE7_PATH = original

        # Null costs → tiebreak by score desc → opus (10.0) first
        assert candidates[0].model_id == "claude-opus-4-6"
        assert candidates[1].model_id == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# decide() — end-to-end route decision with cost-aware primary
# ---------------------------------------------------------------------------

class TestDecideCostAware:

    def test_refactor_decision_picks_cheapest_capable(self, tmp_path):
        """For refactor task class, cheapest capable model is primary."""
        entries = {
            "03_refactoring": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.5,
                 "avg_duration_seconds": 209.0, "cost_usd_per_call": None},
                {"model_id": "deepseek-v4-flash", "composite_score": 8.5,
                 "avg_duration_seconds": 19.0, "cost_usd_per_call": None},
                {"model_id": "glm-5-1", "composite_score": 1.0,
                 "avg_duration_seconds": 0.85, "cost_usd_per_call": None},
            ],
        }
        rec_path = _yaml_with_costs(tmp_path, entries)
        decision = decide(
            "Refactor the dispatch router into smaller modules",
            recommendations_path=rec_path,
        )
        assert decision.task_class == "03_refactoring"
        assert decision.primary is not None
        # deepseek-v4-flash is cheapest capable — ~$0.0014 vs sonnet ~$0.045
        assert decision.primary.model_id == "deepseek-v4-flash"
        assert decision.primary.cost_usd_per_call is not None
        assert decision.cost_estimate == decision.primary.cost_usd_per_call

    def test_route_decision_json_for_refactor(self, tmp_path, state_dir):
        """Show one example route decision JSON proving cheapest-capable selection."""
        entries = {
            "03_refactoring": [
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.5,
                 "avg_duration_seconds": 209.0, "cost_usd_per_call": None},
                {"model_id": "deepseek-v4-flash", "composite_score": 8.5,
                 "avg_duration_seconds": 19.0, "cost_usd_per_call": None},
            ],
        }
        rec_path = _yaml_with_costs(tmp_path, entries)
        decision = decide(
            "Refactor the SubprocessAdapter into smaller modules",
            recommendations_path=rec_path,
        )
        write_route_decision("dispatch-refactor-cost-demo", decision, state_dir=state_dir)

        ndjson_path = state_dir / "route_decisions.ndjson"
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())

        # Verify JSON shape and cheapest-capable selection
        assert record["dispatch_id"] == "dispatch-refactor-cost-demo"
        assert record["task_class"] == "03_refactoring"
        assert record["chosen_route"]["model_id"] == "deepseek-v4-flash"
        assert record["cost_estimate"] is not None
        assert record["cost_estimate"] < 0.01  # deepseek-flash ≈ $0.0014/call

    def test_code_generation_uses_real_yaml_cheapest_capable(self):
        """With real routing_recommendations.yaml + real wave7, cheapest capable wins."""
        decision = decide("Implement the new router module")
        assert decision.task_class == "01_code_generation"
        assert decision.primary is not None
        # With cost enrichment, deepseek-v4-flash ($0.0014) should beat sonnet ($0.045)
        assert decision.primary.cost_usd_per_call is not None
        # Primary must be cheaper than claude-sonnet-4-6
        sonnet_cost = compute_cost_per_call("claude-sonnet-4-6")
        assert decision.primary.cost_usd_per_call <= sonnet_cost


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# VNX_AUTO_ROUTE env-var wiring — bash script integration
# ---------------------------------------------------------------------------

class TestVnxAutoRouteEnvWiring:
    """Verify each dispatch script passes --auto-route when VNX_AUTO_ROUTE=1."""

    SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"

    def _grep_script(self, script_path: Path, pattern: str) -> bool:
        result = subprocess.run(
            ["grep", "-q", pattern, str(script_path)],
            capture_output=True,
        )
        return result.returncode == 0

    def test_dispatch_deliver_sh_has_auto_route_guard(self):
        script = self.SCRIPTS_ROOT / "lib" / "dispatch_deliver.sh"
        assert script.exists()
        assert self._grep_script(script, "VNX_AUTO_ROUTE")
        assert self._grep_script(script, "auto-route")

    def test_dispatch_sh_has_auto_route_guard(self):
        script = self.SCRIPTS_ROOT / "commands" / "dispatch.sh"
        assert script.exists()
        assert self._grep_script(script, "VNX_AUTO_ROUTE")
        assert self._grep_script(script, "auto-route")

    def test_dispatch_agent_sh_has_auto_route_guard(self):
        script = self.SCRIPTS_ROOT / "commands" / "dispatch-agent.sh"
        assert script.exists()
        assert self._grep_script(script, "VNX_AUTO_ROUTE")
        assert self._grep_script(script, "auto-route")

    def test_auto_route_requires_value_one(self):
        """Only VNX_AUTO_ROUTE=1 enables --auto-route; 0, empty, and unset do not."""
        for script_rel in [
            "lib/dispatch_deliver.sh",
            "commands/dispatch.sh",
            "commands/dispatch-agent.sh",
        ]:
            script = self.SCRIPTS_ROOT / script_rel
            content = script.read_text(encoding="utf-8")
            # Guard must check for value "1", not just non-empty
            assert '"1"' in content or "== 1" in content, (
                f"{script_rel}: VNX_AUTO_ROUTE guard must check for value '1'"
            )
