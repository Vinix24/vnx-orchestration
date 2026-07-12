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

SUBSYSTEM COCKPIT METADATA. ``subsystem`` and ``status`` are pure display metadata for the
framework-status cockpit (`docs/core/SUBSYSTEMS.md`): which subsystem a flag belongs to, and that
subsystem's declared status. They carry no runtime behaviour — no read-site consults them, no
default changes because of them. ``status`` is one of ``ALLOWED_STATUSES``. Subsystems that have no
dedicated flag (e.g. ``phantom_guard``, ``dispatch-plan``) are not represented here; they resolve via
``CONFIG_REGISTRY_SUBSYSTEMS`` instead, so the cockpit's rowset is the union of both.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# The only status values a cockpit row may declare (dispatch: framework-status-audit-and-cockpit PR-1).
ALLOWED_STATUSES = frozenset(("LIVE", "PARK", "CUT", "ACTIVATE", "SCOPE", "COCKPIT"))


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
    subsystem: Optional[str] = None  # cockpit MAP grouping — display metadata only
    status: Optional[str] = None  # one of ALLOWED_STATUSES — display metadata only


def _e(key, type_, default, category, description, *, writable=True, approval=False, planned=False,
       subsystem=None, status=None):
    return ConfigEntry(key, type_, default, category, description, writable, approval, planned,
                        subsystem, status)


