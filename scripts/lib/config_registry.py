"""config_registry — the single source of truth for operator-toggleable VNX config (P0).

The dashboard control-plane needs a persistent, audited, per-project config store the runtime
honours. This module is its foundation: the flag inventory (which flags are operator-facing, their
type, their default, who may flip them) + the resolution precedence the runtime reads at decision
time.

SCOPE — only operator FEATURE toggles live here. Paths (VNX_DATA_DIR, VNX_HOME), provider-model
pins (VNX_CODEX_MODEL…), timeouts, internal plumbing, and the `VNX_OVERRIDE_*` constraint brakes
are deliberately OUT: they stay env-only and are never UI-settable.

DEFAULTS MIRROR THE CURRENT CODE. Every default here is the literal fallback the read-site uses
today. Most feature toggles below default "0" (off); `VNX_CI_GATE_REQUIRED` is the exception,
activated "0" -> "1" on 23-08 (OI-1385) once its 5-read-site enforcement chain was proven live
(PR #1628) and the obligation-runner's bounded-pending handling for a temporarily-unavailable
gate was confirmed on main. Changing a default here changes runtime behaviour for any read-site
consulting it with no env/DB override set — that is the intended mechanism for a deliberate
activation like this one, not an accident to guard against.

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
    # OI-1385: the cockpit's canonical-flag-per-subsystem selection (canonical_flags()
    # below) must be an EXPLICIT decision, not "whichever entry is last in this dict".
    # read_site_wired=False means NO production code consults this flag (a fact, not a
    # display opinion) — such an entry can never be its subsystem's canonical flag,
    # structurally, so a flag that later gains a real read-site becomes eligible again
    # without touching any selection logic. cockpit_canonical=True is the tie-breaker
    # when a subsystem has more than one read_site_wired entry; exactly one must be set,
    # or canonical_flags() raises rather than silently picking by dict order.
    read_site_wired: bool = True
    cockpit_canonical: bool = False


def _e(key, type_, default, category, description, *, writable=True, approval=False, planned=False,
       subsystem=None, status=None, read_site_wired=True, cockpit_canonical=False):
    return ConfigEntry(key, type_, default, category, description, writable, approval, planned,
                        subsystem, status, read_site_wired, cockpit_canonical)


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
        subsystem="cheap-recon-scout", status="ACTIVATE"),
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
        subsystem="horizon-planning", status="ACTIVATE"),
    "VNX_HEADLESS_ROUTING": _e(
        "VNX_HEADLESS_ROUTING", "string", "0", "dispatch",
        "Headless dispatch routing mode.",
        subsystem="headless-dispatch-routing", status="ACTIVATE"),
    "VNX_DEFAULT_REVIEW_STACK": _e(
        "VNX_DEFAULT_REVIEW_STACK", "string", "gemini_review,codex_gate,claude_github_optional", "gate",
        "Comma-separated default review-gate stack (dispatch 20260823-beta2-e, OI-1435). "
        "Lets an operator route review gates to any registered gate name — e.g. "
        "kimi_gate,glm_gate — without editing review_gate_manager.py. ci_gate is appended "
        "separately when VNX_CI_GATE_REQUIRED is on; do not include it here.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    "VNX_REVIEW_GATE_TAKEOVER_CHAIN": _e(
        "VNX_REVIEW_GATE_TAKEOVER_CHAIN", "string",
        "codex_gate,kimi_gate,glm_gate,deepseek_gate", "gate",
        "Ordered review-gate takeover chain (BETA3-E1, 26-08 operator decision). On "
        "lane_exhausted a seat rolls over to the NEXT gate named here; deepseek_gate is a "
        "legal end-link that is skipped with a named reason until its runner ships (E2). "
        "ABSENT (no env/DB override) falls back to this literal default string. An EXPLICIT "
        "empty value ('') means NO takeover chain at all -- distinct from absent. Any name "
        "outside the known gate set, or a name repeated (a cycle), fails loud at read time. "
        "Must match gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN verbatim "
        "(pinned by test_beta3_e1_review_gate_chain.py).", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    "VNX_CI_GATE_REQUIRED": _e(
        "VNX_CI_GATE_REQUIRED", "bool", "1", "gate",
        "Require the CI gate before merge.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE",
        cockpit_canonical=True),
    "VNX_WIRING_GATE_REQUIRED": _e(
        "VNX_WIRING_GATE_REQUIRED", "bool", "0", "gate",
        "Require the wiring gate. Shadow mode (0) is a deliberate decision, not an "
        "oversight (OI-1385): wiring_gate sits on "
        "closure_verifier._GATES_NOT_IMPLEMENTED_BY_CLOSURE, so the closure verifier "
        "cannot honestly attest its evidence yet -- promoting to required would let a "
        "gate produce blocking findings that no sluitingscontrole can verify. Revisit "
        "when wiring_gate is promoted off that list (pinned by "
        "test_wiring_gate_shadow_mode_decision_tied_to_closure_verifier_exclusion).",
        approval=True,
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
        subsystem="central-db-routing", status="ACTIVATE"),
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
        "Governance-enforcement-stack master switch (display metadata only; no read-site "
        "wired -- measured 23-08, OI-1385: sweep of the repo outside .git/.vnx-data finds "
        "only the definition below, test files, and docs). read_site_wired=False so this "
        "entry can never win canonical_flags() for its subsystem no matter where it sits "
        "in this dict; VNX_CI_GATE_REQUIRED is the real, wired flag for "
        "governance-enforcement-stack.",
        approval=True,
        subsystem="governance-enforcement-stack", status="PARK",
        read_site_wired=False),
    "VNX_LEARNING_LOOP_ENABLED": _e(
        "VNX_LEARNING_LOOP_ENABLED", "bool", "0", "intelligence",
        "Daily pattern learning / skill refinement / confidence-update loop.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE",
        # Explicit canonical pick among this subsystem's 6 flags (OI-1385): preserves the
        # pre-existing last-wins winner now that canonical_flags() no longer picks by dict
        # order. Not re-audited by this dispatch — out of scope.
        cockpit_canonical=True),
    "VNX_DREAM_SCHEDULER_ENABLED": _e(
        "VNX_DREAM_SCHEDULER_ENABLED", "bool", "0", "intelligence",
        "Nightly memory consolidation + pending review dispatch.",
        subsystem="dream-consolidation", status="ACTIVATE"),
    "VNX_INJECTION_FEEDBACK_ENABLED": _e(
        "VNX_INJECTION_FEEDBACK_ENABLED", "bool", "0", "intelligence",
        "Instrument why intelligence injections are ignored before tuning generation.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE"),
    "VNX_INJECTION_WHY_ENABLED": _e(
        "VNX_INJECTION_WHY_ENABLED", "bool", "0", "intelligence",
        "Persist a per-offer used/ignored-reason row (pattern_injection_outcome) at delivery "
        "time via a deterministic content-overlap check + reason classifier. Off = byte-for-byte "
        "the current filename-only record_adoption_from_receipt behavior (no new reads/writes). "
        "Prerequisite instrumentation for the reason-aware evaluator (PR-B); does not itself "
        "activate the learning loop.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE",
        # Explicit canonical pick (OI-1385): preserves the pre-existing last-wins winner for
        # this subsystem's 2 flags. Not re-audited by this dispatch — out of scope.
        cockpit_canonical=True),
    "VNX_PLAN_GATE_COMPLEX_ONLY": _e(
        "VNX_PLAN_GATE_COMPLEX_ONLY", "bool", "0", "gate",
        "Restrict the plan-gate panel to complex features: a LIGHT-scope plan "
        "(read-site in plan_gate_enforcement.plan_gate_scope) runs the reduced "
        "2-seat panel; HEAVY plans keep the full 5-seat panel.",
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

    # AUTO-staging per tier + canary (dispatch 20260814s-a): phased rollout of
    # smart-router AUTO routing. Defaults OFF so the rollout starts from zero —
    # operator-config ramps it up per tier + a deterministic canary fraction.
    # subsystem is a NEW flag-backed name (disjoint from the flag-less
    # "provider-routing"), so the cockpit never double-represents one subsystem.
    "VNX_SMART_ROUTER_TIER_ZERO": _e(
        "VNX_SMART_ROUTER_TIER_ZERO", "bool", "0", "dispatch",
        "Route tier-zero (trivial reformat) dispatches through the smart router.",
        subsystem="smart-router-staging", status="ACTIVATE"),
    "VNX_SMART_ROUTER_TIER_LOW": _e(
        "VNX_SMART_ROUTER_TIER_LOW", "bool", "0", "dispatch",
        "Route tier-low (script edit) dispatches through the smart router.",
        subsystem="smart-router-staging", status="ACTIVATE"),
    "VNX_SMART_ROUTER_TIER_MID": _e(
        "VNX_SMART_ROUTER_TIER_MID", "bool", "0", "dispatch",
        "Route tier-mid (multi-file/design) dispatches through the smart router.",
        subsystem="smart-router-staging", status="ACTIVATE"),
    "VNX_SMART_ROUTER_TIER_HIGH": _e(
        "VNX_SMART_ROUTER_TIER_HIGH", "bool", "0", "dispatch",
        "Route tier-high (architectural/security) dispatches through the smart router.",
        subsystem="smart-router-staging", status="ACTIVATE"),
    "VNX_SMART_ROUTER_CANARY_PCT": _e(
        "VNX_SMART_ROUTER_CANARY_PCT", "string", "0", "dispatch",
        "Canary fraction (0-100) of an enabled tier's traffic routed via the smart "
        "router; the rest follows the legacy path. Deterministic per dispatch.",
        subsystem="smart-router-staging", status="ACTIVATE",
        # Explicit canonical pick (OI-1385): preserves the pre-existing last-wins winner for
        # this subsystem's 5 flags. Not re-audited by this dispatch — out of scope.
        cockpit_canonical=True),

    # Operator directive 2026-08-21 (dispatch-20260821-t0-tmux-concurrency-10): the
    # claude-tmux N-slot lock's concurrency cap, raised from 5 to 10 and made
    # registry-backed so an operator can flip it from the dashboard, not just env.
    "VNX_TMUX_MAX_CONCURRENT": _e(
        "VNX_TMUX_MAX_CONCURRENT", "string", "10", "dispatch",
        "N-slot concurrency cap for the claude-tmux serial lock "
        "(scripts/lib/dispatch_serialization.py). ACCOUNT-wide: shared with every "
        "other project and worktree running on the same Claude subscription, not "
        "scoped to this project. The process env var still wins over this "
        "config-store value as an explicit per-session override.",
        approval=True,
        subsystem="claude-tmux-serialization", status="ACTIVATE"),
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


def canonical_flags(registry: Optional[Dict[str, ConfigEntry]] = None) -> Dict[str, str]:
    """subsystem -> the one CONFIG_REGISTRY key shown as the cockpit row's canonical ``flag``.

    OI-1385: selection is EXPLICIT, not "whichever entry CONFIG_REGISTRY happens to iterate
    last" — that insertion-order rule let a flag with zero production read-sites
    (VNX_GOVERNANCE_ENFORCED) win over real, wired flags purely by dict placement, and the
    cockpit showed its static "0" as the entire governance-enforcement-stack's afdwingniveau
    while the real enforcement source (.vnx/governance_enforcement.yaml) ran mandatory checks.

    Per subsystem:
      - entries with ``read_site_wired=False`` are never eligible (a structural exclusion via
        the entry's own property, not a name check in this function — a flag that later gains
        a real read-site becomes eligible again automatically).
      - exactly one eligible entry -> that entry wins, regardless of dict order.
      - more than one eligible entry -> exactly one of them must carry
        ``cockpit_canonical=True`` as the explicit tie-breaker; zero or more than one raises
        ValueError rather than silently picking one (order-independent: reversing the dict's
        insertion order never changes the winner).

    ``registry`` defaults to this module's CONFIG_REGISTRY; a caller may pass a substitute dict
    of ConfigEntry for testing the selection rule in isolation.
    """
    reg = CONFIG_REGISTRY if registry is None else registry
    by_subsystem: Dict[str, List[str]] = {}
    for key, entry in reg.items():
        if entry.subsystem:
            by_subsystem.setdefault(entry.subsystem, []).append(key)

    canonical: Dict[str, str] = {}
    for subsystem, keys in by_subsystem.items():
        eligible = [k for k in keys if reg[k].read_site_wired]
        if not eligible:
            raise ValueError(
                f"subsystem {subsystem!r} has no read_site_wired CONFIG_REGISTRY entry among "
                f"{keys} -- the cockpit cannot show a canonical flag for it"
            )
        if len(eligible) == 1:
            canonical[subsystem] = eligible[0]
            continue
        marked = [k for k in eligible if reg[k].cockpit_canonical]
        if len(marked) != 1:
            raise ValueError(
                f"subsystem {subsystem!r} has {len(eligible)} read_site_wired candidates "
                f"{eligible} but {len(marked)} marked cockpit_canonical=True -- exactly one "
                "must be explicit"
            )
        canonical[subsystem] = marked[0]
    return canonical
