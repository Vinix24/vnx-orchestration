#!/usr/bin/env python3
"""OI-1355 — model-name resolution defect in the cost path.

``_load_pricing_from_registry()`` in ``scripts/lib/provider_dispatch.py``
resolves a model name to its registry-configured price via three steps:
exact key, then a substring match tried in DICT-ITERATION order, then a
blind first-entry fallback. Two defects stack:

  1. The substring step accepts the first dict-order match instead of the
     most specific one, so a provider-prefixed model name like
     "claude-opus-5" can resolve to a shorter/wrong key ("opus") when that
     key happens to iterate earlier than the exact-suffix key ("opus-5").
  2. On total mismatch, the third step blindly returns the FIRST model in
     the section and reports it as a confirmed price, instead of a miss.

These tests build their OWN registry fixtures — never the live
wave7_models.yaml — because its anthropic prices are being corrected in
flight by PR #1606; hardcoding live values here would break the moment
that PR lands. Where dict ORDER matters to reproduce the defect, a
fixture's order deliberately mirrors wave7_models.yaml's anthropic section
(opus, opus-4-8, opus-4-6, sonnet, haiku, sonnet-5, opus-5, fable-5), noted
per-test — the PRICES themselves are synthetic and stable.

Production code (``scripts/lib/provider_dispatch.py``) is READ-ONLY here.
The fix lands in a separate dispatch; these tests pin only the two
observable ends (an exact-after-prefix match must win over a broader
substring; a total miss must never fabricate a price) without prescribing
the matching strategy or the miss signal (None vs. exception).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch as pd  # noqa: E402


def _fake_provider_cfg(model_items):
    """Build a fake ProviderConfig-like object with an ORDERED `models` dict.

    `model_items` is an ordered list of (key, cost_input, cost_output)
    tuples — insertion order is preserved and drives the dict-iteration-
    order defect under test. Mirrors the real ProviderConfig/ProviderModel
    shape (a `.models` dict of objects carrying `.cost_input_per_mtok` /
    `.cost_output_per_mtok`) without touching the live registry file.
    """
    cfg = MagicMock()
    cfg.models = {
        key: MagicMock(cost_input_per_mtok=cost_in, cost_output_per_mtok=cost_out)
        for key, cost_in, cost_out in model_items
    }
    return cfg


def _patched_registry(**sections):
    """Patch providers.provider_registry.load() to return a fixture registry.

    `_load_pricing_from_registry` does
    `from providers import provider_registry as _reg; registry = _reg.load()`
    — patching the `load` attribute on the already-imported module
    intercepts that call regardless of how many times the local import
    runs, since `_reg` is always the same module object from sys.modules.
    """
    return patch("providers.provider_registry.load", return_value=sections)


# ---------------------------------------------------------------------------
# RED today: dict-order substring match beats the exact-after-prefix key.
# ---------------------------------------------------------------------------

class TestSubstringMatchPicksWrongKey:

    def test_claude_opus_5_must_not_resolve_to_bare_opus_price(self):
        """claude-opus-5 should price as opus-5, not as the broader "opus" key.

        Fixture order mirrors wave7_models.yaml's anthropic section: "opus"
        iterates before "opus-5", so today's
        `next((v for k, v in cfg.models.items() if k in model_key or model_key in k))`
        matches "opus" first purely because it comes first in the dict, and
        returns its price (10.0/50.0) instead of opus-5's (5.0/25.0).
        """
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("sonnet", 3.0, 15.0),
            ("haiku", 1.0, 5.0),
            ("opus-5", 5.0, 25.0),
        ])
        with _patched_registry(anthropic=cfg):
            pricing = pd._load_pricing_from_registry("claude", "claude-opus-5")
        assert pricing == {"input": 5.0, "output": 25.0}, (
            f"claude-opus-5 must resolve the opus-5 entry (5.0/25.0), got {pricing!r} "
            "— the bare 'opus' entry is winning on dict order, not model identity"
        )

    def test_claude_opus_4_8_must_not_resolve_to_bare_opus_price(self):
        """claude-opus-4-8 should price as opus-4-8, not as the broader "opus" key.

        Same defect, second instance: "opus" precedes "opus-4-8" in dict
        order, so the substring step matches "opus" first even though
        "opus-4-8" is the exact-suffix match.
        """
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("opus-4-8", 7.0, 35.0),
            ("sonnet", 3.0, 15.0),
        ])
        with _patched_registry(anthropic=cfg):
            pricing = pd._load_pricing_from_registry("claude", "claude-opus-4-8")
        assert pricing == {"input": 7.0, "output": 35.0}, (
            f"claude-opus-4-8 must resolve the opus-4-8 entry (7.0/35.0), got {pricing!r} "
            "— the bare 'opus' entry is winning on dict order, not model identity"
        )

    def test_claude_sonnet_5_resolution_must_come_from_the_sonnet_5_key(self):
        """Pins PROVENANCE, not just the outcome value.

        On the live registry, "sonnet" and "sonnet-5" happen to share a
        price today, so a naive `pricing == {'input': 3.0, 'output': 15.0}`
        check would stay green even if the fix still resolves the wrong
        key — it would only break once those live prices diverge. This
        fixture gives the two keys deliberately DIFFERENT prices, in the
        same dict order as the live registry (sonnet before sonnet-5), so
        only a resolution that actually picks "sonnet-5" can pass.
        """
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("sonnet", 3.0, 15.0),
            ("haiku", 1.0, 5.0),
            ("sonnet-5", 4.0, 20.0),
        ])
        with _patched_registry(anthropic=cfg):
            pricing = pd._load_pricing_from_registry("claude", "claude-sonnet-5")
        assert pricing == {"input": 4.0, "output": 20.0}, (
            f"claude-sonnet-5 must resolve the sonnet-5 entry (4.0/20.0), got {pricing!r} "
            "— the bare 'sonnet' entry is winning on dict order; this is masked on the "
            "live registry today only because sonnet and sonnet-5 happen to share a price"
        )


# ---------------------------------------------------------------------------
# RED today: total mismatch fabricates a price instead of missing.
# ---------------------------------------------------------------------------

class TestBlindFallbackFabricatesAPrice:

    def test_totally_unknown_model_yields_no_fabricated_price(self):
        """A model with no registry relationship at all must not price as
        whatever happens to be the section's first entry.

        The fix may signal the miss as `None` or as a raised exception —
        both are acceptable per dispatch instruction (the miss-signal
        design is left to the fix). Only the ABSENCE of a fabricated price
        is pinned here.
        """
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("sonnet", 3.0, 15.0),
            ("haiku", 1.0, 5.0),
        ])
        with _patched_registry(anthropic=cfg):
            try:
                pricing = pd._load_pricing_from_registry("claude", "zzz-totally-unrelated-model")
            except Exception:
                return  # an explicit failure signal is an acceptable miss
        assert pricing is None, (
            f"a model with no registry match must miss (None), got fabricated pricing {pricing!r}"
        )


# ---------------------------------------------------------------------------
# GREEN today, must stay green: exact-match paths the fix must not break.
# ---------------------------------------------------------------------------

class TestExactAndSubProviderMatchesAreUnaffected:

    def test_bare_exact_key_sonnet_keeps_own_price(self):
        """A bare exact registry key hits `cfg.models.get(model_key)`
        directly — the first branch, untouched by the substring/fallback
        defect — and must keep resolving to its own price.
        """
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("sonnet", 3.0, 15.0),
            ("haiku", 1.0, 5.0),
        ])
        with _patched_registry(anthropic=cfg):
            pricing = pd._load_pricing_from_registry("claude", "sonnet")
        assert pricing == {"input": 3.0, "output": 15.0}

    def test_bare_exact_key_haiku_keeps_own_price(self):
        cfg = _fake_provider_cfg([
            ("opus", 10.0, 50.0),
            ("sonnet", 3.0, 15.0),
            ("haiku", 1.0, 5.0),
        ])
        with _patched_registry(anthropic=cfg):
            pricing = pd._load_pricing_from_registry("claude", "haiku")
        assert pricing == {"input": 1.0, "output": 5.0}

    def test_litellm_subprovider_deepseek_resolves_exact_model(self):
        """litellm:deepseek + a slash-qualified model name.

        `registry_key` is extracted from the colon-delimited provider
        string ("litellm:deepseek" -> "deepseek"), and `model_key` is the
        part after the slash in "deepseek/deepseek-v4-pro" — both hit the
        exact-match branch, unaffected by the substring/fallback defect.
        """
        cfg = _fake_provider_cfg([
            ("deepseek-v4-pro", 0.435, 0.87),
            ("deepseek-v4-flash", 0.14, 0.28),
        ])
        with _patched_registry(deepseek=cfg):
            pricing = pd._load_pricing_from_registry("litellm:deepseek", "deepseek/deepseek-v4-pro")
        assert pricing == {"input": 0.435, "output": 0.87}
