"""review_vendor.py — who built a PR head, and whose model a reviewer is (R7).

Operator decision 2026-10-22 (dispatch dlv-9906a482950d): a review seat never
gets a reviewer of the builder's vendor. Measured on PR #2168: kimi was at its
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
  vendor and is never passed over by this rule.
- the builder: the ``provider`` and ``model`` of the dispatch spec of the
  dispatch the gate request names
  (``<data_dir>/dispatches/*/<dispatch_id>/dispatch-spec.json``, the lookup
  ``dispatch_identity.resolve_spec_role`` uses), in the store the review gates
  belong to.

Never guessed. The builder's vendor is unknown, and the rule is not applied,
when the request names no dispatch, no spec is found, a spec cannot be read,
neither field names a maker, or the two fields name different makers (measured
2026-10-22: two specs in the vnx-dev store carry provider ``claude`` with model
``kimi``). Every one of those outcomes says why in :attr:`BuilderVendor.reason`,
and the seat records it (``builder_vendor``) so a reader sees whether the rule
was applied.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import provider_switch

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

# The takeover hop a reviewer of the builder's vendor gets: passed over exactly
# like an unavailable seat, under its own reason.
VENDOR_MATCH_REASON = "vendor_matches_builder"
VENDOR_MATCH_STATUS = "vendor_match"

RULE_APPLIED = "applied"
RULE_NOT_APPLIED = "not_applied"


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
    """The takeover hop for ``gate`` when it shares the builder's vendor, else None."""
    if builder is None or not builder.applies:
        return None
    reviewer_vendor = gate_vendor(gate)
    if reviewer_vendor is None or reviewer_vendor != builder.vendor:
        return None
    return {
        "gate": gate,
        "reason": VENDOR_MATCH_REASON,
        "status": VENDOR_MATCH_STATUS,
        "detail": (
            f"{gate} is a {reviewer_vendor} reviewer and the head was built by "
            f"{builder.provider or '?'}/{builder.model or '?'} ({builder.vendor}, dispatch "
            f"{builder.dispatch_id}): the vendor matched the builder, and a reviewer never "
            "shares the builder's vendor"
        ),
    }


def _builder_from_spec_file(dispatch_id: str, spec_path: Path) -> BuilderVendor:
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return BuilderVendor(dispatch_id, None, f"{spec_path} cannot be read: {exc}", source=str(spec_path))
    if not isinstance(data, dict):
        return BuilderVendor(dispatch_id, None, f"{spec_path} is not a JSON object", source=str(spec_path))
    provider, model = _text(data.get("provider")), _text(data.get("model"))
    vendor, reason = vendor_from_spec(provider, model)
    return BuilderVendor(dispatch_id, vendor, reason, provider=provider, model=model, source=str(spec_path))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
