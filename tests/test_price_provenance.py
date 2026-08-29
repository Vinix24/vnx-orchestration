"""Price provenance tests (OI-1334, OI-1335).

wave7_models.yaml carries prices with no record of where they came from or
when they were last checked. That is how gpt-5.5 kept the 1.25/10.00 pair
inherited from gpt-5.4, and kimi-k3 kept 0.60/2.50 inherited from
kimi-k2-0905-default: the strict-ladder invariant in
provider_registry.load_tier_ladder() only catches a stale price if it
happens to break monotonic ordering, and with these two it did not.

This file does not fix a single price and does not touch wave7_models.yaml.
It pins the provenance contract the fix must satisfy:

  1. every model carries a non-empty price_source and an ISO price_checked_at,
  2. every routing.tier_map rung cites a price checked within the last 90
     days (measured from a fixed reference date this file supplies itself —
     never the system clock, or the test becomes a ticking time bomb),
  3. a price_checked_at in the wrong format fails loudly, naming the model
     and the field — not a bare ValueError from date parsing.

Every test below is expected to fail on current main: price_source and
price_checked_at exist nowhere in the registry today (verified with
`grep -rn "price_source\\|price_checked_at" scripts/ tests/` before writing
this file — zero hits).
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from providers import provider_registry

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The date this dispatch was authored, not date.today(): a freshness check
# tied to the system clock goes red on its own the day it turns 91 days old,
# which is a time bomb, not a regression guard (OI-1334 lesson).
_REFERENCE_DATE = date(2026, 8, 18)
_MAX_PRICE_AGE_DAYS = 90


def _load_real_registry():
    return provider_registry.load()


def _all_model_ids():
    registry = _load_real_registry()
    ids = []
    for provider_key, cfg in registry.items():
        for model_key in cfg.models:
            ids.append((provider_key, model_key))
    return ids


_ALL_MODELS = _all_model_ids()
_ALL_MODEL_TEST_IDS = [f"{provider}/{model}" for provider, model in _ALL_MODELS]


class TestEveryModelCarriesPriceProvenance:
    """OI-1334: price_source + price_checked_at must exist on every model."""

    @pytest.mark.parametrize(
        "provider_key, model_key", _ALL_MODELS, ids=_ALL_MODEL_TEST_IDS
    )
    def test_has_non_empty_price_source(self, provider_key, model_key):
        registry = _load_real_registry()
        model = registry[provider_key].models[model_key]
        price_source = getattr(model, "price_source", "")
        assert price_source, (
            f"{provider_key}/{model_key} has no price_source recorded — "
            "where did this price come from?"
        )

    @pytest.mark.parametrize(
        "provider_key, model_key", _ALL_MODELS, ids=_ALL_MODEL_TEST_IDS
    )
    def test_has_iso_price_checked_at(self, provider_key, model_key):
        registry = _load_real_registry()
        model = registry[provider_key].models[model_key]
        price_checked_at = getattr(model, "price_checked_at", "")
        assert price_checked_at, (
            f"{provider_key}/{model_key} has no price_checked_at recorded"
        )
        assert _ISO_DATE_RE.match(price_checked_at), (
            f"{provider_key}/{model_key} price_checked_at "
            f"{price_checked_at!r} is not YYYY-MM-DD"
        )


def _resolve_tier_model(dispatch_enum: str, model_key: str):
    """Walk the registry sections to the ProviderModel a resolved tier route names.

    route.provider is the dispatch enum (e.g. "claude"), not the registry
    section key (e.g. "anthropic") — mirrors provider_registry._output_cost_for.
    """
    registry = _load_real_registry()
    for cfg in registry.values():
        if cfg.dispatch_enum == dispatch_enum and model_key in cfg.models:
            return cfg.models[model_key]
    pytest.fail(f"cannot resolve {dispatch_enum}/{model_key} in wave7_models.yaml")


_TIER_MAP = provider_registry.load_tier_map()
_ALL_TIERS = sorted(_TIER_MAP.keys())


class TestTierMapPricesAreFresh:
    """OI-1335: every routing.tier_map rung must cite a price checked recently.

    Scoped to each tier's primary step (the seven cost-ladder rungs
    documented in wave7_models.yaml) — not the availability-fallback chains,
    which are a different mechanism (safety net, not the priced escalation
    path).
    """

    @pytest.mark.parametrize("tier", _ALL_TIERS)
    def test_tier_price_checked_within_90_days(self, tier):
        route = _TIER_MAP[tier]
        model = _resolve_tier_model(route.provider, route.model)
        price_checked_at = getattr(model, "price_checked_at", "")
        assert price_checked_at, (
            f"tier {tier!r} ({route.provider}/{route.model}) has no "
            "price_checked_at recorded"
        )
        try:
            checked = date.fromisoformat(price_checked_at)
        except ValueError:
            pytest.fail(
                f"tier {tier!r} ({route.provider}/{route.model}) "
                f"price_checked_at {price_checked_at!r} is not a valid ISO date"
            )
        age_days = (_REFERENCE_DATE - checked).days
        assert age_days <= _MAX_PRICE_AGE_DAYS, (
            f"tier {tier!r} ({route.provider}/{route.model}) price was last "
            f"checked {price_checked_at} — {age_days} days before "
            f"{_REFERENCE_DATE.isoformat()}, older than the "
            f"{_MAX_PRICE_AGE_DAYS}-day freshness window"
        )


class TestMalformedPriceCheckedAtFailsLoud:
    """OI-1334: a wrong-format price_checked_at must fail loudly and specifically.

    Not a bare ValueError bubbling up from date parsing (Python's raw
    "Invalid isoformat string: '18-08-2026'" names neither the model nor the
    field) — the raised error's message must name both, so a broken date is
    diagnosable from the traceback alone.
    """

    def _fixture(self, tmp_path: Path, price_checked_at: str) -> Path:
        registry = {
            "providers": {
                "acme": {
                    "enabled": True,
                    "api_key_env": "ACME_API_KEY",
                    "dispatch_enum": "acme",
                    "models": {
                        "acme-flagship": {
                            "litellm_name": "acme/flagship",
                            "cost_input_per_mtok": 1.0,
                            "cost_output_per_mtok": 2.0,
                            "max_tokens": 1000,
                            "supports_streaming": True,
                            "supports_tool_calls": True,
                            "price_source": "test-fixture",
                            "price_checked_at": price_checked_at,
                        }
                    },
                }
            }
        }
        path = tmp_path / "wave7_models.yaml"
        path.write_text(yaml.dump(registry, sort_keys=False), encoding="utf-8")
        return path

    @pytest.mark.parametrize(
        "bad_value",
        ["18-08-2026", "2026/08/18", "Aug 18 2026", "not-a-date"],
    )
    def test_bad_format_raises_naming_model_and_field(self, tmp_path, bad_value):
        path = self._fixture(tmp_path, bad_value)
        with pytest.raises(Exception) as excinfo:
            provider_registry.load(registry_path=path)
        message = str(excinfo.value)
        assert "acme-flagship" in message, (
            f"error for a bad price_checked_at must name the model — got: {message!r}"
        )
        assert "price_checked_at" in message, (
            f"error for a bad price_checked_at must name the field — got: {message!r}"
        )

# ---------------------------------------------------------------------------
# OI-1335: prices must not be inherited
#
# The registry already carries an honesty field per model — price_source. Today
# 19 of 24 models set it to "unverified: registry-authored", i.e. someone typed
# the number. That marker is enforced to be PRESENT (TestEveryModelCarriesPriceProvenance
# above) and its date is validated for FORMAT, but nothing anywhere reads its VALUE.
# An unverified price therefore reaches cost_usd indistinguishable from a sourced one.
#
# These tests do not change that behaviour. They pin it, which is the part that was
# missing: a price that silently changes — the classic way a version bump inherits
# the previous model's number — now names itself in a red test, and a new unverified
# price cannot be added without a deliberate edit here.
#
# Deliberately NOT done in this PR: correcting the prices that look wrong. Verifying
# a price at source is not possible right now (the OpenRouter key is expired, OI-1500)
# and inventing one from memory is the exact failure mode this cluster is about.
# The suspects are named in the PR body and left marked unverified.
# ---------------------------------------------------------------------------

#: Every price in the registry, pinned. (provider, model) -> (input, output) per MTok.
#: Changing a price means editing this table in the same commit, with a source.
_PINNED_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("anthropic", "opus"): (15.0, 75.0),
    ("anthropic", "opus-4-8"): (5.0, 25.0),
    ("anthropic", "opus-4-6"): (5.0, 25.0),
    ("anthropic", "sonnet"): (3.0, 15.0),
    ("anthropic", "haiku"): (1.0, 5.0),
    ("anthropic", "sonnet-5"): (2.0, 10.0),
    ("anthropic", "opus-5"): (5.0, 25.0),
    ("anthropic", "fable-5"): (10.0, 50.0),
    ("openai", "gpt-5.5"): (5.0, 30.0),
    ("openai", "gpt-5.4"): (1.25, 10.0),
    ("google", "gemini-2.5-pro"): (1.25, 5.0),
    ("deepseek", "deepseek-v4-pro"): (0.435, 0.87),
    ("deepseek", "deepseek-v4-flash"): (0.14, 0.28),
    ("deepseek_harness", "deepseek-v4-pro"): (0.435, 0.87),
    ("deepseek_harness", "deepseek-v4-flash"): (0.14, 0.28),
    ("deepseek_harness", "deepseek-v4-pro-default"): (0.435, 0.87),
    ("moonshot", "kimi-k2-0905-default"): (0.6, 2.5),
    ("moonshot", "kimi-k2-6"): (0.95, 4.0),
    ("zai", "glm-5.2"): (0.76, 2.42),
    ("local_gemma", "gemma-4b-local"): (0.0, 0.0),
    ("kimi_cli", "kimi-k3"): (3.0, 15.0),
    ("kimi_cli", "kimi-k2-7"): (0.6, 2.5),
    ("kimi_cli", "kimi-default"): (0.6, 2.5),
    ("kimi_cli", "kimi-k2-6"): (0.95, 4.0),
}

#: Models whose price_source starts with "unverified" — a self-declared guess.
_UNVERIFIED: set[tuple[str, str]] = {
    ("anthropic", "opus"),
    ("anthropic", "sonnet"),
    ("anthropic", "haiku"),
    ("anthropic", "opus-5"),
    ("anthropic", "fable-5"),
    ("openai", "gpt-5.4"),
    ("google", "gemini-2.5-pro"),
    ("deepseek", "deepseek-v4-pro"),
    ("deepseek", "deepseek-v4-flash"),
    ("deepseek_harness", "deepseek-v4-pro"),
    ("deepseek_harness", "deepseek-v4-flash"),
    ("deepseek_harness", "deepseek-v4-pro-default"),
    ("moonshot", "kimi-k2-0905-default"),
    ("moonshot", "kimi-k2-6"),
    ("zai", "glm-5.2"),
    ("local_gemma", "gemma-4b-local"),
    ("kimi_cli", "kimi-k2-7"),
    ("kimi_cli", "kimi-default"),
    ("kimi_cli", "kimi-k2-6"),
}

#: Models that cite a real source.
_VERIFIED: set[tuple[str, str]] = {
    ("anthropic", "opus-4-8"),
    ("anthropic", "opus-4-6"),
    ("anthropic", "sonnet-5"),
    ("openai", "gpt-5.5"),
    ("kimi_cli", "kimi-k3"),
}


class TestPricesArePinned:
    """A price cannot move without this table moving with it."""

    def test_every_registry_model_is_pinned(self):
        registry = _load_real_registry()
        live = {
            (p, m) for p, cfg in registry.items() for m in cfg.models
        }
        assert live == set(_PINNED_PRICES), (
            "the registry's model set changed.\n"
            f"  added:   {sorted(live - set(_PINNED_PRICES))}\n"
            f"  removed: {sorted(set(_PINNED_PRICES) - live)}\n"
            "Update _PINNED_PRICES, _UNVERIFIED/_VERIFIED in the same commit."
        )

    def test_no_price_changed_without_updating_the_pin(self):
        registry = _load_real_registry()
        drifted = []
        for (prov, model), (want_in, want_out) in _PINNED_PRICES.items():
            entry = registry[prov].models[model]
            got = (entry.cost_input_per_mtok, entry.cost_output_per_mtok)
            if got != (want_in, want_out):
                drifted.append(f"{prov}/{model}: pinned {(want_in, want_out)} -> now {got}")
        assert not drifted, (
            "a price moved without the pin moving with it:\n  "
            + "\n  ".join(drifted)
            + "\n\nIf the new price is correct, update _PINNED_PRICES AND set a real "
            "price_source/price_checked_at. A version bump that carries the previous "
            "model's number forward is exactly what this guard exists to catch (OI-1335)."
        )


class TestPriceProvenanceSplitIsPinned:
    """Which prices are guesses is itself governed state."""

    def test_unverified_set_has_not_grown(self):
        registry = _load_real_registry()
        live_unverified = {
            (p, m)
            for p, cfg in registry.items()
            for m, entry in cfg.models.items()
            if str(getattr(entry, "price_source", "")).startswith("unverified")
        }
        added = live_unverified - _UNVERIFIED
        assert not added, (
            f"new unverified prices: {sorted(added)}. Adding a self-declared guess to "
            "the registry is a decision, not an oversight — record it here explicitly."
        )

    def test_verified_prices_did_not_quietly_become_guesses(self):
        registry = _load_real_registry()
        for prov, model in sorted(_VERIFIED):
            src = str(getattr(registry[prov].models[model], "price_source", ""))
            assert src and not src.startswith("unverified"), (
                f"{prov}/{model} was sourced and now reads {src!r}"
            )

    def test_the_split_covers_every_model(self):
        assert _UNVERIFIED | _VERIFIED == set(_PINNED_PRICES)
        assert not (_UNVERIFIED & _VERIFIED)
