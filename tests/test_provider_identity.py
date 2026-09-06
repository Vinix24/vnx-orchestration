#!/usr/bin/env python3
"""Tests for provider_identity — the closed provider vocabulary
(PRD bewijsketen-en-afdwinging, Golf 1a).

Covers:
  1. Every ProviderIdentity member normalises to itself (idempotent).
  2. The documented variant-cascade (deepseek, kimi, glm, moonshot, generic
     litellm) resolves as designed.
  3. Fail-loud: unrecognisable input raises UnrecognizedProviderError, never
     a passthrough.
  4. Reconciliation with governance_emit._PROVIDER_RE — pins the exact set
     of DELIBERATE divergences (DEEPSEEK, UNKNOWN) so a future edit that
     silently drifts the two vocabularies apart further is caught here.
  5. Reconciliation with dispatch_spec.Provider — the fleet's closed set for
     *dispatch-time* provider selection; every member (except the AUTO
     capability-seam placeholder) must resolve to a consistent
     ProviderIdentity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from provider_identity import (
    ProviderIdentity,
    UnrecognizedProviderError,
    normalize_provider,
)


class TestCanonicalPassthrough:
    """Every closed-set value normalises to itself."""

    @pytest.mark.parametrize("member", list(ProviderIdentity))
    def test_member_value_is_idempotent(self, member):
        assert normalize_provider(member.value) is member

    @pytest.mark.parametrize("member", list(ProviderIdentity))
    def test_member_value_is_case_insensitive(self, member):
        # Salvage-if-recognisable: "Claude", "CODEX", "Litellm:Zai" etc. must
        # still resolve, since only governance_emit's dispatch-time gate
        # requires strict lowercase — the receipt-time vocabulary is more
        # forgiving by design (it is reading free text, not validating a CLI
        # argument).
        assert normalize_provider(member.value.upper()) is member


class TestBlankInput:
    def test_none_is_unknown(self):
        assert normalize_provider(None) is ProviderIdentity.UNKNOWN

    def test_empty_string_is_unknown(self):
        assert normalize_provider("") is ProviderIdentity.UNKNOWN

    def test_whitespace_only_is_unknown(self):
        assert normalize_provider("   ") is ProviderIdentity.UNKNOWN


class TestDeepseekVariants:
    """OI-1111 (test_report_to_receipt_converter.py::TestLaneIdentityResolution
    ::test_deepseek_harness_lane_wins) already pins litellm:deepseek(:model)?
    -> deepseek-harness end to end; this class re-covers it at the unit level
    plus the free-text self-report variant, and the bare-"deepseek" split
    this module makes explicit for the first time.
    """

    def test_litellm_deepseek_bare(self):
        assert normalize_provider("litellm:deepseek") is ProviderIdentity.DEEPSEEK_HARNESS

    def test_litellm_deepseek_with_model_alias(self):
        assert (
            normalize_provider("litellm:deepseek:deepseek-v4-pro")
            is ProviderIdentity.DEEPSEEK_HARNESS
        )

    def test_self_reported_harness_free_text(self):
        assert (
            normalize_provider("deepseek (harness, key-auth)")
            is ProviderIdentity.DEEPSEEK_HARNESS
        )

    def test_bare_deepseek_is_its_own_identity_not_harness(self):
        """The design question golf1a-provider-enum was asked to answer:
        bare "deepseek" is the cheap key-auth litellm sub-provider lane
        (scout_prepass.py, gate_request_handler._request_deepseek,
        vnx_tagger.py's VNX_TAGGER_PROVIDER default) -- a mechanism
        independently real and distinct from deepseek-harness, not a typo
        or a variant spelling of it.
        """
        assert normalize_provider("deepseek") is ProviderIdentity.DEEPSEEK
        assert ProviderIdentity.DEEPSEEK is not ProviderIdentity.DEEPSEEK_HARNESS


class TestKimiVariants:
    def test_kimi_k3(self):
        assert normalize_provider("kimi-k3") is ProviderIdentity.KIMI

    def test_kimi_code_slash_k3(self):
        assert normalize_provider("kimi-code/k3") is ProviderIdentity.KIMI

    def test_verbose_self_report_salvaged(self):
        """Recognisable free text is salvaged into the closed identity
        rather than raising -- this is what actually fixes the "42 distinct
        provider strings" aggregation problem the fail-open bug caused.
        Contrast with TestFailLoud below, where NO keyword is present at
        all and salvage is impossible.
        """
        assert normalize_provider("Moonshot AI (Kimi Code CLI)") is ProviderIdentity.KIMI

    def test_moonshot_lane_key_is_not_laundered_into_kimi(self):
        """kimi-via-cli-only (~/.claude/rules/provider-constraints.md) is a
        BLOCKING constraint against routing kimi work through the Moonshot
        API instead of the CLI. A benchmark-baseline litellm:moonshot lane
        key can legitimately embed a kimi model alias
        (provider_dispatch._build_lane_key: ("moonshot", "kimi-k2-6") ->
        "litellm:moonshot:kimi-k2-6") -- collapsing that into plain "kimi"
        would erase the one signal that lets governance tell a sanctioned
        CLI-OAuth receipt apart from a forbidden-route attempt.
        """
        assert (
            normalize_provider("litellm:moonshot:kimi-k2-6")
            is ProviderIdentity.LITELLM_MOONSHOT
        )
        assert normalize_provider("litellm:moonshot") is ProviderIdentity.LITELLM_MOONSHOT


class TestGlmVariants:
    def test_glm_harness_exact(self):
        assert normalize_provider("glm-harness") is ProviderIdentity.GLM_HARNESS

    def test_litellm_zai_lane_key(self):
        """The smart-router's route decision for an auto-routed glm-* model
        records lane_provider="litellm:zai" (its own internal sub-provider
        lookup key, smart_router.parse_route_model_id) -- folded back into
        GLM_HARNESS so the ledger counts one GLM provider, pre-existing and
        tested end to end via
        test_glm_harness_lane_wins_over_body_sonnet_claude.
        """
        assert normalize_provider("litellm:zai") is ProviderIdentity.GLM_HARNESS


class TestGenericLitellmFallback:
    def test_bare_litellm(self):
        assert normalize_provider("litellm") is ProviderIdentity.LITELLM

    def test_unrecognised_but_well_formed_litellm_compound(self):
        """bedrock/ollama/anthropic/openrouter sub-providers
        (provider_dispatch._LITELLM_SUB_PROVIDER_DEFAULTS) have no dedicated
        ProviderIdentity member -- they fall through to the generic LITELLM
        bucket by SHAPE (governance_emit._PROVIDER_RE), not by an enumerated
        list, so a new litellm sub-provider never needs a matching edit here
        just to stop raising.
        """
        assert (
            normalize_provider("litellm:bedrock:claude-sonnet-4-6")
            is ProviderIdentity.LITELLM
        )
        assert (
            normalize_provider("litellm:openrouter:openai/gpt-4o-mini")
            is ProviderIdentity.LITELLM
        )


class TestFailLoud:
    """The defect this module replaces: report_to_receipt_converter's old
    ``return p`` passthrough let free text with no recognisable provider
    keyword reach the ledger verbatim. normalize_provider must refuse it.
    """

    def test_pure_noise_raises(self):
        with pytest.raises(UnrecognizedProviderError):
            normalize_provider("`.")

    def test_torn_off_instruction_fragment_raises(self):
        """Measured 2026-08-09, still reproducible against the unmodified
        converter as of this PR (see
        test_report_to_receipt_converter.py::TestNormaliseProvider::
        test_unrecognised_garbage_is_never_passed_through_raw for the
        red/green evidence against the actual converter function).
        """
        with pytest.raises(UnrecognizedProviderError):
            normalize_provider(
                "` regel. Zonder die identiteitsregels landt je receipt niet."
            )

    def test_never_seen_vendor_name_raises(self):
        """Break-your-own-guard: a value with no historical precedent at all
        must still refuse, not silently join the vocabulary.
        """
        with pytest.raises(UnrecognizedProviderError):
            normalize_provider("totally-bogus-vendor-xyz")

    def test_error_carries_the_raw_value(self):
        with pytest.raises(UnrecognizedProviderError) as exc_info:
            normalize_provider("`.")
        assert exc_info.value.raw == "`."
        assert "`." in str(exc_info.value)

    def test_raise_hits_the_real_call_not_a_stub(self):
        """Sanity check that this test suite exercises the actual
        normalize_provider() implementation (imported at module load, not a
        local reimplementation or a mock) -- a raise on a value the function
        itself defines as UNRECOGNIZED_PROBE below only proves something if
        the function under test is the genuine article.
        """
        import provider_identity as module

        assert normalize_provider is module.normalize_provider
        probe = "UNRECOGNIZED_PROBE_never_matches_any_branch"
        assert probe not in module._EXACT
        with pytest.raises(UnrecognizedProviderError):
            normalize_provider(probe)


class TestReconcileWithGovernanceEmit:
    """governance_emit._PROVIDER_RE is the OTHER, dispatch-time vocabulary
    this module was asked to reconcile with. They agree everywhere except
    two DELIBERATE divergences (DEEPSEEK, UNKNOWN) -- pinned here so a
    future change that silently widens that gap is caught.

    HARDE BESTANDSGRENS: governance_emit.py itself is out of scope for this
    PR (a parallel dispatch owns it) -- imported read-only for comparison.
    """

    def _provider_re(self):
        from governance_emit import _PROVIDER_RE

        return _PROVIDER_RE

    @pytest.mark.parametrize(
        "member",
        [m for m in ProviderIdentity if m not in (ProviderIdentity.DEEPSEEK, ProviderIdentity.UNKNOWN)],
    )
    def test_agreement_members_satisfy_the_regex(self, member):
        assert self._provider_re().match(member.value), (
            f"{member} is expected to satisfy governance_emit._PROVIDER_RE "
            "but does not -- either the regex changed, or this member no "
            "longer belongs in the agreement set."
        )

    def test_deepseek_is_the_one_receipt_only_divergence(self):
        """Bare "deepseek" is accepted by THIS module (see
        TestDeepseekVariants::test_bare_deepseek_is_its_own_identity_not_harness)
        but rejected by governance_emit._PROVIDER_RE. This is deliberate, not
        an oversight: no dispatch ever selects "--provider deepseek" (absent
        from provider_dispatch._IMPLEMENTED_PROVIDERS too), so the
        dispatch-time gate never needed to accept it -- the value only ever
        enters through a report body written by scout_prepass.py,
        gate_request_handler._request_deepseek, or a vnx_tagger.py-style
        classifier call, none of which pass through governance_emit at all.
        """
        assert not self._provider_re().match(ProviderIdentity.DEEPSEEK.value)

    def test_unknown_is_the_other_receipt_only_divergence(self):
        """"unknown" is a receipt-layer sentinel for "no source recorded an
        identity" -- there is no such thing as dispatching to an "unknown"
        provider, so governance_emit's dispatch-time gate correctly has no
        concept of it either.
        """
        assert not self._provider_re().match(ProviderIdentity.UNKNOWN.value)

    def test_no_further_divergence_exists(self):
        """Exhaustive check: DEEPSEEK and UNKNOWN are the ONLY two members
        the regex rejects. If this fails, a member was added or the regex
        changed without updating the divergence list above.
        """
        regex = self._provider_re()
        rejected = {m for m in ProviderIdentity if not regex.match(m.value)}
        assert rejected == {ProviderIdentity.DEEPSEEK, ProviderIdentity.UNKNOWN}


class TestReconcileWithDispatchSpecProvider:
    """dispatch_spec.Provider is the fleet's closed set for *dispatch-time*
    provider selection (docstring: "CLOSED set -- the ONLY legal provider
    strings"). Every member except AUTO (a capability-seam placeholder that
    is resolved to a concrete provider before anything is ever dispatched or
    receipted) must normalise to a consistent, documented ProviderIdentity.
    """

    def test_every_non_auto_member_normalises_without_raising(self):
        from dispatch_spec import Provider

        for member in Provider:
            if member == Provider.AUTO:
                continue
            # Must not raise -- every real dispatch-time provider selection
            # is a receipt-time-recognisable identity by construction.
            normalize_provider(member.value)

    def test_auto_is_excluded_by_design(self):
        """AUTO ("auto") is a capability-seam placeholder filled in by the
        router before planning -- it is never itself a receipt-worthy
        identity, so it correctly has no ProviderIdentity counterpart and is
        excluded from the loop above rather than silently swallowed.
        """
        from dispatch_spec import Provider

        assert Provider.AUTO.value not in {m.value for m in ProviderIdentity}
        with pytest.raises(UnrecognizedProviderError):
            normalize_provider(Provider.AUTO.value)

    def test_harness_lanes_map_to_themselves(self):
        from dispatch_spec import Provider

        assert normalize_provider(Provider.CLAUDE.value) is ProviderIdentity.CLAUDE
        assert normalize_provider(Provider.CODEX.value) is ProviderIdentity.CODEX
        assert normalize_provider(Provider.GEMINI.value) is ProviderIdentity.GEMINI
        assert normalize_provider(Provider.KIMI.value) is ProviderIdentity.KIMI
        assert (
            normalize_provider(Provider.DEEPSEEK_HARNESS.value)
            is ProviderIdentity.DEEPSEEK_HARNESS
        )
        assert (
            normalize_provider(Provider.GLM_HARNESS.value) is ProviderIdentity.GLM_HARNESS
        )
        assert (
            normalize_provider(Provider.LOCAL_GEMMA.value) is ProviderIdentity.LOCAL_GEMMA
        )

    def test_benchmark_baseline_litellm_compounds_map_consistently(self):
        """LITELLM_ZAI and LITELLM_MOONSHOT carry dispatch_spec's own
        "BENCHMARK-BASELINE ONLY" comment. LITELLM_DEEPSEEK carries no such
        comment (asymmetric on purpose -- see provider_identity.py's
        ProviderIdentity.DEEPSEEK docstring): it is a real, independent
        production lane, not benchmark-only, which is exactly why it does
        NOT get its own "litellm:deepseek" member here and instead folds
        into DEEPSEEK_HARNESS (matching the pre-existing OI-1111 lane
        resolution contract) while LITELLM_ZAI folds into GLM_HARNESS and
        LITELLM_MOONSHOT is preserved as its own distinct member.
        """
        from dispatch_spec import Provider

        assert (
            normalize_provider(Provider.LITELLM_DEEPSEEK.value)
            is ProviderIdentity.DEEPSEEK_HARNESS
        )
        assert (
            normalize_provider(Provider.LITELLM_ZAI.value) is ProviderIdentity.GLM_HARNESS
        )
        assert (
            normalize_provider(Provider.LITELLM_MOONSHOT.value)
            is ProviderIdentity.LITELLM_MOONSHOT
        )
