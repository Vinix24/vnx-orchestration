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

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple, Union

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


# --- required_stack: the second view, with the enforcement layer (P7, B10) -----------------

ENFORCEMENT_FILENAME = "governance_enforcement.yaml"
CI_GATE = "ci_gate"
CI_GATE_KEY = "VNX_CI_GATE_REQUIRED"
# The dial -> the gate it governs. ``ci_green_required`` maps to ``ci_gate``, the name
# review_gate_manager knows (OI-1265); ``ci`` is not a name it knows.
ENFORCEMENT_DIALS: Mapping[str, str] = MappingProxyType({
    "codex_gate_required": "codex_gate",
    "kimi_gate_required": "kimi_gate",
    "ci_green_required": CI_GATE,
})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_CHECK_RE = re.compile(r"^  (\w+):")
_LEVEL_RE = re.compile(r"^    level:\s*([^\s#]*)\s*(?:#.*)?$")


def enforcement_config_path(project_root: Optional[str] = None) -> Optional[Path]:
    """The first existing ``governance_enforcement.yaml``: ``$VNX_HOME/.vnx/``,
    ``$PROJECT_ROOT/.vnx/``, ``$VNX_HOME/`` (the order ``gate.sh`` searched), else ``None``."""
    home = os.environ.get("VNX_HOME", "")
    root = project_root if project_root is not None else os.environ.get("PROJECT_ROOT", "")
    candidates = []
    if home:
        candidates.append(Path(home) / ".vnx" / ENFORCEMENT_FILENAME)
    if root:
        candidates.append(Path(root) / ".vnx" / ENFORCEMENT_FILENAME)
    if home:
        candidates.append(Path(home) / ENFORCEMENT_FILENAME)
    return next((path for path in candidates if path.is_file()), None)


def _dial_level(raw: str, check: str, path: Path) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ReviewGateConfigError(
            f"{path}: checks.{check}.level={raw!r} is not an integer; refusing to guess the "
            "enforcement level of a review seat."
        ) from None


def _levelless(path: Path, check: str) -> ReviewGateConfigError:
    return ReviewGateConfigError(f"{path}: checks.{check} has no level; the enforcement level is unknown.")


def read_enforcement_dials(path: Optional[Path]) -> Dict[str, int]:
    """``{dial: level}`` for the ``checks.*_required`` dials in ``path`` (no PyYAML needed).

    ``{}`` when there is no config file. A dial whose level is missing or not an integer
    raises ``ReviewGateConfigError``: an unreadable level is not a level.
    """
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewGateConfigError(f"{path} cannot be read ({exc}); the enforcement dials are unknown.") from exc
    dials: Dict[str, int] = {}
    in_checks, current, seen_level = False, None, True
    for line in lines:
        stripped = line.rstrip()
        if stripped == "checks:":
            in_checks = True
            continue
        if not in_checks:
            continue
        if stripped and not stripped.startswith(" ") and not stripped.startswith("#"):
            break
        check = _CHECK_RE.match(stripped)
        if check:
            if current in ENFORCEMENT_DIALS and not seen_level:
                raise _levelless(path, current)
            current, seen_level = check.group(1), False
            continue
        level = _LEVEL_RE.match(stripped)
        if level:
            seen_level = True
            if current in ENFORCEMENT_DIALS:
                dials[current] = _dial_level(level.group(1), current, path)
    if current in ENFORCEMENT_DIALS and not seen_level:
        raise _levelless(path, current)
    return dials


def _stack_names(raw: Optional[str]) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def review_stack(
    project_id: Optional[str], resolution: ProfileResolution, resolver=None,
) -> Tuple[List[str], str]:
    """``review_stack`` as the door asks it, with its source: profile, else the registry chain."""
    contributed = value("review_stack", project_id, resolution=resolution, resolver=resolver)
    if contributed is not None:
        return _stack_names(str(contributed)), "profile"
    raw, source = config_registry.resolve_with_source(
        FIELD_REGISTRY_KEYS["review_stack"], project_id, resolver, use_default_resolver=resolver is None,
    )
    return _stack_names(raw), source


def _ci_gate_source(level: Optional[int], project_id: Optional[str], resolver) -> Optional[str]:
    """Why ``ci_gate`` is required, or ``None``: the dial at level >= 2, else a set
    ``VNX_CI_GATE_REQUIRED``. The registry default ("1") is not a request: the stack of a
    project with no dial never carried ``ci_gate`` here."""
    if level is not None and level >= 2:
        return "enforcement"
    raw, source = config_registry.resolve_with_source(
        CI_GATE_KEY, project_id, resolver, use_default_resolver=resolver is None,
    )
    if source != "default" and (raw or "").strip().lower() in _TRUTHY:
        return source
    return None


def required_stack(
    project_id: Optional[str] = None, *, resolution: Optional[ProfileResolution] = None, resolver=None,
    config_path: Optional[Path] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """``(names, provenance)``: the stack ``vnx gate`` verifies (B10).

    Starts from ``review_stack`` (what the door asks) and applies the ``checks.*_required``
    dials of ``governance_enforcement.yaml``: level >= 2 makes the seat mandatory, level 1
    is advisory and requires nothing, level 0 removes the seat. ``ci_gate`` joins when
    ``ci_green_required`` >= 2 or ``VNX_CI_GATE_REQUIRED`` is set true. ``provenance`` names
    the source of each returned name: ``profile``, ``enforcement``, ``override``,
    ``project_config``, ``env`` or ``default``.

    Raises :class:`GateProfileConfigError` for an invalid profile and ``ReviewGateConfigError``
    for an unreadable dial or an empty result: nothing is verified on a stack nobody can read.
    """
    resolution = resolution if resolution is not None else resolve(project_id, resolver=resolver)
    names, source = review_stack(project_id, resolution, resolver)
    dials = read_enforcement_dials(config_path if config_path is not None else enforcement_config_path())
    provenance = {name: source for name in names}
    for dial, gate in ENFORCEMENT_DIALS.items():
        level = dials.get(dial)
        if gate == CI_GATE or level is None:
            continue
        if level >= 2 and gate not in provenance:
            names.append(gate)
            provenance[gate] = "enforcement"
        elif level < 2 and gate in provenance:
            names.remove(gate)
            del provenance[gate]
    ci_source = _ci_gate_source(dials.get("ci_green_required"), project_id, resolver)
    if ci_source is not None and CI_GATE not in provenance:
        names.append(CI_GATE)
        provenance[CI_GATE] = ci_source
    if not names:
        raise ReviewGateConfigError(
            "the required gate stack is empty: the review stack and the enforcement dials name no gate. "
            "Refusing to verify nothing."
        )
    return names, provenance
