"""test_cost_table.py — Unit tests for price_run() in provider_costs.py (ADR-040).

The hardcoded _PROVIDER_RATES table is gone; every price now comes from the
model registry through price_run(). These tests pin the contract:

- the legacy rate-table symbols no longer exist
- price_run computes metered cost from the registry
- every alias of a model resolves to the same price versions
- a run crossing the DeepSeek peak boundary prices with two rates
- a model without a price returns "unavailable:no_price:<model>", never 0
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_costs
from provider_costs import price_run
from providers import provider_registry


# ---------------------------------------------------------------------------
# The legacy rate table is gone
# ---------------------------------------------------------------------------

class TestLegacyRateTableRemoved:
    def test_no_rate_table_symbols_remain(self):
        assert not hasattr(provider_costs, "_PROVIDER_RATES")
        assert not hasattr(provider_costs, "_lookup_rate")
        assert not hasattr(provider_costs, "_compute_cost_from_rates")
        assert not hasattr(provider_costs, "resolve_cost_usd")


# ---------------------------------------------------------------------------
# price_run metered cost
# ---------------------------------------------------------------------------

class TestPriceRunMeteredCost:
    def test_metered_cost_computed_from_registry(self):
        # sonnet-5: 2.00 input / 10.00 output per MTok
        # 1M input + 500k output = 2.00 + 5.00 = 7.00
        cost, source = price_run(
            "claude", "sonnet-5", [{"input": 1_000_000, "output": 500_000}]
        )
        assert cost == pytest.approx(7.0, rel=1e-9)
        assert source == "sonnet-5@2026-08-19"

    def test_zero_tokens_returns_zero_cost(self):
        cost, _source = price_run("codex", "gpt-5.5", [{"input": 0, "output": 0}])
        assert cost == 0.0

    def test_missing_segment_counts_treated_as_zero(self):
        cost, _source = price_run("codex", "gpt-5.5", [{}])
        assert cost == 0.0

    def test_cache_read_is_billed_at_its_own_price(self):
        # deepseek-flash: 0.30 input / 1.20 output / 0.006 cache-read
        cost, _source = price_run(
            "deepseek-harness",
            "deepseek-flash",
            [{"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000}],
        )
        assert cost == pytest.approx(0.30 + 1.20 + 0.006, rel=1e-9)


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

class TestAliasResolution:
    def test_opus_aliases_share_price_versions(self):
        v_opus = provider_registry.resolve_price_versions("anthropic", "opus")
        v_opus48 = provider_registry.resolve_price_versions("anthropic", "opus-4-8")
        assert v_opus is not None
        assert v_opus == v_opus48

    def test_opus_aliases_price_identically(self):
        seg = [{"input": 1_000_000, "output": 500_000}]
        c_opus, _ = price_run("claude", "opus", seg)
        c_opus48, _ = price_run("claude", "opus-4-8", seg)
        assert c_opus == c_opus48
        # 1M input at $5/Mtok + 500k output at $25/Mtok = 5.00 + 12.50
        assert c_opus == pytest.approx(5.0 + 12.5, rel=1e-9)

    def test_provider_prefixed_slug_resolves(self):
        # "claude-opus-5" strips the anthropic section prefix to "opus-5".
        v = provider_registry.resolve_price_versions("anthropic", "claude-opus-5")
        assert v is not None
        assert v[-1].input_per_mtok == 5.0
        assert v[-1].output_per_mtok == 25.0


# ---------------------------------------------------------------------------
# Peak / off-peak boundary
# ---------------------------------------------------------------------------

class TestPeakBoundary:
    def test_run_crossing_peak_boundary_prices_with_two_rates(self):
        # Two segments either side of the 01:00-04:00 UTC weekday peak window:
        # 02:00 is peak, 05:00 is off-peak. 1M input tokens in each.
        segments = [
            {"input": 1_000_000, "output": 0, "ts": "2026-09-14T02:00:00Z"},  # peak
            {"input": 1_000_000, "output": 0, "ts": "2026-09-14T05:00:00Z"},  # off-peak
        ]
        cost, _source = price_run("deepseek-harness", "deepseek-v4-pro", segments)
        assert cost == pytest.approx(1.32 + 0.66, rel=1e-9)

    def test_off_peak_is_half_peak(self):
        peak = price_run(
            "deepseek-harness",
            "deepseek-v4-pro",
            [{"input": 1_000_000, "output": 1_000_000, "peak": True}],
        )[0]
        off = price_run(
            "deepseek-harness",
            "deepseek-v4-pro",
            [{"input": 1_000_000, "output": 1_000_000, "peak": False}],
        )[0]
        assert peak == pytest.approx(1.32 + 3.96, rel=1e-9)
        assert off == pytest.approx(peak / 2, rel=1e-9)

    def test_explicit_peak_flag_wins_over_timestamp(self):
        # A weekday 05:00 timestamp is off-peak, but an explicit peak flag bills peak.
        cost, _source = price_run(
            "deepseek-harness",
            "deepseek-v4-pro",
            [{"input": 1_000_000, "output": 0, "ts": "2026-09-14T05:00:00Z", "peak": True}],
        )
        assert cost == pytest.approx(1.32, rel=1e-9)

    def test_weekend_is_off_peak(self):
        # Saturday 02:00 UTC falls outside the weekday-only peak window.
        cost, _source = price_run(
            "deepseek-harness",
            "deepseek-v4-pro",
            [{"input": 1_000_000, "output": 0, "ts": "2026-09-19T02:00:00Z"}],
        )
        assert cost == pytest.approx(0.66, rel=1e-9)


# ---------------------------------------------------------------------------
# Unpriced models
# ---------------------------------------------------------------------------

class TestUnpricedModelSentinel:
    def test_unknown_model_returns_unavailable(self):
        cost, source = price_run(
            "deepseek-harness", "no-such-model", [{"input": 1_000_000, "output": 1}]
        )
        assert cost is None
        assert source == "unavailable:no_price:no-such-model"

    def test_unknown_provider_returns_unavailable(self):
        cost, source = price_run("not-a-provider", "model-x", [{"input": 1}])
        assert cost is None
        assert source == "unavailable:no_price:model-x"

    def test_openrouter_unpriced_model_returns_unavailable(self):
        # gpt-4o-mini lost its rate-table price with the _PROVIDER_RATES removal;
        # there is no measured registry price, so it must not bill.
        cost, source = price_run(
            "litellm:openrouter", "openrouter/openai/gpt-4o-mini", [{"input": 1_000_000}]
        )
        assert cost is None
        assert source == "unavailable:no_price:openrouter/openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# Peak window classification
# ---------------------------------------------------------------------------

class TestPeakWindowClassification:
    @pytest.mark.parametrize(
        "ts, expected",
        [
            ("2026-09-14T02:00:00Z", True),   # Monday, first peak window
            ("2026-09-14T07:30:00Z", True),   # Monday, second peak window
            ("2026-09-14T00:30:00Z", False),  # Monday, before first window
            ("2026-09-14T05:00:00Z", False),  # Monday, between windows
            ("2026-09-14T12:00:00Z", False),  # Monday, afternoon
            ("2026-09-19T02:00:00Z", False),  # Saturday (weekend)
        ],
    )
    def test_is_peak(self, ts, expected):
        assert provider_costs._is_peak(ts) is expected
