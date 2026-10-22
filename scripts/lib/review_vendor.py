"""review_vendor.py — who built a PR head, and whose model a reviewer is (R7, R7b).

Operator decision 2026-10-22 (dispatch dlv-9906a482950d), refined the same
evening (dispatch dlv-b4de838ac1ec): a review seat never gets a reviewer that
runs the SAME MODEL as the builder. A reviewer of the builder's vendor on a
different model may take the seat: a build by ``deepseek-flash`` may be read
by ``deepseek_gate`` on ``deepseek-v4-pro``, which lets a DeepSeek build be
reviewed by codex plus a DeepSeek seat while kimi is out, without spending
Claude quota. Both models are compared canonically
(``model_normalizer.comparable_model_name``: the phased-out
``deepseek-v4-flash`` is ``deepseek-flash``). Same vendor with either model
unknown fails closed: the reviewer is passed over and the hop names the
unknown model.

Measured on PR #2168 (the case that started R7): kimi was at its
weekly limit, ``deepseek_gate`` took the kimi seat, and round 5 of that PR was
built by ``deepseek-v4-pro`` (dispatch ``dlv-c089a8e32893-r5``, provider
``deepseek-harness``). DeepSeek reviewed DeepSeek, and nothing in the takeover
walk compared the two.

The vendor is the model maker. Both sides are read through
``provider_switch.family_of``, the fabric's one map from a provider, model or
gate identifier to its family, so an identifier is taught to the fabric once
and this module never keeps a second prefix table:

- a reviewer: ``codex_gate`` openai, ``kimi_gate`` moonshot, ``claude_gate``
  anthropic, ``deepseek_gate`` deepseek, ``glm_gate`` zhipu. A gate that calls
  no model (``ci_gate``, ``wiring_gate``, ``claude_github_optional``) has no
  vendor and is never passed over by this rule. Its model is
  ``gate_lane_contract.gate_model``, read at decision time.
- the builder: the ``provider`` and ``model`` of the dispatch spec of the
  dispatch the gate request names
  (``<data_dir>/dispatches/*/<dispatch_id>/dispatch-spec.json``, the lookup
  ``dispatch_identity.resolve_spec_role`` uses), in the store the review gates
  belong to. A placeholder model (``""``, ``default``, ``auto``, ``none``) means
  the lane's own default, read from the lane (``_lane_default_model``).

Never guessed. The builder's vendor is unknown, and the rule is not applied,
when the request names no dispatch, no spec is found, a spec cannot be read,
neither field names a maker, or the two fields name different makers (measured
2026-10-22: two specs in the vnx-dev store carry provider ``claude`` with model
``kimi``). Every one of those outcomes says why in :attr:`BuilderVendor.reason`,
and the seat records it (``builder_vendor``) so a reader sees whether the rule
was applied. A same-vendor reviewer that is allowed through carries
``rule: model_differs`` with both models on that record.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import gate_lane_contract
import provider_switch
from providers.model_normalizer import comparable_model_name

# Family (provider_switch) -> vendor, the model maker. A family with no entry
# here (local, openrouter, tooling, unresolved) names no maker.
VENDOR_BY_FAMILY: Dict[str, str] = {
    provider_switch.CLAUDE: "anthropic",
    provider_switch.CODEX: "openai",
    provider_switch.KIMI: "moonshot",
    provider_switch.DEEPSEEK: "deepseek",
    provider_switch.GLM: "zhipu",
    provider_switch.GEMINI: "google",
}

# A spec model value that names no model: the lane runs its own default.
_MODEL_PLACEHOLDERS = frozenset({"", "default", "auto", "none"})

# The takeover hop a reviewer gets when it runs the builder's model, or when it
# shares the builder's vendor and one of the two models is unknown: passed over
# exactly like an unavailable seat, under its own reason. R7 (dlv-9906a482950d)
# wrote ``vendor_matches_builder`` / ``vendor_match`` on request records; nothing
# in code reads those two strings, only the records on disk carry them.
MODEL_MATCH_REASON = "model_matches_builder"
MODEL_UNKNOWN_REASON = "model_unknown_for_same_vendor"
MODEL_MATCH_STATUS = "model_match"

RULE_APPLIED = "applied"
RULE_NOT_APPLIED = "not_applied"
# A reviewer of the builder's vendor on another model took the seat on purpose.
RULE_MODEL_DIFFERS = "model_differs"


def vendor_of(identifier: Optional[str]) -> Optional[str]:
    """The vendor ``identifier`` (a provider, model or gate name) names, or None."""
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    try:
        family = provider_switch.family_of(identifier)
    except provider_switch.UnknownProviderError:
        return None
    return VENDOR_BY_FAMILY.get(family)


def gate_vendor(gate: str) -> Optional[str]:
    """The vendor of the model behind review gate ``gate``; None for a gate that calls no model."""
    return vendor_of(gate)


def vendor_from_spec(provider: Optional[str], model: Optional[str]) -> Tuple[Optional[str], str]:
    """``(vendor, reason)`` for a builder that ran ``provider`` with ``model``.

    The provider and the model each name a maker or nothing. One that names a
    maker decides; two that name the same maker agree; two that name different
    makers are a contradiction and decide nothing.
    """
    provider_text = _text(provider)
    model_text = _text(model)
    by_provider = vendor_of(provider_text)
    by_model = None if model_text.lower() in _MODEL_PLACEHOLDERS else vendor_of(model_text)
    if by_provider and by_model and by_provider != by_model:
        return None, (
            f"provider {provider_text!r} names {by_provider} and model {model_text!r} names "
            f"{by_model}: the spec contradicts itself, the vendor is not guessed"
        )
    vendor = by_provider or by_model
    if vendor is None:
        return None, f"neither provider {provider_text!r} nor model {model_text!r} names a model vendor"
    return vendor, f"provider {provider_text!r} with model {model_text or '(none)'!r} is {vendor}"


@dataclass(frozen=True)
class BuilderVendor:
    """The builder of the head a review round is about, as far as it can be known."""

    dispatch_id: str
    vendor: Optional[str]
    reason: str
    provider: str = ""
    model: str = ""
    source: str = ""
    # The model the build ran, the spec's own when it names one, else the lane's
    # default (empty when neither can be named).
    effective_model: str = ""

    @property
    def applies(self) -> bool:
        return self.vendor is not None

    def to_record(self) -> Dict[str, Any]:
        """The ``builder_vendor`` field a seat carries on its payload and request record."""
        return {
            "dispatch_id": self.dispatch_id or None,
            "vendor": self.vendor,
            "provider": self.provider or None,
            "model": self.model or None,
            "builder_model": self.effective_model or None,
            "source": self.source or None,
            "rule": RULE_APPLIED if self.applies else RULE_NOT_APPLIED,
            "reason": self.reason,
        }


def resolve_builder_vendor(dispatch_id: Optional[str], *, data_dir: Path) -> BuilderVendor:
    """The vendor of the builder of ``dispatch_id``, read from its spec in ``data_dir``.

    ``data_dir`` is the store the review gates belong to, passed explicitly:
    never the store the environment names. Never raises.
    """
    did = _text(dispatch_id)
    if not did:
        return BuilderVendor("", None, "the review request names no dispatch, so the builder is unknown")
    if "/" in did or "\\" in did or did in (".", ".."):
        return BuilderVendor(did, None, f"dispatch id {did!r} is not a plain name; no spec is read for it")
    dispatches_dir = Path(data_dir) / "dispatches"
    specs = sorted(dispatches_dir.glob(f"*/{glob.escape(did)}/dispatch-spec.json"))
    if not specs:
        return BuilderVendor(did, None, f"no dispatch-spec.json for {did} under {dispatches_dir}")
    found = []
    for spec_path in specs:
        builder = _builder_from_spec_file(did, spec_path)
        if not builder.applies:
            return builder
        found.append(builder)
    vendors = sorted({builder.vendor for builder in found if builder.vendor})
    if len(vendors) > 1:
        return BuilderVendor(
            did, None,
            f"the specs of {did} name different vendors ({', '.join(vendors)}): not guessed",
            source=", ".join(builder.source for builder in found),
        )
    return found[0]


def vendor_match_hop(gate: str, builder: Optional[BuilderVendor]) -> Optional[Dict[str, Any]]:
    """The takeover hop for ``gate`` when it runs the builder's model, else None.

    A reviewer of another vendor, or of the builder's vendor on a different
    model, is not passed over. Same vendor with either model unknown is (fail
    closed), and the hop says which.
    """
    if builder is None or not builder.applies:
        return None
    reviewer_vendor = gate_vendor(gate)
    if reviewer_vendor is None or reviewer_vendor != builder.vendor:
        return None
    reviewer_model = gate_lane_contract.gate_model(gate) or ""
    unknown = [
        who for who, name in (("builder", builder.effective_model), ("reviewer", reviewer_model)) if not name
    ]
    if not unknown and comparable_model_name(builder.effective_model) != comparable_model_name(reviewer_model):
        return None
    return {
        "gate": gate,
        "reason": MODEL_UNKNOWN_REASON if unknown else MODEL_MATCH_REASON,
        "status": MODEL_MATCH_STATUS,
        "builder_model": builder.effective_model or None,
        "reviewer_model": reviewer_model or None,
        "detail": _model_match_detail(gate, reviewer_vendor, reviewer_model, builder, unknown),
    }


def same_vendor_record(gate: str, builder: Optional[BuilderVendor], record: Dict[str, Any]) -> Dict[str, Any]:
    """``record`` (a seat's ``builder_vendor``) as it reads when ``gate`` fills the seat.

    A reviewer of the builder's vendor that was not passed over runs another
    model than the builder: the record says so (``rule: model_differs``, both
    models), so a reader sees a DeepSeek seat on a DeepSeek build was a decision.
    Any other record comes back unchanged.
    """
    if builder is None or not builder.applies or gate_vendor(gate) != builder.vendor:
        return record
    return {
        **record,
        "rule": RULE_MODEL_DIFFERS,
        "builder_model": builder.effective_model or None,
        "reviewer_model": gate_lane_contract.gate_model(gate),
    }


def _model_match_detail(
    gate: str, reviewer_vendor: str, reviewer_model: str, builder: BuilderVendor, unknown: list,
) -> str:
    head = (
        f"{gate} is a {reviewer_vendor} reviewer on {reviewer_model or '(unknown model)'} and the head was "
        f"built by {builder.provider or '?'}/{builder.effective_model or '(unknown model)'} "
        f"({builder.vendor}, dispatch {builder.dispatch_id}): "
    )
    if unknown:
        return head + (
            f"the {' and '.join(unknown)} model is unknown, so it cannot be shown to differ from the "
            "builder's and the reviewer is passed over"
        )
    return head + "a reviewer never runs the model that built the head"


def _lane_default_model(provider: str) -> str:
    """The model the lane of ``provider`` runs when a spec names none, read from the lane; "" if unnamed."""
    name = provider.lower()
    try:
        if name == "deepseek-harness":
            from provider_spawns.deepseek_harness_spawn import DEFAULT_DEEPSEEK_HARNESS_MODEL
            return DEFAULT_DEEPSEEK_HARNESS_MODEL
        if name == "glm-harness":
            from provider_spawns.glm_harness_spawn import DEFAULT_GLM_HARNESS_MODEL
            return DEFAULT_GLM_HARNESS_MODEL
        if name == "kimi":
            from kimi_wrapper import DEFAULT_KIMI_MODEL
            return DEFAULT_KIMI_MODEL
        if name == "codex":
            from providers import provider_registry
            return provider_registry.resolve_codex_model()
    except (ImportError, ValueError):
        # vnx-silent-except: a lane default that cannot be read leaves the builder model unknown, which fails closed
        return ""
    return ""


def builder_model(provider: str, model: str) -> str:
    """The model a build with ``provider`` and spec ``model`` ran: the spec's, else the lane default."""
    if model.lower() in _MODEL_PLACEHOLDERS:
        return _lane_default_model(provider)
    return model


def _builder_from_spec_file(dispatch_id: str, spec_path: Path) -> BuilderVendor:
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return BuilderVendor(dispatch_id, None, f"{spec_path} cannot be read: {exc}", source=str(spec_path))
    if not isinstance(data, dict):
        return BuilderVendor(dispatch_id, None, f"{spec_path} is not a JSON object", source=str(spec_path))
    provider, model = _text(data.get("provider")), _text(data.get("model"))
    vendor, reason = vendor_from_spec(provider, model)
    return BuilderVendor(
        dispatch_id, vendor, reason, provider=provider, model=model, source=str(spec_path),
        effective_model=builder_model(provider, model),
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
