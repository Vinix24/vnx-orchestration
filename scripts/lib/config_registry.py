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
  2. ``project_config`` DB value — the UI-set value (injected via ``db_resolver``); skipped for a
     key with scope ``global``.
  3. ``global_config`` — the machine-wide value in ``~/.vnx/config.json`` (``config_global``,
     ADR-087; ``VNX_GLOBAL_CONFIG_FILE`` moves it). Needs no store.
  4. ``VNX_<BARE>`` env var — the process-start value.
  5. the registry default.

A stored value (project or global) beats an exported env value; ``VNX_OVERRIDE_<BARE>`` overrules
one process. Without a wired store step 2 is skipped; without a global file step 3 is.

SUBSYSTEM COCKPIT METADATA. ``subsystem`` and ``status`` are pure display metadata for the
framework-status cockpit (`docs/core/SUBSYSTEMS.md`): which subsystem a flag belongs to, and that
subsystem's declared status. They carry no runtime behaviour — no read-site consults them, no
default changes because of them. ``status`` is one of ``ALLOWED_STATUSES``. Subsystems that have no
dedicated flag (e.g. ``phantom_guard``, ``dispatch-plan``) are not represented here; they resolve via
``CONFIG_REGISTRY_SUBSYSTEMS`` instead, so the cockpit's rowset is the union of both.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

# The only status values a cockpit row may declare (dispatch: framework-status-audit-and-cockpit PR-1).
ALLOWED_STATUSES = frozenset(("LIVE", "PARK", "CUT", "ACTIVATE", "SCOPE", "COCKPIT"))
ALLOWED_SCOPES = frozenset(("project", "global"))


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    type: str  # "bool" | "string" | "enum" | "integer" | "float"
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
    # ADR-087 decision 2. "project" (every key today): a project_config row beats the global
    # value. "global": only the global file counts; the resolver ignores a project row.
    scope: str = "project"
    # Enum keys only; None means no check. The write-side check is G3 (config_store_db._coerce).
    allowed_values: Optional[Tuple[str, ...]] = None
    # Opt-in integer bound, enforced by registry resolution before a reader gets a value.
    minimum: Optional[int] = None

    def __post_init__(self) -> None:
        if self.scope not in ALLOWED_SCOPES:
            raise ValueError(f"{self.key}: scope {self.scope!r} is not one of {sorted(ALLOWED_SCOPES)}")
        if self.allowed_values is not None and (
                not isinstance(self.allowed_values, tuple) or not self.allowed_values
                or not all(isinstance(v, str) for v in self.allowed_values)):
            raise ValueError(f"{self.key}: allowed_values must be a non-empty tuple of strings")
        if self.minimum is not None:
            if self.type != "integer" or type(self.minimum) is not int:
                raise ValueError(f"{self.key}: minimum requires an integer type and bound")
            self.validate(self.default)

    def validate(self, value: str, *, source: str = "value") -> str:
        """Return the value unchanged; raise ValueError if an opt-in integer bound fails."""
        if self.minimum is not None and (
                not isinstance(value, str) or re.fullmatch(r"[+-]?[0-9]+", value.strip()) is None
                or int(value) < self.minimum):
            raise ValueError(f"{self.key}: {source} must be a finite integer >= {self.minimum}; got {value!r}")
        return value


def _e(key, type_, default, category, description, *, writable=True, approval=False, planned=False,
       subsystem=None, status=None, read_site_wired=True, cockpit_canonical=False, scope="project",
       allowed_values=None, minimum=None):
    return ConfigEntry(key, type_, default, category, description, writable, approval, planned,
                        subsystem, status, read_site_wired, cockpit_canonical, scope, allowed_values, minimum)


