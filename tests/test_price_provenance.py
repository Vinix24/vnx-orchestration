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
