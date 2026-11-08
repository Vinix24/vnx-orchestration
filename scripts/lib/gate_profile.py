"""The gate profile: one fail-closed read function (track gate-profiel-per-project, P1).

A project chooses ONE profile (``VNX_GOVERNANCE_PROFILE``: ``light``, ``standard`` or
``heavy``) and that choice sets the review stack, the review round cap, the takeover
chain and the plan-gate seats together. This module is the only place that reads the key
and the only place that holds the table.

Fail-closed (B13, the binding condition of the plan-gate tiebreaker of 2026-11-08):

- absent, ``None``, empty or whitespace only is "not set" and means ``standard``;
- after ONE ``strip()`` exactly ``light``, ``standard`` or ``heavy`` is valid
  (case-sensitive: ``" heavy "`` is valid, ``"Heavy"`` is not);
- anything else (``zwaar``, ``Heavy``, ``high``, ``standard heavy``, a typo) raises
  :class:`GateProfileConfigError` carrying the raw value, its source and the reason.
  There is no fallback to ``standard`` for a value that failed: that would quietly
  weaken the governance the project asked for.

Contribution (B3, B4): ``standard`` contributes nothing, so every reader keeps its own
chain and the behaviour of today is a property of the construction. ``light`` and
``heavy`` contribute the value of their column, but never over a value an operator set
for the field's own key through ``VNX_OVERRIDE_*`` or a ``project_config`` row.

P1 wires the review stack only (``review_seats``); P5 gives ``light`` a one-seat stack and
refuses it below the safety floor. ``round_cap``, ``takeover_chain`` and
``plan_gate_seats`` are provided here with their tests; P3 and P4 read them.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Union

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _path in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import config_registry
import config_runtime
from dispatch_spec import ReviewGateConfigError

PROFILE_KEY = "VNX_GOVERNANCE_PROFILE"
PROFILES = ("light", "standard", "heavy")
DEFAULT_PROFILE = "standard"

FIELDS = ("review_stack", "round_cap", "takeover_chain", "plan_gate_seats")

Value = Union[str, int]

# The full 3 x 4 table. The ``standard`` column is not a design: it is the value measured
# on origin/main (config_registry VNX_DEFAULT_REVIEW_STACK, VNX_REVIEW_ROUND_CAP,
# VNX_REVIEW_GATE_TAKEOVER_CHAIN; configs/plan_gate_panel.yaml default_seat only), and a
# test pins it to those defaults. ``light.review_stack`` is ``codex_gate`` alone (P5): the
# one value that weakens a review. It lands in the same change that refuses ``light`` on a
# signal path (B2, ``review_seat_evaluation.light_floor_refusal``, ADR-008 amendment), so the
# stack and its floor cannot be separated.
PROFILE_TABLE: Mapping[str, Mapping[str, Value]] = MappingProxyType({
    "light": MappingProxyType({
        "review_stack": "codex_gate",
        "round_cap": 2,
        "takeover_chain": "codex_gate,kimi_gate,claude_gate",
        "plan_gate_seats": 1,
    }),
    "standard": MappingProxyType({
        "review_stack": "codex_gate,kimi_gate",
        "round_cap": 3,
        "takeover_chain": "codex_gate,kimi_gate,claude_gate,deepseek_gate",
        "plan_gate_seats": 1,
    }),
    "heavy": MappingProxyType({
        "review_stack": "codex_gate,kimi_gate,claude_gate",
        "round_cap": 5,
        "takeover_chain": "codex_gate,kimi_gate,claude_gate,deepseek_gate",
        "plan_gate_seats": 2,
    }),
})

# The registry key each field overrides. ``plan_gate_seats`` has none: it is read from
# configs/plan_gate_panel.yaml, which has no operator override layer.
FIELD_REGISTRY_KEYS: Mapping[str, Optional[str]] = MappingProxyType({
    "review_stack": "VNX_DEFAULT_REVIEW_STACK",
    "round_cap": "VNX_REVIEW_ROUND_CAP",
    "takeover_chain": "VNX_REVIEW_GATE_TAKEOVER_CHAIN",
    "plan_gate_seats": None,
})

_OPERATOR_SOURCES = frozenset({"override", "project_config"})


class GateProfileConfigError(ReviewGateConfigError):
    """``VNX_GOVERNANCE_PROFILE`` holds a value that is not light, standard or heavy.

    A ``ReviewGateConfigError``, so every reader that already refuses an unreadable
    review configuration (the dispatch door) refuses this one the same way. Carries
    ``raw`` (the value exactly as read), ``source`` and ``reason``.
    """

    def __init__(self, raw: object, source: str, reason: str) -> None:
        self.raw = raw
        self.source = source
        self.reason = reason
        super().__init__(
            f"{PROFILE_KEY}={raw!r} (source: {source}) is refused: {reason}. "
            f"Legal values: {', '.join(PROFILES)}. Refusing rather than falling back to "
            f"{DEFAULT_PROFILE!r}, which would quietly change the governance the project asked for."
        )


@dataclass(frozen=True)
class ProfileResolution:
    """The profile in force, where it came from, and what it contributes per field.

    ``values`` holds the contribution per field: the profile's column for ``light`` and
    ``heavy``, ``None`` for every field under ``standard`` (B3). The whole table is
    :data:`PROFILE_TABLE`.
    """

    profile: str
    source: str
    raw: Optional[str]
    values: Mapping[str, Optional[Value]]

    def value(self, field: str) -> Optional[Value]:
        """The contribution for one field; ``None`` means "no contribution, use your own chain"."""
        if field not in FIELDS:
            raise KeyError(f"unknown gate-profile field {field!r}; legal: {', '.join(FIELDS)}")
        return self.values[field]


def _validated(raw: object, source: str) -> str:
    """The profile name for a raw value that is set; raises for anything not exactly legal."""
    if not isinstance(raw, str):
        raise GateProfileConfigError(raw, source, f"not text but {type(raw).__name__}")
    name = raw.strip()
    if name not in PROFILES:
        raise GateProfileConfigError(raw, source, "not one of the legal profile names (case-sensitive)")
    return name


def _contribution(profile: str) -> Mapping[str, Optional[Value]]:
    if profile == DEFAULT_PROFILE:
        return MappingProxyType({field: None for field in FIELDS})
    return MappingProxyType(dict(PROFILE_TABLE[profile]))


def resolve(project_id: Optional[str] = None, *, resolver=None) -> ProfileResolution:
    """The gate profile in force for this process's project (or ``project_id``), fresh.

    ``resolver`` reads the profile key from that store instead of the process's.

    Raises :class:`GateProfileConfigError` for a set value that is not a legal profile.
    """
    config_runtime.autowire()
    raw, source = config_registry.resolve_with_source(
        PROFILE_KEY, project_id, resolver, use_default_resolver=resolver is None,
    )
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ProfileResolution(DEFAULT_PROFILE, source if source == "unset" else "default", None,
                                 _contribution(DEFAULT_PROFILE))
    name = _validated(raw, source)
    return ProfileResolution(name, source, raw, _contribution(name))


def value(
    field: str, project_id: Optional[str] = None, *, resolution: Optional[ProfileResolution] = None,
    resolver=None,
) -> Optional[Value]:
    """The profile's contribution for ``field``, or ``None`` for "use your own chain".

    ``None`` as well when the operator set the field's own registry key through
    ``VNX_OVERRIDE_*`` or a ``project_config`` row: that choice outranks the profile (B4).
    A caller that already holds the resolution passes it, so the key is read once.
    """
    resolution = resolution if resolution is not None else resolve(project_id, resolver=resolver)
    contributed = resolution.value(field)
    key = FIELD_REGISTRY_KEYS[field]
    if contributed is None or key is None:
        return contributed
    _, own_source = config_registry.resolve_with_source(
        key, project_id, resolver, use_default_resolver=resolver is None,
    )
    return None if own_source in _OPERATOR_SOURCES else contributed
