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
import warnings as _warnings
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from providers import provider_registry

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The date this check was last authored, not date.today(): a freshness check tied
# to the system clock goes red on its own the day it turns 91 days old, which is a
# time bomb, not a regression guard (OI-1334 lesson).
#
# INVARIANT (OI-1354): this date must never be EARLIER than the newest
# price_checked_at in the tier map. A rung dated after it scores a negative age,
# which passes "<= 90 days" forever — a silent, permanent exemption from the
# freshness rule. TestNoTierPriceIsFutureDated pins that. Recording a fresher price
# therefore means advancing this date in the same commit, deliberately, which is the
# point: the reference moves by a decision, never by the calendar.
#
# Advanced 2026-08-18 -> 2026-08-29: tier-mid (claude/sonnet-5) was re-checked
# 2026-08-19 and scored -1 against the old reference. Re-measured at the new date,
# the oldest rung is 29 days; nothing is stale.
_REFERENCE_DATE = date(2026, 8, 29)
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
    # dispatch-20260905-084500-registry-gpt6-astra: allowlist extension by
    # operator decision 2026-09-05. Rates are the <=272K-input flat pair; above
    # 272K input the whole request bills 2x input / 1.5x output (not modelled
    # here). Codex lane runs on a ChatGPT login — bookkeeping, not billed.
    ("openai", "gpt-6-astra"): (10.0, 50.0),
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
    ("openai", "gpt-6-astra"),
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


# ---------------------------------------------------------------------------
# OI-1354: the frozen reference date guards the FORM, not the living clock
#
# TestTierMapPricesAreFresh above compares against _REFERENCE_DATE, frozen at the
# date the check was authored. That choice is right: a freshness assertion tied to
# date.today() turns red on its own the morning a price crosses 91 days, which is a
# time bomb rather than a regression guard (OI-1334).
#
# The cost of freezing it is that the check describes a moment that has passed. The
# world keeps moving and nothing says so. OI-1349 is the proof: it was filed claiming
# gpt-5.5 was 93 days stale and the other six rungs 15-18 days old. Re-measured
# 2026-08-29, gpt-5.5 sits at 12 days (re-checked 2026-08-17) and NO rung exceeds 90.
# The finding was true when written and no one could see it stop being true.
#
# So: keep the frozen assertion, and make the living clock visible without letting it
# fail CI. Staleness is reported as a warning; the mechanism that computes it is
# tested against fixed dates, so the reporter itself can fail.
#
# Also closed here: a price_checked_at in the FUTURE relative to the comparison date
# yields a negative age, which passes "<= 90 days" forever. Today tier-mid
# (claude/sonnet-5, checked 2026-08-19) already scores -1 against the frozen
# reference. Left alone, a far-future date is a permanent, silent exemption from the
# freshness rule.
# ---------------------------------------------------------------------------



def tier_price_ages(as_of: date) -> dict[str, int]:
    """Age in days of each tier_map rung's price_checked_at, relative to `as_of`.

    A rung with no recorded date is omitted — that absence is a separate failure,
    already asserted by TestTierMapPricesAreFresh. Negative values are returned as-is
    so a future-dated price is visible as the anomaly it is rather than being clamped
    into a comfortable-looking small number.
    """
    ages: dict[str, int] = {}
    for tier in _ALL_TIERS:
        route = _TIER_MAP[tier]
        model = _resolve_tier_model(route.provider, route.model)
        checked = getattr(model, "price_checked_at", "")
        if not checked:
            continue
        ages[tier] = (as_of - date.fromisoformat(checked)).days
    return ages


def stale_tiers(as_of: date, max_age_days: int = _MAX_PRICE_AGE_DAYS) -> dict[str, int]:
    """Rungs whose price is older than the freshness window at `as_of`."""
    return {t: a for t, a in tier_price_ages(as_of).items() if a > max_age_days}


def future_dated_tiers(as_of: date) -> dict[str, int]:
    """Rungs whose price_checked_at lies after `as_of` — a negative age.

    These pass every "<= max_age" assertion no matter how far in the future they sit,
    so a typo'd or deliberately forward-dated year exempts a price from the freshness
    rule permanently and silently.
    """
    return {t: a for t, a in tier_price_ages(as_of).items() if a < 0}


class TestStalenessReporterWorks:
    """The reporter is the thing that must be trustworthy, so it is tested against
    fixed dates. These CAN fail; the live report below deliberately cannot."""

    def test_ages_are_measured_from_the_given_date(self):
        early = tier_price_ages(date(2026, 8, 18))
        later = tier_price_ages(date(2026, 9, 18))
        assert early, "no tier carries a price_checked_at — the fixture is broken"
        assert set(early) == set(later)
        for tier in early:
            assert later[tier] - early[tier] == 31

    def test_a_far_future_date_makes_every_rung_stale(self):
        far = date(2031, 1, 1)
        assert set(stale_tiers(far)) == set(tier_price_ages(far))

    def test_nothing_is_stale_at_its_own_check_date(self):
        """Uses _REFERENCE_DATE, not a literal. A hardcoded date here would drift away
        from the constant the moment the reference advances — which is the exact defect
        class this file exists to catch, reproduced inside its own test."""
        for tier, age in tier_price_ages(_REFERENCE_DATE).items():
            if age >= 0:
                assert tier not in stale_tiers(_REFERENCE_DATE - timedelta(days=age))

    def test_future_dated_detection_triggers_on_a_negative_age(self):
        very_early = date(2020, 1, 1)
        assert set(future_dated_tiers(very_early)) == set(tier_price_ages(very_early))
        assert future_dated_tiers(date(2031, 1, 1)) == {}


class TestNoTierPriceIsFutureDated:
    """A future date is not freshness, it is an exemption. This one DOES fail."""

    def test_no_rung_is_dated_after_the_reference(self):
        future = future_dated_tiers(_REFERENCE_DATE)
        assert not future, (
            f"tier prices dated after {_REFERENCE_DATE.isoformat()}: {future}. "
            "A negative age passes every '<= 90 days' check forever, so a forward-dated "
            "price_checked_at silently exempts that rung from the freshness rule. "
            "Either the date is a typo, or _REFERENCE_DATE needs advancing in the same "
            "commit that recorded the newer check."
        )


class TestLivePriceStalenessIsVisible:
    """Reports today's staleness WITHOUT failing. Never assert here — the whole point
    of the frozen reference above is that CI must not go red on the calendar."""

    def test_report_live_staleness_as_a_warning(self):
        today = date.today()
        stale = stale_tiers(today)
        future = future_dated_tiers(today)
        if stale:
            _warnings.warn(
                "price freshness (informational, not a failure): "
                + ", ".join(
                    f"{tier} is {age} days old" for tier, age in sorted(stale.items())
                )
                + f" — older than {_MAX_PRICE_AGE_DAYS} days as of {today.isoformat()}. "
                "Re-check at source and advance _REFERENCE_DATE in the same commit.",
                UserWarning,
                stacklevel=2,
            )
        if future:
            _warnings.warn(
                f"price_checked_at in the future as of {today.isoformat()}: {future}",
                UserWarning,
                stacklevel=2,
            )
