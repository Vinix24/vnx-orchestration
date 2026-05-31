"""test_cost_table.py — Unit tests for the _PROVIDER_RATES rate table in provider_costs.py.

Tests:
- Rate table has expected providers
- Expected models are present
- is_subscription_flat is bool for every entry
- _compute_cost_from_rates returns correct values
- _lookup_rate normalizes path-prefixed model names
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from provider_costs import (
    _PROVIDER_RATES,
    _lookup_rate,
    _compute_cost_from_rates,
)


EXPECTED_PROVIDERS = {"claude", "codex", "gemini", "kimi"}
EXPECTED_MODELS = {
    ("claude", "claude-sonnet-4-6"),
    ("claude", "sonnet"),
    ("claude", "claude-opus-4-8"),
    ("claude", "claude-haiku-4-5"),
    ("codex", "gpt-5.5"),
    ("gemini", "gemini-2.5-pro"),
    ("kimi", "kimi-k2.6"),
    ("kimi", "kimi-default"),
}


class TestRateTableStructure:
    def test_rate_table_is_dict(self):
        assert isinstance(_PROVIDER_RATES, dict)

    def test_rate_table_not_empty(self):
        assert len(_PROVIDER_RATES) > 0

    def test_all_keys_are_tuples_of_two_strings(self):
        for key in _PROVIDER_RATES:
            assert isinstance(key, tuple), f"key {key!r} is not a tuple"
            assert len(key) == 2, f"key {key!r} length != 2"
            assert isinstance(key[0], str), f"provider {key[0]!r} is not str"
            assert isinstance(key[1], str), f"model {key[1]!r} is not str"

    def test_all_values_are_three_tuples(self):
        for key, val in _PROVIDER_RATES.items():
            assert isinstance(val, tuple), f"value for {key!r} is not tuple"
            assert len(val) == 3, f"value for {key!r} length != 3"
            input_rate, output_rate, is_flat = val
            assert isinstance(input_rate, float), f"input_rate for {key!r} is not float"
            assert isinstance(output_rate, float), f"output_rate for {key!r} is not float"
            assert isinstance(is_flat, bool), f"is_subscription_flat for {key!r} is not bool"

    def test_expected_providers_present(self):
        providers_in_table = {k[0] for k in _PROVIDER_RATES}
        for provider in EXPECTED_PROVIDERS:
            assert provider in providers_in_table, f"provider {provider!r} missing"

    def test_expected_models_present(self):
        for key in EXPECTED_MODELS:
            assert key in _PROVIDER_RATES, f"expected key {key!r} missing from rate table"

    def test_kimi_is_subscription_flat(self):
        for (provider, model), (_, _, is_flat) in _PROVIDER_RATES.items():
            if provider == "kimi":
                assert is_flat is True, f"kimi model {model!r} should be subscription_flat"

    def test_claude_and_codex_are_not_subscription_flat(self):
        for (provider, model), (_, _, is_flat) in _PROVIDER_RATES.items():
            if provider in ("claude", "codex", "gemini"):
                assert is_flat is False, f"{provider}/{model} should not be subscription_flat"

    def test_rates_are_non_negative(self):
        for key, (in_rate, out_rate, _) in _PROVIDER_RATES.items():
            assert in_rate >= 0, f"input rate for {key!r} is negative"
            assert out_rate >= 0, f"output rate for {key!r} is negative"


class TestLookupRate:
    def test_exact_match(self):
        result = _lookup_rate("claude", "claude-sonnet-4-6")
        assert result is not None
        in_rate, out_rate, is_flat = result
        assert in_rate == 3.0
        assert out_rate == 15.0
        assert is_flat is False

    def test_path_prefix_normalized(self):
        # Model with path prefix should normalize to bare model name
        result = _lookup_rate("claude", "anthropic/claude-sonnet-4-6")
        assert result is not None
        in_rate, out_rate, is_flat = result
        assert in_rate == 3.0
        assert is_flat is False

    def test_unknown_provider_returns_none(self):
        result = _lookup_rate("unknown-provider", "some-model")
        assert result is None

    def test_unknown_model_returns_none(self):
        result = _lookup_rate("claude", "claude-v99-nonexistent")
        assert result is None


class TestComputeCostFromRates:
    def test_metered_cost_computed_correctly(self):
        # claude-sonnet: 3.0 input/mtok, 15.0 output/mtok
        # 1M input + 500k output = $3.0 + $7.5 = $10.5
        cost, is_flat = _compute_cost_from_rates("claude", "claude-sonnet-4-6", 1_000_000, 500_000)
        assert is_flat is False
        assert cost == pytest.approx(10.5, rel=1e-6)

    def test_subscription_flat_returns_none_cost(self):
        cost, is_flat = _compute_cost_from_rates("kimi", "kimi-k2.6", 1000, 500)
        assert is_flat is True
        assert cost is None

    def test_zero_tokens_returns_zero_cost(self):
        cost, is_flat = _compute_cost_from_rates("codex", "gpt-5.5", 0, 0)
        assert is_flat is False
        assert cost == 0.0

    def test_none_tokens_treated_as_zero(self):
        cost, is_flat = _compute_cost_from_rates("codex", "gpt-5.5", None, None)
        assert is_flat is False
        assert cost == 0.0

    def test_unknown_provider_returns_none(self):
        cost, is_flat = _compute_cost_from_rates("unknown", "model-x", 1000, 500)
        assert cost is None
        assert is_flat is False

    def test_gemini_pro_cost(self):
        # gemini-2.5-pro: 0.25 input/mtok, 0.75 output/mtok
        # 100k input + 200k output = $0.025 + $0.15 = $0.175
        cost, is_flat = _compute_cost_from_rates("gemini", "gemini-2.5-pro", 100_000, 200_000)
        assert is_flat is False
        assert cost == pytest.approx(0.175, rel=1e-6)
