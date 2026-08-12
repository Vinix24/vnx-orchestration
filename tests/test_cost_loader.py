"""Tests for cost_loader — model-name normalization and cost resolution.

Covers:
- Ledger-stored GLM model names resolve to costs (OI-977)
- normalize_model_name is applied before cost lookup
- Deprecated GLM variants (glm-4.5, glm-5.1, etc.) resolve to glm-5.2 cost
- Unknown models still return None
- Existing map-form names continue to work

Dispatch-ID: 20260804-m02-oi977
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from cost_loader import compute_cost_per_call


# ---------------------------------------------------------------------------
# GLM ledger-stored names (OI-977 — the core defect)
# ---------------------------------------------------------------------------

class TestGlmLedgerNamesResolveToCost:
    """Every GLM model name stored in the ledger must return a cost, not None."""

    def test_glm_5_2_dot_form_has_cost(self):
        """448 receipts in the ledger use this exact spelling."""
        cost = compute_cost_per_call("glm-5.2")
        assert cost is not None, "glm-5.2 (dot form, as stored in ledger) must resolve to a cost"
        assert cost > 0

    def test_openrouter_z_ai_glm_5_2_has_cost(self):
        """276 receipts in the ledger use this exact spelling."""
        cost = compute_cost_per_call("openrouter/z-ai/glm-5.2")
        assert cost is not None, (
            "openrouter/z-ai/glm-5.2 (provider-prefixed, as stored in ledger) "
            "must resolve to a cost"
        )
        assert cost > 0

    def test_openrouter_z_ai_glm_5_1_has_cost(self):
        """288 receipts in the ledger use this exact spelling (deprecated variant)."""
        cost = compute_cost_per_call("openrouter/z-ai/glm-5.1")
        assert cost is not None, (
            "openrouter/z-ai/glm-5.1 (deprecated variant in ledger) "
            "must resolve to glm-5.2 cost"
        )
        assert cost > 0

    def test_openrouter_z_ai_glm_5_has_cost(self):
        """363 receipts in the ledger use this exact spelling (bare version)."""
        cost = compute_cost_per_call("openrouter/z-ai/glm-5")
        assert cost is not None, (
            "openrouter/z-ai/glm-5 (bare variant in ledger) "
            "must resolve to glm-5.2 cost"
        )
        assert cost > 0

    def test_glm_4_5_has_cost(self):
        """1 receipt in the ledger uses this deprecated spelling."""
        cost = compute_cost_per_call("glm-4.5")
        assert cost is not None, "glm-4.5 (deprecated) must resolve to glm-5.2 cost"
        assert cost > 0

    def test_glm_4_6_has_cost(self):
        """1 receipt in the ledger uses this deprecated spelling."""
        cost = compute_cost_per_call("glm-4.6")
        assert cost is not None, "glm-4.6 (deprecated) must resolve to glm-5.2 cost"
        assert cost > 0


# ---------------------------------------------------------------------------
# Existing map-form names — must keep working
# ---------------------------------------------------------------------------

class TestExistingMapFormNames:
    """Names that were already in _ROUTING_MODEL_MAP must still resolve."""

    def test_glm_5_1_dash_form_has_cost(self):
        """Legacy dash-form key used in routing_recommendations."""
        cost = compute_cost_per_call("glm-5-1")
        assert cost is not None
        assert cost > 0

    def test_glm_5_2_dash_form_has_cost(self):
        """Legacy dash-form key used in routing_recommendations."""
        cost = compute_cost_per_call("glm-5-2")
        assert cost is not None
        assert cost > 0

    def test_deepseek_v4_pro_has_cost(self):
        """DeepSeek models were already working."""
        cost = compute_cost_per_call("deepseek-v4-pro")
        assert cost is not None
        assert cost > 0

    def test_kimi_k2_0905_has_cost(self):
        """Kimi legacy key was already working."""
        cost = compute_cost_per_call("kimi-k2-0905")
        assert cost is not None
        assert cost > 0


# ---------------------------------------------------------------------------
# OI-1143: routing_recommendations candidates missing from the cost register
# ---------------------------------------------------------------------------

class TestOI1143CandidateModelsResolve:
    """kimi-k2-7-code and codex-gpt-5-4/-5-5 are routing_recommendations
    candidates; smart_router.decide() logged 'unknown candidate model' for them
    and weighed them cost-blind (cost_usd_per_call stayed None)."""

    def test_kimi_k2_7_code_has_cost(self):
        # kimi-cli "kimi-for-coding" — registry key kimi_cli/kimi-k2-7
        cost = compute_cost_per_call("kimi-k2-7-code")
        assert cost is not None and cost > 0

    def test_kimi_k2_7_registry_key_same_cost(self):
        cost = compute_cost_per_call("kimi-k2-7")
        assert cost is not None  # not vacuously equal via None == None
        assert cost == compute_cost_per_call("kimi-k2-7-code")

    def test_codex_gpt_5_4_has_cost(self):
        # codex CLI lane running openai gpt-5.4 — API-metered ($1.25/$10 per
        # Mtok, routing_policy.yaml cost reference, measured 2026-06-06)
        cost = compute_cost_per_call("codex-gpt-5-4")
        assert cost is not None and cost > 0

    def test_codex_gpt_5_5_has_cost(self):
        cost = compute_cost_per_call("codex-gpt-5-5")
        assert cost is not None and cost > 0

    def test_enrich_candidates_no_unknown_warning(self, caplog):
        """enrich_candidates must fill the cost without the unknown-candidate
        warning for every routing_recommendations model_id of these lanes."""
        import logging
        from types import SimpleNamespace

        from cost_loader import enrich_candidates

        candidates = [
            SimpleNamespace(model_id=m, cost_usd_per_call=None)
            for m in ("kimi-k2-7-code", "codex-gpt-5-4", "codex-gpt-5-5")
        ]
        with caplog.at_level(logging.WARNING, logger="cost_loader"):
            enrich_candidates(candidates)
        assert "unknown candidate model" not in caplog.text
        for candidate in candidates:
            assert candidate.cost_usd_per_call is not None, candidate.model_id
            assert candidate.cost_usd_per_call > 0, candidate.model_id


# ---------------------------------------------------------------------------
# Negative-path: unknown models
# ---------------------------------------------------------------------------

class TestUnknownModelReturnsNone:
    """Truly unknown models still return None."""

    def test_future_unknown_model_returns_none(self):
        assert compute_cost_per_call("future-unknown-model-9.9") is None

    def test_missing_wave7_returns_none(self, tmp_path):
        cost = compute_cost_per_call("claude-sonnet-4-6", wave7_path=tmp_path / "absent.yaml")
        assert cost is None


# ---------------------------------------------------------------------------
# Consistency: all GLM variants resolve to the same cost
# ---------------------------------------------------------------------------

class TestGlmConsistency:
    """All GLM variants (canonical, deprecated, provider-prefixed) resolve
    to the same glm-5.2 cost."""

    def test_all_glm_variants_same_cost(self):
        variants = [
            "glm-5.2",                   # canonical dot-form
            "glm-5-1",                   # legacy dash-form
            "glm-5-2",                   # legacy dash-form
            "openrouter/z-ai/glm-5.2",   # provider-prefixed canonical
            "openrouter/z-ai/glm-5.1",   # provider-prefixed deprecated
            "openrouter/z-ai/glm-5",     # provider-prefixed bare
            "glm-4.5",                   # deprecated
            "glm-4.6",                   # deprecated
        ]
        costs = []
        for name in variants:
            cost = compute_cost_per_call(name)
            assert cost is not None, f"Variant {name!r} must resolve to a cost"
            costs.append(cost)

        # All variants must return the same glm-5.2 cost
        # OI-1083: re-measured 2026-08-10 — input: 5000 * 0.76/1M + output: 2000 * 2.42/1M = 0.0038 + 0.00484 = 0.00864
        expected = 0.00864
        for name, cost in zip(variants, costs):
            assert abs(cost - expected) < 1e-9, (
                f"Variant {name!r}: expected {expected}, got {cost}"
            )
