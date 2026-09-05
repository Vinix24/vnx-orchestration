"""provider_identity.py — the single closed vocabulary for a receipt's
``provider`` field (PRD bewijsketen-en-afdwinging, Golf 1a).

Reconciles two vocabularies that disagree today:

  1. ``governance_emit._PROVIDER_RE`` — dispatch-time, fail-loud (raises
     ValueError on mismatch). Accepts ``claude|codex|gemini|kimi|
     deepseek-harness|glm-harness|litellm(:sub(:alias)?)?|local-gemma``.
     Does NOT accept bare ``deepseek`` or ``unknown``.
  2. ``report_to_receipt_converter._CANONICAL_PROVIDER_LANE`` — receipt-time.
     Accepts the same set PLUS bare ``deepseek`` as its own canonical value,
     and falls back to ``return p`` (the raw string, verbatim) for anything
     it doesn't recognise — a fail-OPEN passthrough. That passthrough is how
     four free-text fragments — a literal ``` `. ``` and a torn-off
     instruction sentence among them — reached ``t0_receipts.ndjson``
     unchanged on 2026-08-09.

This module is the closed, fail-loud replacement for vocabulary #2, wired in
via ``report_to_receipt_converter._normalise_provider`` ->
:func:`normalize_provider`. Vocabulary #1 (``governance_emit.py``) is
untouched here by design — migrating its dispatch-time validation onto this
same enum is a follow-up PR, not this one.

Where this module DELIBERATELY diverges from ``governance_emit._PROVIDER_RE``
(and why), see :class:`ProviderIdentity`'s per-member docstring and
``tests/test_provider_identity.py::TestReconcileWithGovernanceEmit`` — which
asserts that DEEPSEEK and UNKNOWN are the *only* two divergences and pins the
reason for each.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

# Ensure sibling scripts/lib modules resolve even when a caller imports this
# module by path without scripts/lib already on sys.path (mirrors the same
# guard in governance_emit.py / report_to_receipt_converter.py).
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from governance_emit import _PROVIDER_RE  # noqa: E402


class ProviderIdentity(str, Enum):
    """CLOSED set — the only legal values for a receipt's ``provider`` field.

    Provenance per member:

    CLAUDE, CODEX, GEMINI, KIMI
        Native CLI lanes. Members of both ``dispatch_spec.Provider`` and
        ``governance_emit._PROVIDER_RE``.

    DEEPSEEK_HARNESS = "deepseek-harness"
        Claude-CLI harness, ``ANTHROPIC_BASE_URL`` redirected to the DeepSeek
        endpoint, own key + hardening
        (``provider_spawns/deepseek_harness_spawn.py``;
        ``~/.claude/rules/provider-constraints.md``'s
        ``deepseek-harness-subscription-blocked``). Member of both
        ``dispatch_spec.Provider`` and ``governance_emit._PROVIDER_RE``.
        The smart-router's route decision for an auto-routed
        ``deepseek-*`` model resolves via ``smart_router.parse_route_model_id``
        to ``litellm:deepseek:<model_id>`` — :func:`normalize_provider` folds
        that (and the bare ``litellm:deepseek`` prefix, and free-text like
        ``"deepseek (harness, key-auth)"``) back into THIS member, matching
        the pre-existing, tested lane-identity-resolution contract (OI-1111:
        ``tests/test_report_to_receipt_converter.py::
        TestLaneIdentityResolution::test_deepseek_harness_lane_wins``) — not
        changed by this module.

    DEEPSEEK = "deepseek"
        A SEPARATE, independently real DeepSeek lane: the cheap key-auth
        sub-provider under the generic litellm multi-provider dispatcher
        (``provider_dispatch.py``'s ``sub_provider="deepseek"`` /
        ``_SUB_PROVIDER_KEY_REQS["deepseek"] = "DEEPSEEK_API_KEY"``).
        This exact bare spelling is what ``scout_prepass.py``'s sidecar
        schema, ``gate_request_handler._request_deepseek``'s review-gate
        payload, and ``vnx_tagger.py``'s ``VNX_TAGGER_PROVIDER`` default
        ("the cheap key-auth DeepSeek-Flash" lane) all stamp directly — none
        of them go through a route decision, so their report bodies reach
        :func:`normalize_provider` via the body-fallback path only.
        ``governance_emit._PROVIDER_RE`` does NOT accept this literal (only
        the ``deepseek-harness`` literal, or the compound
        ``litellm:deepseek(:model)?`` form) — a DELIBERATE divergence, not an
        oversight: no dispatch ever selects ``--provider deepseek`` (it is
        absent from ``provider_dispatch._IMPLEMENTED_PROVIDERS`` too), so
        governance_emit's dispatch-time gate never needed to accept it. The
        producers above write straight into a report body, a surface
        governance_emit's regex was never wired to validate. See
        ``TestReconcileWithGovernanceEmit`` for the pinned assertion.

    GLM_HARNESS = "glm-harness"
        Claude-CLI harness -> local litellm proxy (:4141) -> OpenRouter/zai.
        Member of both ``dispatch_spec.Provider`` and
        ``governance_emit._PROVIDER_RE``. The smart-router's route decision
        for an auto-routed ``glm-*`` model resolves to the sub-provider key
        ``litellm:zai`` (its own internal lookup spelling) —
        :func:`normalize_provider` folds that back into THIS member so the
        ledger counts one GLM provider, not two (pre-existing, tested:
        ``test_glm_harness_lane_wins_over_body_sonnet_claude``).

    LOCAL_GEMMA = "local-gemma"
        Local model runner. Member of both ``dispatch_spec.Provider`` and
        ``governance_emit._PROVIDER_RE``.

    LITELLM = "litellm"
        Bare generic litellm dispatch (env-driven sub-provider selection via
        ``VNX_LITELLM_MODEL``; listed in
        ``provider_dispatch._IMPLEMENTED_PROVIDERS``) AND the catch-all for
        any OTHER well-formed ``litellm:<sub>(:<alias>)?`` compound this
        module has no specific rule for (bedrock, ollama, anthropic, the
        openrouter-arbitrary lane). Recognised by shape via the SAME
        ``governance_emit._PROVIDER_RE`` this module reconciles against, so a
        new litellm sub-provider never needs a matching edit here just to
        stop raising.

    LITELLM_MOONSHOT = "litellm:moonshot"
        ``dispatch_spec.Provider.LITELLM_MOONSHOT`` — "BENCHMARK-BASELINE
        ONLY" per that enum's own comment: the Moonshot-API route
        ``~/.claude/rules/provider-constraints.md``'s ``kimi-via-cli-only``
        (blocking) forbids in production ("Kimi K2/K2.6 alleen via kimi CLI
        OAuth, niet via Moonshot API"). Kept as its OWN member instead of
        folding into KIMI, which a naive ``"kimi" in value`` substring match
        would do (a benchmark lane key can legitimately read
        ``litellm:moonshot:kimi-k2-6``): collapsing it would erase the one
        signal that lets governance tell a sanctioned CLI-OAuth kimi receipt
        apart from a forbidden-route attempt.

    UNKNOWN = "unknown"
        Explicit sentinel: the provider field was genuinely blank/absent.
        This is NOT a catch-all for unparseable text — see
        :class:`UnrecognizedProviderError` for that path. 333 live
        ``task_complete``/non-pytest receipts carry this value (measured
        2026-09-05); it is the honest admission "no source recorded an
        identity", not corruption.
    """

    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"
    KIMI = "kimi"
    DEEPSEEK_HARNESS = "deepseek-harness"
    DEEPSEEK = "deepseek"
    GLM_HARNESS = "glm-harness"
    LOCAL_GEMMA = "local-gemma"
    LITELLM = "litellm"
    LITELLM_MOONSHOT = "litellm:moonshot"
    UNKNOWN = "unknown"


class UnrecognizedProviderError(ValueError):
    """Raised by :func:`normalize_provider` when *raw* matches no known identity.

    Replaces ``report_to_receipt_converter._normalise_provider``'s old
    fail-open ``return p`` passthrough. Callers must not swallow this into a
    silently-invented value of their own — the sanctioned handling (see
    ``report_to_receipt_converter._build_receipt_from_report_core``) is to
    book it as an explicit ``"unrecognized_provider"`` contract violation
    (``event_type="report_contract_invalid"``), which still writes a receipt
    (so the report is never silently re-processed forever) without ever
    inventing a provider string that no source actually reported.
    """

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(f"unrecognized provider value: {raw!r}")


_EXACT: Dict[str, "ProviderIdentity"] = {member.value: member for member in ProviderIdentity}


def normalize_provider(raw: Optional[str]) -> ProviderIdentity:
    """Map any known provider spelling to its closed :class:`ProviderIdentity`.

    Fail-loud: raises :class:`UnrecognizedProviderError` on anything this
    vocabulary cannot place — never a passthrough of the raw string.

    Resolution order (case-insensitive, first match wins):
      1. Blank/whitespace-only/``None`` -> UNKNOWN.
      2. Exact match against a closed-set value -> that member.
      3. Contains "deepseek" (any casing, any surrounding text — covers the
         ``litellm:deepseek(:model)?`` compound and free-text self-reports
         like "deepseek (harness, key-auth)") -> DEEPSEEK_HARNESS. Bare
         "deepseek" never reaches this branch — it is caught by the exact
         match in step 2.
      4. Starts with "litellm:moonshot" -> LITELLM_MOONSHOT. Checked BEFORE
         the "kimi" substring match in step 5 so a benchmark lane key like
         "litellm:moonshot:kimi-k2-6" is never laundered into looking like
         the sanctioned kimi lane.
      5. Contains "kimi" -> KIMI.
      6. Contains "glm", or starts with "litellm:zai" -> GLM_HARNESS.
      7. Shape matches ``governance_emit._PROVIDER_RE`` (any well-formed
         ``litellm:<sub>(:<alias>)?`` this module has no specific rule for)
         -> LITELLM.
      8. Otherwise -> raise UnrecognizedProviderError.
    """
    if raw is None or not str(raw).strip():
        return ProviderIdentity.UNKNOWN

    p = str(raw).strip()
    pl = p.lower()

    exact = _EXACT.get(pl)
    if exact is not None:
        return exact

    if "deepseek" in pl:
        return ProviderIdentity.DEEPSEEK_HARNESS
    if pl.startswith("litellm:moonshot"):
        return ProviderIdentity.LITELLM_MOONSHOT
    if "kimi" in pl:
        return ProviderIdentity.KIMI
    if "glm" in pl or pl.startswith("litellm:zai"):
        return ProviderIdentity.GLM_HARNESS
    if _PROVIDER_RE.match(pl):
        return ProviderIdentity.LITELLM

    raise UnrecognizedProviderError(p)