# The inventory. Defaults verified against the read-sites (codex review finding: defaults must
# match current code). All feature toggles are off ("0") today; the tagger provider is deepseek.
#
# subsystem/status backfill (framework-status-audit-and-cockpit PR-1): every entry below is tagged
# with the cockpit subsystem it belongs to and that subsystem's declared status. These are the
# flag-BACKED subsystems; the flag-LESS kernel subsystems (dispatch-plan, phantom_guard, etc.) live
# in CONFIG_REGISTRY_SUBSYSTEMS below so the two sets never collide on the same subsystem name.
CONFIG_REGISTRY: Dict[str, ConfigEntry] = {
    # config-een-oppervlak PR-1 (ADR-087): rows for the keys the reader PRs move onto config_runtime.
    # Placed first on purpose: _canonical_flags lets the last-inserted key per subsystem win, so these
    # rows must stay before the existing master flags to leave the cockpit ledger unchanged.
    # Behaviour-neutral: every default equals what the reader uses today and every reader still
    # reads os.environ, so read_site_wired=False until its own PR. The scope of a key follows plan V2.1.
    "VNX_ANALYZER_LLM": _e(
        "VNX_ANALYZER_LLM", "string", "ollama-only", "intelligence",
        "Conversation-analyzer LLM strategy: auto | ollama-only | claude-only | deepseek-harness. "
        "Governs unrestricted sessions only.", scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False,
        allowed_values=("auto", "ollama-only", "claude-only", "deepseek-harness")),
    "VNX_ANALYZER_RESTRICTED_CLAUDE_CAP": _e(
        "VNX_ANALYZER_RESTRICTED_CLAUDE_CAP", "integer", "", "intelligence",
        "Nightly cap on restricted sessions sent to Claude. Empty means the reader's own default (20).",
        scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_OLLAMA_HOST": _e(
        "VNX_OLLAMA_HOST", "string", "http://localhost:11434", "intelligence",
        "Ollama base URL.", scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_OLLAMA_MODEL": _e(
        "VNX_OLLAMA_MODEL", "string", "qwen2.5-coder:14b", "intelligence",
        "Ollama model of the conversation analyzer. The ollama adapter and the classifier provider "
        "still carry other defaults of their own (PR-4a unifies them).",
        scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_DIGEST_EMAIL": _e(
        "VNX_DIGEST_EMAIL", "string", "", "intelligence",
        "Recipient of the nightly digest email; empty skips the email phase.",
        scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_SMTP_HOST": _e(
        "VNX_SMTP_HOST", "string", "smtp.gmail.com", "intelligence",
        "SMTP server of the digest email.", scope="global", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_RECEIPT_CLASSIFIER_ENABLED": _e(
        "VNX_RECEIPT_CLASSIFIER_ENABLED", "bool", "0", "intelligence",
        "Receipt classifier opt-in.", subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_RECEIPT_CLASSIFIER_MODE": _e(
        "VNX_RECEIPT_CLASSIFIER_MODE", "string", "batch", "intelligence",
        "Receipt classifier mode: batch (default), per_receipt or failures_direct.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_RECEIPT_CLASSIFIER_PROVIDER": _e(
        "VNX_RECEIPT_CLASSIFIER_PROVIDER", "string", "haiku", "intelligence",
        "Provider of the receipt classifier.",
        subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD": _e(
        "VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD", "float", "0.20", "intelligence",
        "Daily USD spend cap of the receipt classifier. Raising it waives the 0.20 USD/day classifier cost cap.",
        approval=True, subsystem="intelligence-self-learning-loop", status="ACTIVATE", read_site_wired=False),
    "VNX_AUTO_REPORT": _e(
        "VNX_AUTO_REPORT", "bool", "1", "dispatch",
        "Auto-report Stop hook gate. Default on: the value every fabric process saw while "
        "vnx_paths.sh exported it.", subsystem="headless-dispatch-routing", status="ACTIVATE"),
    "VNX_ENFORCE_WORKER_PERMISSIONS": _e(
        "VNX_ENFORCE_WORKER_PERMISSIONS", "bool", "0", "gate",
        "Worker permission enforcement (off by default).", subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_PROVENANCE_ENFORCEMENT": _e(
        "VNX_PROVENANCE_ENFORCEMENT", "bool", "0", "gate",
        "Provenance (trace token) check: 0 warns, 1 blocks.", subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_PROVENANCE_LEGACY_ACCEPTED": _e(
        "VNX_PROVENANCE_LEGACY_ACCEPTED", "bool", "1", "gate",
        "1 accepts legacy trace-token formats, 0 only the preferred one.", subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_SLUG_ENFORCEMENT": _e(
        "VNX_SLUG_ENFORCEMENT", "bool", "0", "gate",
        "CI slug match: 0 warns, 1 blocks.", subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_QUEUE_POPUP_ENABLED": _e(
        "VNX_QUEUE_POPUP_ENABLED", "bool", "1", "dispatch",
        "Queue popup of the start and resume commands; 0 turns it off.", subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T1_PROVIDER": _e(
        "VNX_T1_PROVIDER", "string", "", "dispatch",
        "Provider of terminal T1 (layer 2 of the worker pair, ADR-091). Empty: this layer delivers "
        "nothing and the worker registry decides.", subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T2_PROVIDER": _e(
        "VNX_T2_PROVIDER", "string", "", "dispatch",
        "Provider of terminal T2 (layer 2 of the worker pair, ADR-091). Empty: this layer delivers "
        "nothing and the worker registry decides.", subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T3_PROVIDER": _e(
        "VNX_T3_PROVIDER", "string", "", "dispatch",
        "Provider of terminal T3 (layer 2 of the worker pair, ADR-091). Empty: this layer delivers "
        "nothing and the worker registry decides.", subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T0_PROVIDER": _e(
        "VNX_T0_PROVIDER", "string", "claude_code", "dispatch",
        "Provider of terminal T0.",
        subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T0_SKIP_PERMISSIONS": _e(
        "VNX_T0_SKIP_PERMISSIONS", "bool", "0", "dispatch",
        "Launch terminal T0 with the CLI permission prompts skipped. Setting 1 waives the permission prompts "
        "(--dangerously-skip-permissions) for that terminal.",
        approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T1_SKIP_PERMISSIONS": _e(
        "VNX_T1_SKIP_PERMISSIONS", "bool", "0", "dispatch",
        "Launch terminal T1 with the CLI permission prompts skipped. Setting 1 waives the permission prompts "
        "(--dangerously-skip-permissions) for that terminal.",
        approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T2_SKIP_PERMISSIONS": _e(
        "VNX_T2_SKIP_PERMISSIONS", "bool", "0", "dispatch",
        "Launch terminal T2 with the CLI permission prompts skipped. Setting 1 waives the permission prompts "
        "(--dangerously-skip-permissions) for that terminal.",
        approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_T3_SKIP_PERMISSIONS": _e(
        "VNX_T3_SKIP_PERMISSIONS", "bool", "0", "dispatch",
        "Launch terminal T3 with the CLI permission prompts skipped. Setting 1 waives the permission prompts "
        "(--dangerously-skip-permissions) for that terminal.",
        approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_PAID_LANE_DAILY_BUDGET_USD": _e(
        "VNX_PAID_LANE_DAILY_BUDGET_USD", "float", "5.00", "dispatch",
        "Daily USD cap of the paid provider lanes. Raising it waives the 5 USD/day paid-lane cost cap.",
        approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),
    "VNX_REQUIRE_DISPATCH_TRACK": _e(
        "VNX_REQUIRE_DISPATCH_TRACK", "bool", "0", "dispatch",
        "Require a track_id on a dispatch. Off (default): the door only warns; on: a dispatch without a track is refused.",
        subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=True),
    "VNX_SUPERVISOR_MODE": _e(
        "VNX_SUPERVISOR_MODE", "string", "legacy", "dispatch",
        "Unified supervisor opt-in: unified switches on auto-respawn, lease sweep and runtime supervision; legacy leaves them off.",
        subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False,
        allowed_values=("legacy", "unified")),
    "VNX_ENFORCEMENT_PROFILE": _e(
        "VNX_ENFORCEMENT_PROFILE", "string", "shadow", "gate",
        "Enforcement profile: shadow (default) | enforced.", approval=True, subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=True,
        allowed_values=("shadow", "enforced")),
    "VNX_ACCOUNT_DIRS": _e(
        "VNX_ACCOUNT_DIRS", "string", "[]", "dispatch",
        "Claude account directories as a JSON list (ADR-090). The reader validates the content.",
        scope="global", approval=True, subsystem="headless-dispatch-routing", status="ACTIVATE", read_site_wired=False),

    "VNX_SCOUT_PROVIDER": _e(
        "VNX_SCOUT_PROVIDER", "string", "deepseek", "intelligence",
        "Provider of the scout recon pre-pass; a value outside the allowed set falls back to deepseek.",
        subsystem="cheap-recon-scout", status="ACTIVATE", read_site_wired=True,
        allowed_values=("codex", "deepseek", "gemini", "ollama")),
    "VNX_SCOUT_PREPASS": _e(
        "VNX_SCOUT_PREPASS", "bool", "0", "intelligence",
        "Cheap-model scout recon pre-pass in the door (fail-open).",
        subsystem="cheap-recon-scout", status="ACTIVATE", cockpit_canonical=True),
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
        subsystem="headless-dispatch-routing", status="ACTIVATE", cockpit_canonical=True),
    "VNX_BUILDER_SETTING_SOURCES": _e(
        "VNX_BUILDER_SETTING_SOURCES", "string", "project,local", "dispatch",
        "Comma list passed as --setting-sources to headless claude builders (the claude_headless "
        "lane). Default project,local: the builder starts without the operator's user-level "
        "CLAUDE.md, rules, skills, plugins and hooks. It carries cleanupPeriodDays from the user "
        "settings file and claudeMdExcludes for the user-level CLAUDE.md and rules that the "
        "walk-up from the worktree to ~ would load as project memory; effortLevel is not carried. "
        "user,project,local restores today's "
        "argv exactly (no flag). An invalid value falls back to the default and is logged.",
        approval=True, subsystem="headless-builder-context", status="ACTIVATE",
        read_site_wired=True, cockpit_canonical=True),
    "VNX_DOC_STYLE_FILE": _e(
        "VNX_DOC_STYLE_FILE", "string", "", "dispatch",
        "Absolute or ~-relative path of a Markdown file with writing rules that staging appends "
        "to every docs dispatch (task class 04_documentation or 06_design, or paths that are all "
        "docs/** or *.md outside changelog.d/). Empty means off. A set path that cannot be read "
        "(missing, not a file, empty) refuses staging of a docs dispatch.",
        approval=True, subsystem="docs-style-file", status="ACTIVATE",
        read_site_wired=True),
    "VNX_DEFAULT_REVIEW_STACK": _e(
        "VNX_DEFAULT_REVIEW_STACK", "string", "codex_gate,deepseek_gate", "gate",
        "Comma-separated default review-gate stack. Operator decision 2026-11-11: the Kimi "
        "subscription is cancelled, so seat 2 is deepseek_gate (API credit, deepseek-v4-pro); "
        "seat 1 stays a SUBSCRIPTION reviewer (codex_gate on the codex CLI). kimi_gate stays "
        "registered and an explicit stack may still name it. Since OI-2058 (R5a) every "
        "seat gets its own reviewer: when deepseek_gate takes codex_gate's seat, its own "
        "seat walks on to the next distinct gate in the chain. Since R5b the merge door "
        "requires every seat of this stack, each filled by a distinct reviewer. Seat 1 is a "
        "subscription gate (gate_billing_table.GATE_BILLING classifies every gate; a guard "
        "test pins it); seat 2 and the takeover chain may be metered. "
        "History: the default was gemini_review,codex_gate,claude_github_optional until "
        "20260914, when gemini_review (binary never on PATH) and claude_github_optional "
        "(never configured) were dropped and glm_gate was added as a second standing seat "
        "(#1852); 20260926 replaced glm_gate by kimi_gate. claude_github_optional and "
        "gemini_review remain legal gate_recorder.GATE_PROVIDERS entries, so their "
        "historical records still read as what they were. gemini_review went further on "
        "2026-09-26 (operator decision: Gemini is no longer a reviewer): it is a retired "
        "gate (dispatch_spec.RETIRED_GATE_NAMES), not a registered one; a stack that still "
        "names it is dropped with a warning by review_gate_manager. "
        "Lets an operator route review gates to any registered gate name without editing "
        "review_gate_manager.py. ci_gate is appended separately when VNX_CI_GATE_REQUIRED "
        "is on; do not include it here.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    "VNX_REVIEW_GATE_TAKEOVER_CHAIN": _e(
        "VNX_REVIEW_GATE_TAKEOVER_CHAIN", "string",
        "codex_gate,deepseek_gate,claude_gate", "gate",
        "Ordered review-gate takeover chain (BETA3-E1, 26-08 operator decision; order "
        "changed 2026-10-04, OI-1984, 2026-10-22, R7, and 2026-11-11). On lane_exhausted a "
        "seat rolls over to the NEXT gate named here. Since 2026-11-11 (Kimi cancelled) the "
        "default is codex_gate, deepseek_gate (deepseek-v4-pro), then claude_gate as the last "
        "link: claude_gate failed its calibration run on sonnet and on opus, so it reads "
        "only when deepseek is out or passed over. kimi_gate and glm_gate left the default "
        "chain; both stay registered and can still be named explicitly here. A seat "
        "never gets a reviewer of the builder's vendor (R7): such a reviewer is passed over "
        "like an unavailable one (review_vendor). Since "
        "OI-1714/OI-1838 deepseek_gate is a real harness-lane gate (available by "
        "registration, requested via gate_request_handler._request_deepseek), and since "
        "OI-1984 it is a member of the dispatch_spec.Gate enum, so its takeover record "
        "signs the seat it took over at the merge door; claude_gate is one the same way. "
        "ABSENT (no env/DB override) falls back to this literal default string. An EXPLICIT "
        "empty value ('') means NO takeover chain at all -- distinct from absent. Any name "
        "outside the known gate set, or a name repeated (a cycle), fails loud at read time. "
        "Must match gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN verbatim "
        "(pinned by test_beta3_e1_review_gate_chain.py).", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    "VNX_SALVAGE_PUSH_TIMEOUT_S": _e(
        "VNX_SALVAGE_PUSH_TIMEOUT_S", "string", "900", "dispatch",
        "Seconds the door's push of a dispatch branch may take (OI-2744). A project whose "
        "pre-push hook runs its CI script needs more than the old 120 s; on a timeout the "
        "worktree with the salvage commit is kept. A value that is not a positive int falls back to 900.",
        approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    # OI-2288: the codex review seat's reasoning effort, read by
    # gate_runner.resolve_codex_review_effort at every codex_gate launch. Listed
    # read-only (writable=False): like the codex model pins this module's header
    # keeps out of the UI, it is read from the process environment and never
    # from project_config, so a dashboard value would change nothing. Not the
    # last governance-enforcement-stack entry, so the ledger row keeps its flag.
    "VNX_CODEX_REVIEW_EFFORT": _e(
        "VNX_CODEX_REVIEW_EFFORT", "string", "", "gate",
        "Reasoning effort the codex_gate review seat is launched with (OI-2288). Empty "
        "(the default): per the request's risk class, high for high or critical and "
        "medium for every other class or none. Set: one of low, medium, high, xhigh or "
        "max for every codex review. Any other value fails the run as "
        "codex_review_effort_invalid, naming the variable and the value. Before OI-2288 "
        "the gate passed no effort, so model_reasoning_effort in the operator's "
        "~/.codex/config.toml (xhigh) decided every review; the model's own default is "
        "medium. The result record carries the effort used as reasoning_effort. "
        "Env-only: read from the process environment at launch.",
        writable=False,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # RG-1 (track review-rondegrens-met-tiebreaker, ADR-041 amendment R1e): the
    # codex review-round cap. B3.2 also caps a PR once this many codex FAIL
    # rounds fell on distinct heads since the PR's last `opened` line. Read-site
    # is ``review_rounds.round_cap`` through ``config_runtime.get`` (the
    # per-project chain); the value is a string holding a positive integer and
    # a malformed one fails loud there instead of falling back to the default.
    "VNX_REVIEW_ROUND_CAP": _e(
        "VNX_REVIEW_ROUND_CAP", "string", "3", "gate",
        "Codex review-round cap: how many codex FAIL rounds on distinct heads, counted since "
        "the PR's last opened line in the round ledger, put the PR at the B3.2 cap. Default 3. "
        "Resolved per project through config_runtime (VNX_OVERRIDE_REVIEW_ROUND_CAP > "
        "project_config row > VNX_REVIEW_ROUND_CAP > default); review_rounds.round_cap parses it "
        "as a positive integer and fails loud on an empty, zero, negative or non-integer value. "
        "There is no vnx config verb: a non-default goes through dashboard/api_config.py or a "
        "project_config row. Setting it high is the rollback without code.",
        subsystem="governance-enforcement-stack", status="LIVE"),
    # Track gate-profiel-per-project, P1: the project's gate weight. Read ONLY through
    # gate_profile.resolve (never as a bare string by a reader), which validates it
    # fail-closed: an unset value is "standard", any other value than light, standard or
    # heavy (after one strip) raises GateProfileConfigError naming the raw value and its
    # source. Nothing falls back to "standard" for a value that failed.
    "VNX_GOVERNANCE_PROFILE": _e(
        "VNX_GOVERNANCE_PROFILE", "enum", "standard", "gate",
        "The project's gate profile: light, standard or heavy. Sets the review stack, the review "
        "round cap, the takeover chain and the plan-gate seats in one choice (the table is "
        "gate_profile.PROFILE_TABLE). standard (the default) contributes nothing: every reader "
        "keeps its own chain, which is today's behaviour. Resolved through the same precedence "
        "chain as every key (VNX_OVERRIDE_GOVERNANCE_PROFILE > project_config row > "
        "VNX_GOVERNANCE_PROFILE > default). Any other value fails closed with "
        "GateProfileConfigError naming the raw value and its source; there is no fallback to "
        "standard.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    # Track tweede-stoel-op-signaal, PR 5. The key review_seat_evaluation.active_seat_rule
    # reads through config_runtime.get (the merge door and vnx-gate/review share that one
    # reader). Registered so a project can carry the value in project_config: that row is
    # written through config_store_db.set_config with an approval id. The default is the
    # EMPTY string on purpose -- with no row and no env var the read-site used to return
    # None, so active_seat_rule() answered ActiveSeatRule("first_seat_or_every_seat_by_risk",
    # "", True) and that empty value is what the merge record carries. A non-empty default
    # would change the recorded value on every merge of every project (pinned by
    # tests/test_config_registry_seat_rule.py).
    "VNX_REVIEW_SEAT_RULE": _e(
        "VNX_REVIEW_SEAT_RULE", "string", "", "gate",
        "Review seat rule. first_seat_or_every_seat_by_risk (the behaviour while the key "
        "is unset) narrows the stack by the risk class of the head; "
        "first_seat_or_every_seat_on_signal asks the second seat only after a clean PASS "
        "of seat 1 and a fired signal. An unset key keeps the risk rule; any other value "
        "fails closed to the risk rule and is recorded by name "
        "(review_seat_evaluation.active_seat_rule). The emergency brake is "
        "VNX_OVERRIDE_REVIEW_SEAT_RULE. vnx-dev sets the signal rule as a project_config "
        "row with an approval id since 2026-11-09; every other project has no row and "
        "keeps the risk rule.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # Track reviews-zonder-uitkomst D1. Both keys change what the codex review seat runs the
    # PR's tests with, so what a review sees: approval=True like VNX_REVIEW_SEAT_RULE, written
    # to project_config with an approval id. Read by gate_runner.resolve_review_interpreter and
    # gate_runner._review_tests_block; unset keeps the fabric's OI-2591 interpreter chain.
    "VNX_REVIEW_PYTHON": _e(
        "VNX_REVIEW_PYTHON", "string", "", "gate",
        "Absolute path of the interpreter the codex review seat runs this project's tests with "
        "(first rule of gate_runner.resolve_review_interpreter). It must import pytest, or the "
        "next rule applies: <projectroot>/.venv/bin/python, then the fabric's interpreter chain. "
        "Unset keeps that chain.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    "VNX_REVIEW_PYTHONPATH": _e(
        "VNX_REVIEW_PYTHONPATH", "string", "", "gate",
        "Comma-separated paths, relative to the gate worktree root (for example src), that the "
        "codex review seat puts first in PYTHONPATH when it runs the PR's tests. An entry that is "
        "absolute, holds .., whitespace or a shell metacharacter is left out of the command and "
        "named as text in the prompt. Unset adds no PYTHONPATH.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    # Deltareview DR-6 (ADR-064 "De schakelaar", ADR-060 amendment). Read by review_scope.active_review_scope.
    "VNX_REVIEW_SCOPE": _e(
        "VNX_REVIEW_SCOPE", "string", "", "gate",
        "Review scope. full (default): every round is a full review. delta_shadow: the shadow delta runs next to "
        "the full round. delta: the delta decides; writing it is refused without a passed DR-5 measurement, a merged "
        "DR-7 and a green main CI. Unset, unknown or unreadable reads as full.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # The PR-size thresholds of scripts/pre_merge_gate.check_pr_size, per project (operator
    # decision 2026-11-09: the limit is configurable, not a hard 450). Same resolution path as
    # VNX_REVIEW_SEAT_RULE. The defaults here are the only copy of both thresholds; a value that is
    # not a positive integer, or a WARN above HOLD, falls back to both defaults and is named
    # in the gate's detail line. It never disables the check.
    "VNX_PR_SIZE_WARN": _e(
        "VNX_PR_SIZE_WARN", "integer", "450", "gate",
        "PR size (lines added + removed) above which pre_merge_gate reports a large PR. Not "
        "blocking. A value that is not a positive integer, or above VNX_PR_SIZE_HOLD, falls "
        "back to 450 and is named in the gate detail.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    # ADR-088 besluit 9: HOLD 600 -> 450, equal to WARN, so gate-check has no soft band above 450.
    "VNX_PR_SIZE_HOLD": _e(
        "VNX_PR_SIZE_HOLD", "integer", "450", "gate",
        "PR size (lines added + removed) above which pre_merge_gate holds the merge. A value "
        "that is not a positive integer, or below VNX_PR_SIZE_WARN, falls back to 450 and is "
        "named in the gate detail.", approval=True,
        subsystem="governance-enforcement-stack", status="LIVE"),
    # ADR-088 besluit 1, 3 and 8 (track pr-grootte-afgedwongen PG-0). Read by the pr_size
    # preflight of the merge door since PG-1d (pr_size_policy.read_policy). The door reads
    # the floor (layers=("project_config", "default")) and the full chain and compares; an override only tightens.
    "VNX_PR_SIZE_ENFORCE": _e(
        "VNX_PR_SIZE_ENFORCE", "string", "warn", "gate",
        "How the merge door treats a PR above the size limit: off (not measured), warn (a line "
        "on the receipt) or refuse (NO-GO). Read by the merge door's pr_size preflight (ADR-088, "
        "PG-1d). The reader validates the value until the registry carries allowed values: any "
        "other value reads as refuse and is named.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    "VNX_PR_SIZE_HOLD_SECURITY": _e(
        "VNX_PR_SIZE_HOLD_SECURITY", "integer", "300", "gate",
        "PR size limit (lines added + removed) when one changed path is a security path "
        "(ADR-088 besluit 8). Read by the merge door's pr_size preflight (PG-1d).",
        approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    "VNX_SECURITY_PATHS": _e(
        "VNX_SECURITY_PATHS", "string", "", "gate",
        "Comma-separated path prefixes this project adds to the security set of the PR-size "
        "check. It only adds to the set built from code; a shorter list removes nothing. Read "
        "by the merge door's pr_size preflight (ADR-088 besluit 8, PG-1d).", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # The admission contract of the main window (ADR-073 besluit 3 and 6), read by
    # main_window.read_admission_config. Unset N is mode off: the gate records GO and the
    # merge behaviour is unchanged.
    "VNX_MAIN_UNVERIFIED_MAX": _e(
        "VNX_MAIN_UNVERIFIED_MAX", "integer", "", "gate",
        "Merges on main whose VNX CI run may still be running before the door refuses another "
        "(ADR-073). Unset: mode off. A value that is not a positive integer reads as unset "
        "(never 0) and is named in the record as invalid_config.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    "VNX_MAIN_RUN_MAX_AGE_MIN": _e(
        "VNX_MAIN_RUN_MAX_AGE_MIN", "integer", "40", "gate",
        "Minutes after which a queued or in-progress main run, or a merge commit without a "
        "run, is a NO-GO (stale_run, no_run) instead of running. A value that is not a "
        "positive integer falls back to 40 and is named.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # OI-2701 (option a), read by review_seat_evaluation.approved_full_fill_pairs. Empty by
    # default: no project approves a takeover, so every project keeps today's behaviour.
    "VNX_REVIEW_TAKEOVER_FULL_FILL": _e(
        "VNX_REVIEW_TAKEOVER_FULL_FILL", "string", "", "gate",
        "Comma-separated gate:model pairs (e.g. deepseek_gate:deepseek-v4-pro) whose takeover "
        "of seat 1 counts as a direct seat-1 PASS under the signal seat rule, given a PASS, zero "
        "blocking findings and both warning counts known on the exact head. Empty (the default) "
        "approves nobody, as does a malformed entry (logged); any other takeover keeps every "
        "seat.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE"),
    # ADR-074 (impactselectie in schaduw). No production code reads this key until PR 3 of the track
    # impactselectie-in-schaduw: the hook contract is fixed in the ADR first. Empty means every PR
    # runs the full suite.
    "VNX_CI_IMPACT_CMD": _e(
        "VNX_CI_IMPACT_CMD", "string", "", "gate",
        "Per-project impact-selection hook (ADR-074). Changed paths in; test files plus graph "
        "version out; exit 0. Any other outcome (non-zero exit, unparseable output, time-out) means "
        "the full run. The VNX value points to scripts/ci/impact_select.py. Empty (the default) "
        "selects nothing and every PR runs the full suite.", approval=True,
        subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    # ADR-097: registered ahead of the M3a/M3b readers (ci-measurement-every-project M3a-k).
    "VNX_CI_WORKFLOWS": _e(
        "VNX_CI_WORKFLOWS", "string", "", "gate",
        "PR-CI workflow names, comma-separated (ADR-097 section 1; owner M3a). Empty means "
        "every workflow with a PR run counts.", scope="project",
        subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_CI_RECORD_MAX_CALLS": _e(
        "VNX_CI_RECORD_MAX_CALLS", "integer", "1000", "gate",
        "GitHub call budget per project per night for ci_run/1 collection (ADR-097 section 3; "
        "owner M3a, shared with M3b). Includes pagination, job reads, artifact discovery and "
        "download, and retries; shared across catch-up days. A framework budget, not an adapter "
        "key. Must be a finite positive integer; invalid configuration is rejected before collection.",
        scope="project", minimum=1,
        subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
    "VNX_CI_JUNIT_ARTIFACT": _e(
        "VNX_CI_JUNIT_ARTIFACT", "string", "", "gate",
        "JUnit artifact name per attempt (ADR-097 section 2; owner M3b). Empty means test fields "
        "are unknown with junit_not_configured.", scope="project",
        subsystem="governance-enforcement-stack", status="ACTIVATE", read_site_wired=False),
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
        "A stored value beats the env var; VNX_OVERRIDE_PLAN_GATE_ENFORCE beats both. "
        "Operator override VNX_OVERRIDE_PLAN_GATE=1. Default advisory.",
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
    "VNX_DREAM_PROVIDER": _e(
        "VNX_DREAM_PROVIDER", "string", "deepseek", "intelligence",
        "Provider that runs the dream consolidation: deepseek or kimi. Any other value skips the cycle.",
        subsystem="dream-consolidation", status="ACTIVATE"),
    "VNX_DREAM_SCHEDULER_ENABLED": _e(
        "VNX_DREAM_SCHEDULER_ENABLED", "bool", "0", "intelligence",
        "Nightly memory consolidation + pending review dispatch.",
        subsystem="dream-consolidation", status="ACTIVATE", cockpit_canonical=True),
    "VNX_INJECTION_FEEDBACK_ENABLED": _e(
        "VNX_INJECTION_FEEDBACK_ENABLED", "bool", "0", "intelligence",
        "Instrument why intelligence injections are ignored before tuning generation.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE"),
    # intelligence-effect-measurable PR-1: the A/B window as registered, audited configuration
    # (project_config rows) instead of a plist edit. Every default is off or empty: registering
    # these keys opens no window. They sit BEFORE VNX_INJECTION_WHY_ENABLED on purpose: the
    # SUBSYSTEMS.md ledger generator takes the last-inserted entry of a subsystem, and that
    # must stay VNX_INJECTION_WHY_ENABLED (also the cockpit_canonical flag).
    "VNX_INTEL_AB_TEST": _e(
        "VNX_INTEL_AB_TEST", "bool", "0", "intelligence",
        "Open the intelligence-injection A/B window: dispatches are assigned a treatment or "
        "control arm. Off = no assignment, every dispatch injects as before.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE",
        read_site_wired=False),
    "VNX_INTEL_PLACEBO_ARM": _e(
        "VNX_INTEL_PLACEBO_ARM", "bool", "0", "intelligence",
        "Give the control arm of the A/B window a placebo section instead of nothing. "
        "Off = the control arm receives no intelligence section.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE",
        read_site_wired=False),
    "VNX_INTEL_AB_WINDOW": _e(
        "VNX_INTEL_AB_WINDOW", "string", "", "intelligence",
        "The frozen A/B window configuration (window id, salt, bounds) as one JSON document. "
        "Empty = no window is open.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE",
        read_site_wired=False),
    "VNX_INJECTION_WHY_ENABLED": _e(
        "VNX_INJECTION_WHY_ENABLED", "bool", "0", "intelligence",
        "Persist a per-offer used/ignored-reason row (pattern_injection_outcome) at delivery "
        "time via a deterministic content-overlap check + reason classifier. Off = byte-for-byte "
        "the current filename-only record_adoption_from_receipt behavior (no new reads/writes). "
        "Prerequisite instrumentation for the reason-aware evaluator (PR-B); does not itself "
        "activate the learning loop.",
        subsystem="injection-effectiveness-eval-loop", status="ACTIVATE",
        # Explicit canonical pick (OI-1385): preserves the pre-existing last-wins winner for
        # this subsystem's 5 entries. Not re-audited by this dispatch — out of scope.
        cockpit_canonical=True),
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
    "VNX_JUNIT_COUNTS": _e(
        "VNX_JUNIT_COUNTS", "bool", "1", "gate",
        "Both receipt write paths (report_parser and the envelope sub-path) take their "
        "receipt test counts from the builder's JUnit XML beside the report (track "
        "deterministische-testverificatie) instead of the prose in the Verification "
        "section. Off = today's text counts, byte for byte; this single setting is the "
        "rollback for both paths at once.",
        subsystem="deterministic-test-verification", status="ACTIVATE"),
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

    # OI-1718: the cutover behind-guard threshold. Every central-install
    # cutover (release --set-current, update --to) measures and prints how far
    # the target lags main BEFORE flipping `current`; above this count the
    # flip is refused without an explicit non-empty --cutover-reason (recorded
    # in the central-install audit log). Unmeasurable behindness is reported
    # as UNKNOWN, never silently 0, and proceeds loudly (update/rollback are
    # recovery tools); rollback is exempt from refusal by design.
    "VNX_CUTOVER_MAX_BEHIND_COMMITS": _e(
        "VNX_CUTOVER_MAX_BEHIND_COMMITS", "string", "20", "gate",
        "Max commits a cutover target may lag main before the flip is refused "
        "without an explicit --cutover-reason (OI-1718). Default 20 mirrors "
        "update.py DEFAULT_CUTOVER_MAX_BEHIND_COMMITS (pinned by "
        "test_registry_entry_registered_with_description); the incident was a "
        "tag measured 36->42 commits behind main within one evening.",
        approval=True,
        subsystem="central-install-cutover", status="ACTIVATE"),

    # Fleet pin audit (track vnx-fleet-pin-audit, PR-7): how far a project's
    # engine pin may lag the store's `current` before scripts/fleet_pin_audit.py
    # reports it as a finding instead of a WARN. Read through
    # config_registry.get() (never os.environ directly), so the standard
    # precedence chain applies. A non-numeric or negative value is loud on
    # stderr and falls back to the default. An UNKNOWN distance is never a
    # finding, and never ok: not measurable is not a pass.
    "VNX_FLEET_PIN_MAX_LAG_RELEASES": _e(
        "VNX_FLEET_PIN_MAX_LAG_RELEASES", "string", "3", "gate",
        "Max releases a project's .vnx-version pin may lag the store's current "
        "before fleet_pin_audit.py reports it as a finding (exit 1) instead of a "
        "WARN. Default 3 mirrors fleet_pin_audit.DEFAULT_MAX_LAG_RELEASES. An "
        "unknown behind-count always stays a WARN.",
        approval=True,
        subsystem="fleet-pin-audit", status="ACTIVATE"),

    # Operator directive 2026-08-21 (dispatch-20260821-t0-tmux-concurrency-10): the
    # claude-tmux N-slot lock's concurrency cap, raised from 5 to 10 and made
    # registry-backed so an operator can flip it from the dashboard, not just env.
    "VNX_TMUX_MAX_CONCURRENT": _e(
        "VNX_TMUX_MAX_CONCURRENT", "string", "10", "dispatch",
        "N-slot concurrency cap for the claude-tmux serial lock "
        "(scripts/lib/dispatch_serialization.py). ACCOUNT-wide: shared with every "
        "other project and worktree running on the same Claude subscription, not "
        "scoped to this project. A stored value beats the env var; "
        "VNX_OVERRIDE_TMUX_MAX_CONCURRENT beats both.",
        approval=True,
        subsystem="claude-tmux-serialization", status="ACTIVATE"),

    # Operator decision 2026-09-25 (dispatch 20260925-t0-context-rotation-enforced): a T0
    # orchestrator MUST rotate at 500K context. Measured by scripts/lib/t0_context_budget.py
    # from the session transcript's last assistant usage block, enforced by
    # scripts/hooks/t0_context_guard.py. T0 only: workers never read these.
    "VNX_T0_ROTATE_WARN_TOKENS": _e(
        "VNX_T0_ROTATE_WARN_TOKENS", "string", "400000", "dispatch",
        "T0 context size (tokens) at which the guard tells T0 to wind down and prepare the "
        "rotation (additionalContext on each prompt).",
        subsystem="t0-context-rotation", status="ACTIVATE"),
    "VNX_T0_ROTATE_HARD_TOKENS": _e(
        "VNX_T0_ROTATE_HARD_TOKENS", "string", "600000", "dispatch",
        "T0 context size (tokens) at which the guard demands the rotation now, even with work "
        "in flight; the handoff carries that work over.",
        approval=True,
        subsystem="t0-context-rotation", status="ACTIVATE"),
    # FORCE last: vnx_cli/commands/subsystems.py picks the last-inserted flag per subsystem
    # for the ledger row, config_registry.canonical_flags() the marked one; both say FORCE.
    "VNX_T0_ROTATE_FORCE_TOKENS": _e(
        "VNX_T0_ROTATE_FORCE_TOKENS", "string", "500000", "dispatch",
        "T0 context size (tokens) at which rotation is pending: the guard refuses to start new "
        "work (dispatch, staging, gh pr create, a new /goal) and blocks the end of each turn "
        "until the rotate flow ran. Finishing work in flight stays allowed.",
        approval=True,
        subsystem="t0-context-rotation", status="ACTIVATE", cockpit_canonical=True),

    # OI-2066: per-gate BASE lane deadline for the harness-lane review gates,
    # read at runtime by gate_lane_contract.harness_timeout_seconds(), which adds
    # 600 s for every started 100,000 diff chars above the first 100,000 and
    # clamps the result to 3600 s. Same defaults as
    # gate_lane_contract.TIMEOUT_CONFIG (900). Defined BEFORE the three
    # *_MAX_DIFF_CHARS keys below on purpose: the ledger generator shows a
    # subsystem's LAST-INSERTED flag (see OI-1874 r4 below), and that must stay
    # VNX_KIMI_GATE_MAX_DIFF_CHARS, the row's cockpit_canonical flag.
    "VNX_GLM_GATE_TIMEOUT_SECONDS": _e(
        "VNX_GLM_GATE_TIMEOUT_SECONDS", "string", "900", "gate",
        "Base lane deadline (seconds) for glm_gate. The diff extends it by 600 s per "
        "started 100,000 chars above the first 100,000; 3600 s bounds the result, a "
        "configured base included. An invalid or non-positive value falls back to 900.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS": _e(
        "VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", "string", "900", "gate",
        "Base lane deadline (seconds) for deepseek_gate. The diff extends it by 600 s "
        "per started 100,000 chars above the first 100,000; 3600 s bounds the result, a "
        "configured base included. An invalid or non-positive value falls back to 900.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_CLAUDE_GATE_TIMEOUT_SECONDS": _e(
        "VNX_CLAUDE_GATE_TIMEOUT_SECONDS", "string", "900", "gate",
        "Base lane deadline (seconds) for claude_gate. The diff extends it by 600 s "
        "per started 100,000 chars above the first 100,000; 3600 s bounds the result, a "
        "configured base included. An invalid or non-positive value falls back to 900.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_KIMI_GATE_TIMEOUT_SECONDS": _e(
        "VNX_KIMI_GATE_TIMEOUT_SECONDS", "string", "900", "gate",
        "Base lane deadline (seconds) for kimi_gate. The diff extends it by 600 s per "
        "started 100,000 chars above the first 100,000; 3600 s bounds the result, a "
        "configured base included. An invalid or non-positive value falls back to 900.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),

    # OI-1874: per-gate diff-char cap for the harness-lane review gates, read at
    # runtime by gate_lane_contract.max_diff_chars(). Since the operator decision
    # of 2026-10-03 (every reviewer may read up to 500,000 tokens of diff) all
    # three share ONE default, gate_lane_contract.DEFAULT_MAX_DIFF_CHARS (800000):
    # the reading budget, bounded by the argv ceiling of the delivery route
    # (OI-1961). Each gate keeps its own key so a project may lower it per gate.
    #
    # OI-1874 r3: subsystem="harness-lane-review-gates", NOT
    # "governance-enforcement-stack" -- these three keys used to carry that tag,
    # and vnx_cli/commands/subsystems.py's cockpit generator picks a subsystem's
    # ledger row by LAST-INSERTED flag, so adding them after
    # governance-enforcement-stack's existing entries silently flipped that row's
    # displayed flag/status to one of these (LIVE) while its "what" text still
    # said enforcement wiring is deferred -- a real subsystem's cockpit row
    # changed shape without a single edit to its own text. These three describe a
    # different, already-live mechanism (the harness-lane diff cap, not the
    # deferred hash-chain/attestation/evidence-bound stack), so they get their
    # own row instead of borrowing one. VNX_KIMI_GATE_MAX_DIFF_CHARS is marked
    # cockpit_canonical=True: config_registry.canonical_flags() requires exactly
    # one explicit tie-breaker once a subsystem has more than one
    # read_site_wired candidate (see that function's own docstring) -- kimi_gate
    # is picked as the default review stack's subscription-priority reviewer
    # (VNX_DEFAULT_REVIEW_STACK, DISPATCH_RULES.md).
    #
    # OI-1874 r4: status="ACTIVATE", NOT "LIVE". check_live_requires_measured_health.py
    # (D6b) refuses LIVE paired with unmeasured health, and no effectiveness
    # probe exists yet for this brand-new row (its only seed health is "unknown
    # -- no probe yet"). This is not a demotion of a broken feature: the cap IS
    # wired and read at every harness-lane gate run right now, the SAME shape as
    # sibling rows "t0-context-rotation" and "central-install-cutover" -- both
    # real, currently-enforcing mechanisms that also carry status="ACTIVATE"
    # for the identical reason (no probe registered yet). "ACTIVATE-and-measure"
    # describes "dormant until measured" for some rows and "live but
    # unmeasured" for these; the guard cares only about the LIVE+unmeasured
    # combination, not about which of those two this row actually is.
    #
    # OI-1874 r4: VNX_KIMI_GATE_MAX_DIFF_CHARS is defined LAST of these three
    # keys, deliberately -- vnx_cli/commands/subsystems.py's ``_canonical_flags``
    # (the ledger/CLI generator) still picks a subsystem's displayed flag by
    # LAST-INSERTED dict order, a separate, older implementation from this
    # module's own ``canonical_flags()`` below (which correctly honors
    # ``cockpit_canonical``; ``dashboard/api_subsystems.py`` already delegates to
    # it, OI-1385). Making the ledger generator delegate too is the real fix,
    # but it is NOT done here: governance-enforcement-stack already has its own
    # pre-existing last-inserted-vs-cockpit_canonical mismatch (VNX_GOVERNANCE_ENFORCED
    # vs VNX_CI_GATE_REQUIRED) that predates this dispatch, and switching the
    # generator's selection algorithm would silently change that unrelated,
    # already-committed row too. Ordering these three so last-inserted already
    # agrees with cockpit_canonical is the narrow fix that only touches this
    # new row.
    "VNX_GLM_GATE_MAX_DIFF_CHARS": _e(
        "VNX_GLM_GATE_MAX_DIFF_CHARS", "string", "800000", "gate",
        "Diff-char cap for the prompt glm_gate builds from a PR diff. The default is "
        "the operator's reading budget (500,000 tokens), bounded by the argv ceiling "
        "of 800000 chars (OI-1961); a value above the ceiling is clamped to it. A "
        "project may lower it.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS": _e(
        "VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS", "string", "800000", "gate",
        "Diff-char cap for the prompt deepseek_gate builds from a PR diff. The "
        "default is the operator's reading budget (500,000 tokens), bounded by the "
        "argv ceiling of 800000 chars (OI-1961); a value above the ceiling is "
        "clamped to it. A project may lower it.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_CLAUDE_GATE_MAX_DIFF_CHARS": _e(
        "VNX_CLAUDE_GATE_MAX_DIFF_CHARS", "string", "800000", "gate",
        "Diff-char cap for the prompt claude_gate builds from a PR diff. The "
        "default is the operator's reading budget (500,000 tokens), bounded by the "
        "argv ceiling of 800000 chars (OI-1961); a value above the ceiling is "
        "clamped to it. A project may lower it.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE"),
    "VNX_KIMI_GATE_MAX_DIFF_CHARS": _e(
        "VNX_KIMI_GATE_MAX_DIFF_CHARS", "string", "800000", "gate",
        "Diff-char cap for the prompt kimi_gate builds from a PR diff. The default is "
        "the operator's reading budget (500,000 tokens), bounded by the argv ceiling "
        "of 800000 chars (OI-1961); a value above the ceiling is clamped to it. A "
        "project may lower it.",
        approval=True,
        subsystem="harness-lane-review-gates", status="ACTIVATE", cockpit_canonical=True),

    # ADR-045 (track audit-spoor-kern, group 1 — K-1): the dispatch lifecycle in
    # the ledger — one dispatch_started, one dispatch_finished per attempt and a
    # report_ingested that is never an outcome. Its own block, deliberately away
    # from the gate entries above: this flag gates a WRITE path (the append layer
    # refuses the three new line types while it is off), not a review decision.
    # On by default since the activation delivery (K-act, ADR-045 §9/§10): every
    # writer landed and was proven live before this flip. Setting it to 0 is the
    # emergency brake — with it off no lifecycle writer emits a line at all, while
    # a dispatch that started in v3 still ends in v3 (fixed per dispatch).
    "VNX_LEDGER_LIFECYCLE": _e(
        "VNX_LEDGER_LIFECYCLE", "bool", "1", "dispatch",
        "ADR-045 dispatch lifecycle in the receipt ledger: dispatch_started, "
        "one dispatch_finished per (project_id, dispatch_id, attempt) and a "
        "report_ingested that never counts as an outcome. On by default since "
        "K-act (ADR-045 §9/§10); 0 is the emergency brake — with it off no "
        "lifecycle line is written at all (the append layer refuses the three "
        "types). Purely deterministic: no model call sits in the write path.",
        subsystem="ledger-lifecycle", status="LIVE"),
    # agent layout: `off` (default), `shadow` and `on`; any unknown value reads as `off` with one
    # WARNING (scripts/lib/agent_layout.py). Independent of agent-folder resolution (agent_resolver).
    "VNX_AGENT_LAYOUT": _e(
        "VNX_AGENT_LAYOUT", "string", "off", "dispatch",
        "Agent-as-folder skill layout: off (default), shadow (a non-discovered copy under "
        ".vnx-agent-skills/) or on (the skills at the provider's native, discovered destination, "
        "set per project by the operator). An unknown value reads as off.",
        subsystem="agent-layout", status="SCOPE"),
    # morning round (ADR-095): morning_pause.py reads these through a getter; since D1b the door
    # (stop_conditions.check_morning_pause) and pr_merge read them (morning_pause_gate.py), so all
    # four are wired. VNX_MORNING_PAUSE_ENABLED is the subsystem's canonical flag, inserted last
    # so the CLI's last-inserted rule agrees.
    "VNX_MORNING_PAUSE_START": _e(
        "VNX_MORNING_PAUSE_START", "string", "07:00", "dispatch",
        "Morning pause start, HH:MM in VNX_MORNING_PAUSE_TZ.",
        subsystem="morning-pause", status="PARK"),
    "VNX_MORNING_PAUSE_DEADLINE": _e(
        "VNX_MORNING_PAUSE_DEADLINE", "string", "09:00", "dispatch",
        "Morning pause deadline, HH:MM in VNX_MORNING_PAUSE_TZ; after it the pause never holds.",
        subsystem="morning-pause", status="PARK"),
    "VNX_MORNING_PAUSE_TZ": _e(
        "VNX_MORNING_PAUSE_TZ", "string", "Europe/Amsterdam", "dispatch",
        "IANA timezone of the morning pause window.",
        subsystem="morning-pause", status="PARK"),
    "VNX_MORNING_PAUSE_ENABLED": _e(
        "VNX_MORNING_PAUSE_ENABLED", "bool", "true", "dispatch",
        "Morning pause (ADR-095): firing and merging stop from the start time until the morning "
        "points are decided, at the latest at the deadline. On by default (D4b); no snapshot for today inside the window means not started, not paused.",
        subsystem="morning-pause", status="PARK", cockpit_canonical=True),
    # ADR-096 B2: the door reads it through config_runtime (dispatch_cli._check_predecessor_verdict).
    "VNX_DELIVERABLE_PREDECESSOR_MODE": _e(
        "VNX_DELIVERABLE_PREDECESSOR_MODE", "string", "refuse", "dispatch",
        "Deliverable predecessors (ADR-096): the door refuses (refuse), reports (warn) or skips (off) a fire "
        "whose hard predecessor has no pr_merged receipt.",
        subsystem="deliverable-predecessor", status="ACTIVATE", cockpit_canonical=True,
        allowed_values=("off", "warn", "refuse")),
    # ADR-096 B4b-d: read through config_runtime by dispatch_cli._blast_radius_open_pr_read.
    # Defined BEFORE VNX_BLAST_RADIUS_MODE on purpose: the subsystems ledger generator picks a
    # subsystem's displayed flag by last-inserted order, so keep VNX_BLAST_RADIUS_MODE last (OI-1874 r4).
    "VNX_BLAST_RADIUS_READ_DEADLINE_S": _e(
        "VNX_BLAST_RADIUS_READ_DEADLINE_S", "string", "45", "dispatch",
        "Total seconds the door's blast-radius check may spend reading open PRs (gh) inside the locked "
        "section; a PR not read in time is unknown for this fire only. A value that is not a positive "
        "int falls back to 45.",
        subsystem="blast-radius", status="ACTIVATE"),
    # ADR-096 B4b: the door reads it through config_runtime (dispatch_cli._check_blast_radius_verdict).
    "VNX_BLAST_RADIUS_MODE": _e(
        "VNX_BLAST_RADIUS_MODE", "string", "warn", "dispatch",
        "Blast radius (ADR-096): overlap of a fire's paths with live work or an open PR warns (warn), "
        "refuses unless --override-overlap REASON is given (refuse), or is not read (off); "
        "an unknown value reads as refuse.",
        subsystem="blast-radius", status="ACTIVATE", cockpit_canonical=True,
        allowed_values=("off", "warn", "refuse")),
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


# The layers of the precedence chain, highest first, by the source name resolve_with_source
# returns for each. One constant, so a new layer (config-een-oppervlak G2 adds a global one)
# lands in the chain walk and in every ``layers=`` subset a reader names at the same time.
CHAIN_LAYERS: "tuple[str, ...]" = ("override", "project_config", "global_config", "env", "default")

# The sources a strict read returns for a layer it could not read: never a value, never "off".
ERROR_SOURCES = frozenset(("project_config_error", "global_config_error"))

# One WARNING per unreadable global file (path, reason) and per ignored project row (project, key).
_WARNED_GLOBAL_UNREADABLE: set = set()
_WARNED_IGNORED_PROJECT_ROW: set = set()


def _checked_layers(layers) -> frozenset:
    """The set of ``layers``; raises ``ValueError`` on an empty set or an unknown name."""
    if isinstance(layers, str):
        raise ValueError(f"layers must be a sequence of layer names, not the string {layers!r}")
    walked = frozenset(layers)
    unknown = sorted(walked - frozenset(CHAIN_LAYERS))
    if unknown or not walked:
        raise ValueError(
            f"unknown or empty layers {sorted(walked)!r}: each must be one of {CHAIN_LAYERS!r}")
    return walked


def _bare(key: str) -> str:
    return key[len("VNX_"):] if key.startswith("VNX_") else key


def resolve_with_source(
    key: str, project_id: Optional[str] = None, resolver: Optional[DbResolver] = None,
    *, use_default_resolver: bool = True, strict_resolver: bool = False,
    layers: "tuple[str, ...]" = CHAIN_LAYERS,
) -> "tuple[Optional[str], str]":
    """``(value, source)`` through the one precedence chain :func:`_resolve` is built on.

    ``source`` is ``override`` (``VNX_OVERRIDE_<BARE>``), ``project_config`` (the DB layer),
    ``global_config`` (``~/.vnx/config.json`` via ``config_global``, ADR-087), ``env``
    (``VNX_<BARE>`` as the process started), ``default`` (the registry default) or
    ``unset`` (no value in any layer walked). With no ``resolver`` and
    ``use_default_resolver`` the process's wired store and default project are used, as
    :func:`get` does.

    ``strict_resolver`` (default ``False``) changes what a raising DB resolver means. Off, it is
    treated as "no row" and the chain falls through to env/default, as :func:`get` always did.
    On, the chain stops there and returns ``(None, "project_config_error")``: the DB state is
    unknown, so a caller whose safe answer is "off" can tell that apart from "no row". The
    override is still read first, before the DB is consulted. An unreadable global file is
    handled the same way: strict stops on ``(None, "global_config_error")``; not strict falls
    through to env with one WARNING per process. A missing global file is no value. A key with
    scope ``global`` skips the project layer; a project row for it is ignored with a WARNING.

    ``layers`` (default :data:`CHAIN_LAYERS`, the full chain) names the layers walked, always
    in chain order whatever order they are given in. A reader that must tell the approved
    floor apart from a loose override (ADR-088 besluit 3) resolves once with the floor layers
    and once with the full chain. An empty ``layers`` or a name outside :data:`CHAIN_LAYERS`
    raises ``ValueError``. Entries with a ``minimum`` validate the selected value and raise
    ``ValueError`` on invalid values or unavailable sources; their resolver is always strict.
    Unconstrained entries retain the existing resolution behavior.
    """
    walked = _checked_layers(layers)
    entry = CONFIG_REGISTRY.get(key)
    constrained = entry is not None and entry.minimum is not None
    strict_resolver = strict_resolver or constrained

    def checked(value, source):
        return entry.validate(value, source=source) if constrained else value

    override = os.environ.get(f"VNX_OVERRIDE_{_bare(key)}")
    if "override" in walked and override is not None:
        return checked(override, "override"), "override"
    if "project_config" in walked:
        if resolver is None and use_default_resolver:
            resolver = _db_resolver
            project_id = project_id if project_id is not None else _default_project_id
        if resolver is not None and entry is not None and entry.scope == "global":
            _warn_ignored_project_row(key, project_id, resolver)
        elif resolver is not None:
            try:
                db_val = resolver(project_id, key)
            except Exception as exc:
                if strict_resolver:
                    logging.getLogger(__name__).warning(
                        "config_registry: project_config resolver failed for %s (%s: %s)",
                        key, type(exc).__name__, exc)
                    return checked(None, "project_config_error"), "project_config_error"
                db_val = None
            if db_val is not None:
                return checked(db_val, "project_config"), "project_config"
    if "global_config" in walked and entry is not None:
        import config_global
        try:
            global_val = config_global.get(key)
        except config_global.GlobalConfigUnreadable as exc:
            if strict_resolver:
                logging.getLogger(__name__).warning("config_registry: %s", exc)
                return checked(None, "global_config_error"), "global_config_error"
            if (str(exc.path), exc.reason) not in _WARNED_GLOBAL_UNREADABLE:
                _WARNED_GLOBAL_UNREADABLE.add((str(exc.path), exc.reason))
                logging.getLogger(__name__).warning(
                    "config_registry: %s; falling through to env and the default", exc)
            global_val = None
        if global_val is not None:
            return checked(global_val, "global_config"), "global_config"
    env_val = os.environ.get(key)
    if "env" in walked and env_val is not None:
        return checked(env_val, "env"), "env"
    if "default" in walked and entry is not None:
        return checked(entry.default, "default"), "default"
    return checked(None, "unset"), "unset"


def _warn_ignored_project_row(key: str, project_id: Optional[str], resolver: DbResolver) -> None:
    """One WARNING per (project, key) when a scope-global key has a project row; never raises."""
    if (project_id, key) in _WARNED_IGNORED_PROJECT_ROW:
        return
    try:
        row = resolver(project_id, key)
    except Exception as exc:
        # Only a diagnostic: the project layer does not apply to a global key either way.
        logging.getLogger(__name__).debug("config_registry: no project-row check for global key %s "
                                          "(%s: %s)", key, type(exc).__name__, exc)
        return
    if row is not None:
        _WARNED_IGNORED_PROJECT_ROW.add((project_id, key))
        logging.getLogger(__name__).warning(
            "config_registry: project %s has a project_config row for %s, which has scope "
            "global; the row is ignored (only the global file counts)", project_id, key)


def _resolve(key: str, project_id: Optional[str], resolver: Optional[DbResolver]) -> Optional[str]:
    """The precedence chain: override > ``resolver``'s DB value > global file > env > default.

    ``resolver`` is the step-2 DB layer in play, or ``None`` for the env-only
    world. Shared by :func:`get` (the process's wired store) and
    :func:`get_for_project` (a store the caller names), so the order of the
    five sources cannot drift between them. The order lives in
    :func:`resolve_with_source`; this returns its value.
    """
    return resolve_with_source(key, project_id, resolver, use_default_resolver=False)[0]


def get(key: str, project_id: Optional[str] = None) -> Optional[str]:
    """Resolve a config value via the precedence chain. Returns the registry default (or None for
    an unknown key) when nothing overrides it. Raises ValueError for an invalid constrained value."""
    # The explicit project wins, else the process default (wired by config_runtime.autowire).
    pid = project_id if project_id is not None else _default_project_id
    return _resolve(key, pid, _db_resolver)


def get_for_project(key: str, project_id: str, state_dir: "Path | str") -> Optional[str]:
    """Resolve ``key`` for a project store the caller names, not for this process's project.

    Step 2 is read from ``state_dir``'s ``runtime_coordination.db`` for
    ``project_id``, so a caller working on another project's data (a ledger
    read from a state dir, ``--data-dir``) gets that project's value even when
    this process is wired to a different one. The operator override and the
    process-start env value keep the precedence :func:`get` gives them; only
    the DB source is redirected. Raises ValueError for an invalid constrained value.
    """
    import config_store_db

    entry = CONFIG_REGISTRY.get(key)
    constrained = entry is not None and entry.minimum is not None
    return _resolve(key, project_id, config_store_db.make_db_resolver(
        lambda _pid: state_dir, raise_on_error=constrained))


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