# The inventory. Defaults verified against the read-sites (codex review finding: defaults must
# match current code). All feature toggles are off ("0") today; the tagger provider is deepseek.
#
# subsystem/status backfill (framework-status-audit-and-cockpit PR-1): every entry below is tagged
# with the cockpit subsystem it belongs to and that subsystem's declared status. These are the
# flag-BACKED subsystems; the flag-LESS kernel subsystems (dispatch-plan, phantom_guard, etc.) live
# in CONFIG_REGISTRY_SUBSYSTEMS below so the two sets never collide on the same subsystem name.
CONFIG_REGISTRY: Dict[str, ConfigEntry] = {
    "VNX_SCOUT_PREPASS": _e(
        "VNX_SCOUT_PREPASS", "bool", "0", "intelligence",
        "Cheap-model scout recon pre-pass in the door (fail-open).",
        subsystem="cheap-recon-scout", status="LIVE"),
    "VNX_TAGGER_ENABLED": _e(
        "VNX_TAGGER_ENABLED", "bool", "0", "intelligence",
        "Persist-time LLM tagging over the closed VNX vocabulary.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_TAGGER_PROVIDER": _e(
        "VNX_TAGGER_PROVIDER", "string", "deepseek", "intelligence",
        "Provider for the LLM tagger (model-agnostic).",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_INTEL_RANK_THEN_BUDGET": _e(
        "VNX_INTEL_RANK_THEN_BUDGET", "bool", "0", "intelligence",
        "Rank intelligence candidates by tag-overlap, then budget.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_OUTCOME_GROUNDING_V2": _e(
        "VNX_OUTCOME_GROUNDING_V2", "bool", "0", "intelligence",
        "Junction-grounded confidence updates from receipts.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_HAIKU_CLASSIFY": _e(
        "VNX_HAIKU_CLASSIFY", "bool", "0", "intelligence",
        "Use Haiku for high-volume receipt classification.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_ROADMAP_AUTOPILOT": _e(
        "VNX_ROADMAP_AUTOPILOT", "bool", "0", "dispatch",
        "Autonomous roadmap auto-next loading (starts work).", approval=True,
        subsystem="horizon-planning", status="LIVE"),
    "VNX_HEADLESS_ROUTING": _e(
        "VNX_HEADLESS_ROUTING", "string", "0", "dispatch",
        "Headless dispatch routing mode.",
        subsystem="headless-dispatch-routing", status="LIVE"),
    "VNX_CI_GATE_REQUIRED": _e(
        "VNX_CI_GATE_REQUIRED", "bool", "0", "gate",
        "Require the CI gate before merge.", approval=True,
        subsystem="governance-enforcement-stack", status="PARK"),
    "VNX_WIRING_GATE_REQUIRED": _e(
        "VNX_WIRING_GATE_REQUIRED", "bool", "0", "gate",
        "Require the wiring gate.", approval=True,
        subsystem="governance-enforcement-stack", status="PARK"),
    "VNX_EVIDENCE_BOUND_GATE": _e(
        "VNX_EVIDENCE_BOUND_GATE", "enum", "advisory", "gate",
        "Evidence-bound merge gate mode: off | advisory | required. "
        "Advisory logs missing/invalid evidence but never blocks; required enforces evidence before merge. "
        "Default is advisory for D3 bootstrap.", approval=True,
        subsystem="evidence-bound-gate", status="PARK"),
    "VNX_PLAN_GATE_ENFORCE": _e(
        "VNX_PLAN_GATE_ENFORCE", "enum", "advisory", "gate",
        "Plan-first-gate enforcement mode: off | advisory | required (ADR-030). "
        "Advisory warns when a track-linked dispatch has an unresolved plan gate; required blocks it. "
        "The process env var overrides this; operator override VNX_OVERRIDE_PLAN_GATE=1. Default advisory.",
        approval=True,
        subsystem="plan-gate-panel", status="SCOPE"),
    "VNX_USE_CENTRAL_DB": _e(
        "VNX_USE_CENTRAL_DB", "enum", "", "dispatch",
        "Central-DB read mode (''=per-project | '1'=central | 'shadow'). Process-start routing — "
        "env-only, surfaced read-only: live-toggling would split reads across DBs mid-process.",
        writable=False,
        subsystem="central-db-routing", status="LIVE"),
    "VNX_USE_FEDERATION": _e(
        "VNX_USE_FEDERATION", "bool", "0", "intelligence",
        "Cross-project intelligence federation (NOT yet implemented).",
        writable=False, planned=True,
        subsystem="cross-project-federation", status="ACTIVATE"),

    # framework-status-audit-and-cockpit PR-2: net-new subsystem flags, registered as display
    # metadata only (§2.1 of the plan doc). No read-site consults these; registering them does not
    # change any gate/enforcement decision. VNX_EVIDENCE_BOUND_GATE and VNX_PLAN_GATE_ENFORCE
    # already existed before this PR (backfilled with subsystem/status in PR-1) and are NOT re-added.
    "VNX_GOVERNANCE_ENFORCED": _e(
        "VNX_GOVERNANCE_ENFORCED", "bool", "0", "gate",
        "Governance-enforcement-stack master switch (display metadata only; no read-site wired).",
        approval=True,
        subsystem="governance-enforcement-stack", status="PARK"),
    "VNX_LEARNING_LOOP_ENABLED": _e(
        "VNX_LEARNING_LOOP_ENABLED", "bool", "0", "intelligence",
        "Daily pattern learning / skill refinement / confidence-update loop.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE"),
    "VNX_DREAM_SCHEDULER_ENABLED": _e(
        "VNX_DREAM_SCHEDULER_ENABLED", "bool", "0", "intelligence",
        "Nightly memory consolidation + pending review dispatch.",
        subsystem="dream-consolidation", status="ACTIVATE"),
    "VNX_INJECTION_FEEDBACK_ENABLED": _e(
        "VNX_INJECTION_FEEDBACK_ENABLED", "bool", "0", "intelligence",
        "Instrument why intelligence injections are ignored before tuning generation.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE"),
    "VNX_PLAN_GATE_COMPLEX_ONLY": _e(
        "VNX_PLAN_GATE_COMPLEX_ONLY", "bool", "0", "gate",
        "Restrict the plan-gate panel to complex features (display metadata only; "
        "the scope-skip read-site is deferred to review-floor-enforcer).",
        # subsystem matches the docs/core/SUBSYSTEMS.md seed row
        # "plan-gate-task-class-scope" (framework-status-audit-and-cockpit PR-3
        # fix) — distinct from "plan-gate-panel" (VNX_PLAN_GATE_ENFORCE), so the
        # cockpit generator has exactly one canonical flag per ledger row.
        subsystem="plan-gate-task-class-scope", status="SCOPE"),
    "VNX_HASH_CHAIN_REQUIRED": _e(
        "VNX_HASH_CHAIN_REQUIRED", "bool", "0", "gate",
        "Tamper-evident NDJSON hash-chain requirement (display metadata only; no read-site wired).",
        approval=True,
        subsystem="receipt-hash-chain", status="PARK"),
    "VNX_ATTESTATION_REQUIRED": _e(
        "VNX_ATTESTATION_REQUIRED", "bool", "0", "gate",
        "SSH-signed PR attestation requirement (display metadata only; no read-site wired).",
        approval=True,
        subsystem="signed-attestation", status="PARK"),
    "VNX_MIGRATION_SYSTEM": _e(
        "VNX_MIGRATION_SYSTEM", "enum", "manifest", "dispatch",
        "Pinned selector recording which migration mechanism is active. Parked pending the "
        "migration-consolidation-and-tenancy-cut trigger.",
        writable=False,
        subsystem="migration-mechanisms", status="PARK"),
}

# Flag-LESS subsystems from the cockpit ledger (docs/core/SUBSYSTEMS.md) — kernel/meta subsystems
# with no operator-toggleable flag. Kept disjoint from the subsystem names used in CONFIG_REGISTRY
# above so a future union view (`vnx subsystems`, PR-3) never double-represents one subsystem.
CONFIG_REGISTRY_SUBSYSTEMS: Dict[str, dict] = {
    "provider-routing": {
        "status": "LIVE",
        "description": "Model/provider selection, constraint solving, fallback order.",
    },
    "git-grounded-reconcile": {
        "status": "LIVE",
        "description": "Per-project canonical stores, git-provenance linking, no shared-state fork.",
    },
    "phantom_guard": {
        "status": "LIVE",
        "description": "Receipt deduplication and replay protection.",
    },
    "tmux-operational-scar": {
        "status": "LIVE",
        "description": "Terminal/session lifecycle, session handover, F1.1 safe linkage.",
    },
    "zero-llm-injection": {
        "status": "LIVE",
        "description": "No prompt injection via environment or receipts; strict input boundaries.",
    },
    "dispatch-plan": {
        "status": "LIVE",
        "description": "Single-entry dispatch door, dispatch-plan reconciliation.",
    },
    "test-suite": {
        "status": "LIVE",
        "description": "Pytest + integration coverage for kernel and cockpit.",
    },
    "within-db-tenancy": {
        "status": "PARK",
        "description": "Composite (project_id, id) keys inside per-project DBs. Removal PARKed "
                        "pending per-table central-DB safety proof.",
    },
    "docs-bloat": {
        "status": "CUT",
        "description": "Comparisons, stale archive, marketing docs inflating docs/ count.",
    },
    "subsystem-cockpit": {
        "status": "COCKPIT",
        "description": "SUBSYSTEMS.md + config_registry + vnx subsystems + dashboard tile.",
    },
    "effectiveness-probe-framework": {
        "status": "COCKPIT",
        "description": "Generic \"does it produce crap?\" probes per subsystem.",
    },
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
            "subsystem": entry.subsystem,
            "status": entry.status,
        })
    return out
