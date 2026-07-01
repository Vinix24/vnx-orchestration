"""config_registry — the single source of truth for operator-toggleable VNX config (P0).

The dashboard control-plane needs a persistent, audited, per-project config store the runtime
honours. This module is its foundation: the flag inventory (which flags are operator-facing, their
type, their default, who may flip them) + the resolution precedence the runtime reads at decision
time.

SCOPE — only operator FEATURE toggles live here. Paths (VNX_DATA_DIR, VNX_HOME), provider-model
pins (VNX_CODEX_MODEL…), timeouts, internal plumbing, and the `VNX_OVERRIDE_*` constraint brakes
are deliberately OUT: they stay env-only and are never UI-settable.

DEFAULTS MIRROR THE CURRENT CODE. Every default here is the literal fallback the read-site uses
today (e.g. `VNX_CI_GATE_REQUIRED` defaults to "0" — off — exactly as `review_gate_manager.py`
reads it). Changing a default here must never change runtime behaviour vs the env-only world.

Precedence (highest first), implemented by `get()`:
  1. ``VNX_OVERRIDE_<BARE>`` env var — the operator emergency brake (e.g. VNX_OVERRIDE_SCOUT_PREPASS).
  2. ``project_config`` DB value — the UI-set value (wired in a later PR; injected via ``db_resolver``).
  3. ``VNX_<BARE>`` env var — the process-start value (today's behaviour; never broken).
  4. the registry default.

Until the DB layer lands, ``db_resolver`` is None and step 2 is skipped — so this module is a
behaviour-preserving overlay on the env-only world.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    type: str  # "bool" | "string" | "enum"
    default: str
    category: str  # "intelligence" | "dispatch" | "gate"
    description: str
    writable_from_ui: bool
    requires_approval: bool
    planned: bool = False  # exists in the registry/UI but not yet a live runtime flag


def _e(key, type_, default, category, description, *, writable=True, approval=False, planned=False):
    return ConfigEntry(key, type_, default, category, description, writable, approval, planned)


# The inventory. Defaults verified against the read-sites (codex review finding: defaults must
# match current code). All feature toggles are off ("0") today; the tagger provider is deepseek.
CONFIG_REGISTRY: Dict[str, ConfigEntry] = {
    "VNX_SCOUT_PREPASS": _e(
        "VNX_SCOUT_PREPASS", "bool", "0", "intelligence",
        "Cheap-model scout recon pre-pass in the door (fail-open)."),
    "VNX_TAGGER_ENABLED": _e(
        "VNX_TAGGER_ENABLED", "bool", "0", "intelligence",
        "Persist-time LLM tagging over the closed VNX vocabulary."),
    "VNX_TAGGER_PROVIDER": _e(
        "VNX_TAGGER_PROVIDER", "string", "deepseek", "intelligence",
        "Provider for the LLM tagger (model-agnostic)."),
    "VNX_INTEL_RANK_THEN_BUDGET": _e(
        "VNX_INTEL_RANK_THEN_BUDGET", "bool", "0", "intelligence",
        "Rank intelligence candidates by tag-overlap, then budget."),
    "VNX_OUTCOME_GROUNDING_V2": _e(
        "VNX_OUTCOME_GROUNDING_V2", "bool", "0", "intelligence",
        "Junction-grounded confidence updates from receipts."),
    "VNX_HAIKU_CLASSIFY": _e(
        "VNX_HAIKU_CLASSIFY", "bool", "0", "intelligence",
        "Use Haiku for high-volume receipt classification."),
    "VNX_ROADMAP_AUTOPILOT": _e(
        "VNX_ROADMAP_AUTOPILOT", "bool", "0", "dispatch",
        "Autonomous roadmap auto-next loading (starts work).", approval=True),
    "VNX_HEADLESS_ROUTING": _e(
        "VNX_HEADLESS_ROUTING", "string", "0", "dispatch",
        "Headless dispatch routing mode."),
    "VNX_CI_GATE_REQUIRED": _e(
        "VNX_CI_GATE_REQUIRED", "bool", "0", "gate",
        "Require the CI gate before merge.", approval=True),
    "VNX_WIRING_GATE_REQUIRED": _e(
        "VNX_WIRING_GATE_REQUIRED", "bool", "0", "gate",
        "Require the wiring gate.", approval=True),
    "VNX_USE_CENTRAL_DB": _e(
        "VNX_USE_CENTRAL_DB", "enum", "", "dispatch",
        "Central-DB read mode (''=per-project | '1'=central | 'shadow'). Process-start routing — "
        "env-only, surfaced read-only: live-toggling would split reads across DBs mid-process.",
        writable=False),
    "VNX_USE_FEDERATION": _e(
        "VNX_USE_FEDERATION", "bool", "0", "intelligence",
        "Cross-project intelligence federation (NOT yet implemented).",
        writable=False, planned=True),
}

# A resolver for the per-project DB layer (step 2). Signature: (project_id, key) -> str | None.
# None until the project_config DAO is wired (a later PR), so step 2 is a no-op today.
DbResolver = Callable[[Optional[str], str], Optional[str]]
_db_resolver: Optional[DbResolver] = None


def set_db_resolver(resolver: Optional[DbResolver]) -> None:
    """Wire the per-project DB layer (step 2 of the precedence chain)."""
    global _db_resolver
    _db_resolver = resolver


# The project a single-tenant runtime process resolves against when a caller does not pass an
# explicit project_id. Wired by config_runtime.autowire() at runtime startup; None until then, so
# read-sites that omit project_id behave exactly as the env-only world (the resolver gets None →
# no DB lookup). The dashboard passes project_id explicitly and never relies on this.
_default_project_id: Optional[str] = None


def set_default_project_id(project_id: Optional[str]) -> None:
    """Set the implicit project_id used by get()/get_bool() when the caller omits one."""
    global _default_project_id
    _default_project_id = project_id


def _bare(key: str) -> str:
    return key[len("VNX_"):] if key.startswith("VNX_") else key


def get(key: str, project_id: Optional[str] = None) -> Optional[str]:
    """Resolve a config value via the precedence chain. Returns the registry default (or None for
    an unknown key) when nothing overrides it. Never raises."""
    entry = CONFIG_REGISTRY.get(key)
    # 1. operator emergency-brake override (always wins, even for unknown keys)
    override = os.environ.get(f"VNX_OVERRIDE_{_bare(key)}")
    if override is not None:
        return override
    # 2. per-project DB value. Resolve the project: explicit arg wins, else the process default.
    if _db_resolver is not None:
        pid = project_id if project_id is not None else _default_project_id
        try:
            db_val = _db_resolver(pid, key)
        except Exception:
            db_val = None
        if db_val is not None:
            return db_val
    # 3. process-start env value (today's behaviour, never broken)
    env_val = os.environ.get(key)
    if env_val is not None:
        return env_val
    # 4. registry default
    return entry.default if entry is not None else None


_TRUTHY = frozenset(("1", "true", "yes", "on"))


def get_bool(key: str, project_id: Optional[str] = None) -> bool:
    """Bool view of get(): true for canonical truthy values (1/true/yes/on, case-insensitive).

    Applies to every source the precedence chain returns (VNX_OVERRIDE_* env vars, regular env
    vars, and per-project DB values): any truthy spelling resolves True, not just the literal "1".
    """
    val = get(key, project_id)
    if val is None:
        return False
    return val.strip().lower() in _TRUTHY


def all_effective(project_id: Optional[str] = None) -> List[dict]:
    """Every registry flag with its effective value + provenance — for the config API/UI."""
    out: List[dict] = []
    for key, entry in CONFIG_REGISTRY.items():
        value = get(key, project_id)
        out.append({
            "key": key,
            "type": entry.type,
            "category": entry.category,
            "description": entry.description,
            "default": entry.default,
            "value": value,
            "is_default": value == entry.default,
            "writable_from_ui": entry.writable_from_ui,
            "requires_approval": entry.requires_approval,
            "planned": entry.planned,
        })
    return out
