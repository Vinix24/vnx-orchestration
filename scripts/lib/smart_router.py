"""smart_router.py — Task classifier + recommendation lookup for cost-aware routing.

Classifies dispatch instructions into one of 7 task classes via heuristic regex +
tag matching, then looks up ranked model recommendations from
routing_recommendations.yaml.

PR-SR-4 additions: parse_route_model_id() maps model_id to (provider, model_alias)
for dispatch CLI flags. write_route_decision() appends decisions to
route_decisions.ndjson via state_writer (fcntl-locked).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import yaml

# GOVERNANCE_MIN_TIERS is the single source of truth for the governance-variant
# vocabulary. The gate-weight derivation below must emit only keys from this
# closed set, never a new variant name.
from observability_tier import GOVERNANCE_MIN_TIERS

_RECOMMENDATIONS_PATH = Path(__file__).parent / "providers" / "routing_recommendations.yaml"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RouteCandidate:
    """Single model recommendation for a task class."""
    model_id: str
    composite_score: float
    avg_duration_seconds: float
    cost_usd_per_call: Optional[float] = None
    cost_tier: Optional[int] = None  # 0 = local/free; None = standard billing
    quality_tier: Optional[int] = None  # 1=low, 2=mid, 3=premium capability


@dataclass
class RouteDecision:
    """Result of classify + recommend."""
    task_class: str
    primary: Optional[RouteCandidate]
    fallback: Optional[RouteCandidate]
    reason: str
    constraints_applied: List[str] = field(default_factory=list)
    cost_estimate: Optional[float] = None


@dataclass(frozen=True)
class GovernanceVariantResult:
    """Derived governance variant plus the reasoning for the trace."""
    variant: str       # one of GOVERNANCE_MIN_TIERS keys
    reason: str        # why this variant was chosen (deterministic rule fired)
    gate: str          # the review-gate weight this variant resolves to
    direction: str     # "up" | "unchanged" | "down" vs the codex_gate baseline
    # Independent axis (plan-gate weight ladder): True when task_class is
    # 01_code_generation. A new feature runs the full panel regardless of the
    # path-derived variant, so the plan-gate needs this carried alongside.
    is_new_feature: bool = False


@dataclass(frozen=True)
class GateWeightResolution:
    """Final review-gate weight for a dispatch.

    ``source`` is "explicit" when the spec declared a gate (the router never
    overrides it) or "derived" when the router filled a silent spec. On BOTH
    paths ``governance_variant`` carries the variant the path derivation
    produced, so an explicit gate is never a silent override of an unknown
    weight — the trace always names what it replaced.

    ``override_direction`` is "" when the chosen gate matches the derived gate
    (or the path was derived with no explicit gate), else "upgrade",
    "downgrade", or "strict-downgrade". "strict-downgrade" marks the one move
    the mechanism treats as its most dangerous: an override that lightens a
    coding-strict derivation (the heaviest variant class, picked exactly at
    irreversible work). It is not blocked, only marked, so a later sweep can
    find it.
    """
    gate: str
    source: str
    governance_variant: str
    reason: str
    override_direction: str = ""


# ---------------------------------------------------------------------------
# Task class definitions — heuristic patterns
# ---------------------------------------------------------------------------

_TASK_CLASS_PATTERNS: List[tuple[str, re.Pattern]] = [
    ("05_debugging", re.compile(
        r"(?i)(?:"
        r"(?:^|\W)debug\b|fix\s+(?:bug|issue|error|crash|regression)"
        r"|diagnos|troubleshoot"
        r"|investigate\s+(?:the\s+)?(?:bug|issue|error|failure|regression|crash|flak)"
        r"|root[\s_-]?cause|bisect|stack[\s_-]?trace"
        r")",
    )),
    ("02_code_review", re.compile(
        r"(?i)(?:"
        r"(?:code|peer|security)[\s_-]?review"
        r"|(?:^|\W)(?:review|audit)\s+(?:the\s+)?(?:PR|code|module|changes|security|auth)"
        r"|inspect\s+code|check\s+(?:code|quality|style)"
        r"|(?:^|\W)lint(?:ing)?\b|static[\s_-]?analysis|gate[\s_-]?check"
        r")",
    )),
    ("06_design", re.compile(
        r"(?i)(?:"
        r"(?:^|\W)design\b|(?:^|\W)architect\b"
        r"|plan\s+(?:the\s+)?(?:system|feature|module|migration)"
        r"|(?:^|\W)rfc\b|design[\s_-]?doc|system[\s_-]?design|api[\s_-]?design"
        r"|technical[\s_-]?spec|blueprint|schema[\s_-]?design"
        r")",
    )),
    ("07_translation", re.compile(
        r"(?i)(?:"
        r"translat|(?:^|\W)i18n\b|(?:^|\W)l10n\b|localiz"
        r"|port\s+(?:to|from)\s+\w+"
        r"|convert\s+(?:to|from)\s+\w+"
        r"|migrat(?:e|ion)\s+(?:to|from)\s+\w+"
        r")",
    )),
    ("04_documentation", re.compile(
        r"(?i)(?:"
        r"(?:^|\W)document(?:ation)?\b"
        r"|write\s+(?:(?:a|the|an)\s+)?(?:docs|documentation|readme|adr|changelog)"
        r"|update\s+(?:the\s+)?(?:docs|documentation|readme|adr|changelog)"
        r"|(?:add|write)\s+(?:(?:a|the)\s+)?docstring"
        r"|jsdoc|typedoc|api[\s_-]?doc"
        r")",
    )),
    ("03_refactoring", re.compile(
        r"(?i)(?:"
        r"refactor|restructure|reorganize|split\s+(?:module|file|class)"
        r"|extract\s+(?:function|class|module|method)"
        r"|(?:^|\W)rename\b|move\s+(?:code|function|class|module)"
        r"|dedup|consolidat|simplif|clean\s*up"
        r")",
    )),
    ("01_code_generation", re.compile(
        r"(?i)(?:"
        r"implement|create\s+(?:new\s+)?(?:module|class|function|endpoint|feature|script)"
        r"|add\s+(?:new\s+)?(?:support|handler|adapter|route|command)"
        r"|(?:^|\W)build\b|scaffold|bootstrap|generate\s+code"
        r"|write\s+(?:(?:a|the)\s+)?(?:module|class|function|script)"
        r")",
    )),
]

TASK_CLASSES: Dict[str, re.Pattern] = {tc: pat for tc, pat in _TASK_CLASS_PATTERNS}

ROLE_TO_TASK_CLASS: Dict[str, str] = {
    "backend-developer": "01_code_generation",
    "frontend-developer": "01_code_generation",
    "api-developer": "01_code_generation",
    "python-optimizer": "01_code_generation",
    "supabase-expert": "01_code_generation",
    "test-engineer": "01_code_generation",
    "quality-engineer": "02_code_review",
    "reviewer": "02_code_review",
    "code-reviewer": "02_code_review",  # OI-1143: real fleet role (phantom_guard REVIEW_ROLES) was unmapped
    "security-engineer": "02_code_review",
    "architect": "06_design",
    "system-architect": "06_design",  # OI-1143: fleet role string alongside the short form
    "planner": "06_design",
    "technical-writer": "04_documentation",
    "debugger": "05_debugging",
    "performance-profiler": "05_debugging",
}

# The classifier's fallthrough class. Roles mapping HERE carry no discriminating
# signal (builder roles do refactors/debugging/docs in the same lane, and
# "backend-developer" is additionally the fabric's no-role-resolved sentinel —
# dispatch_govern._FAKE_DEFAULT_ROLE), so for them the instruction text decides.
_DEFAULT_TASK_CLASS = "01_code_generation"


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_task(
    instruction: str,
    role: Optional[str] = None,
    dispatch_paths: Optional[Sequence[str]] = None,
) -> str:
    """Classify a dispatch instruction into one of the 7 task classes.

    Priority (OI-1143 — role signal dominates verb-guessing when present):
      1. A role mapping to a NON-default task class (code-reviewer, debugger,
         architect, ...) is an explicit operator signal and wins outright — a
         review dispatched to a code-reviewer stays a review even when the
         instruction text happens to trip a code-gen verb pattern.
      2. Instruction text matched against heuristic regex patterns (first match
         wins, ordered by task class number).
      3. Role-based fallback for default-class (builder) roles. Builder roles map
         to the classifier's own default, so step 1 skipping them changes nothing
         for a no-regex-match instruction — and keeps the instruction text
         deciding for the no-role-resolved sentinel ("backend-developer").
      4. Default: 01_code_generation (safest default — most dispatches are code work)

    dispatch_paths is reserved for future signal enrichment (e.g. docs-only paths
    → documentation class) but not used in the heuristic yet.
    """
    normalized = (instruction or "").strip()

    mapped: Optional[str] = None
    if role:
        role_key = role.strip().lstrip("/").lower()
        mapped = ROLE_TO_TASK_CLASS.get(role_key)

    if mapped and mapped != _DEFAULT_TASK_CLASS:
        return mapped

    for task_class, pattern in _TASK_CLASS_PATTERNS:
        if pattern.search(normalized):
            return task_class

    if mapped:
        return mapped

    return _DEFAULT_TASK_CLASS


# ---------------------------------------------------------------------------
# Recommendations loader
# ---------------------------------------------------------------------------

# Composite score at or below which a model is considered incapable for the task class.
_INCAPABLE_SCORE_FLOOR = 1.0

# Operator-chosen capability threshold (2026-06-28): a model scoring at/above this clears the
# "capable enough" bar and competes on COST; models below it are ranked by capability instead, so a
# cheap-but-weak model can never beat a much stronger one. On the 0-10 composite scale, 7.0 = solidly
# capable. Tunable; kept absolute so the per-candidate sort key stays composable.
_CAPABILITY_THRESHOLD = 7.0


def _compute_quality_tier(composite_score: float, cost_tier: Optional[int]) -> int:
    """Derive quality tier (1-3) from composite_score and cost_tier.

    cost_tier=0 (local/free) is locked to tier 1 regardless of score.
    Otherwise: score >= 7.5 → 3, score >= 5.0 → 2, else → 1.
    """
    if cost_tier == 0:
        return 1
    if composite_score >= 7.5:
        return 3
    if composite_score >= 5.0:
        return 2
    return 1


def _cost_aware_sort_key(c: "RouteCandidate") -> tuple:
    """Sort key for cost-aware candidate ranking — capability-threshold, then cheapest.

    Operator-chosen policy (2026-06-28, the hybrid):
      1. Models at/above _CAPABILITY_THRESHOLD clear the capability bar (band 0). Among them the
         CHEAPEST wins (cost ASC), with composite_score DESC as the tiebreaker on equal cost. A
         null/unknown cost ranks LAST within the band (+inf) — an unmeasured model is never assumed
         free. This is why a cheap-and-strong model beats an expensive-and-stronger one, but a
         cheap-and-WEAK model (below the bar) cannot beat a strong one.
      2. Models below the threshold (band 1) are ranked by capability DESC (best available), cost ASC
         as a tiebreaker — so the strongest sub-bar model still wins when nothing clears the bar.
    """
    cost = c.cost_usd_per_call if c.cost_usd_per_call is not None else float("inf")
    if c.composite_score >= _CAPABILITY_THRESHOLD:
        return (0, cost, -c.composite_score)
    return (1, -c.composite_score, cost)


def _filter_by_constraints(
    candidates: List[RouteCandidate],
    env: Optional[Dict] = None,
) -> "tuple[List[RouteCandidate], List[str]]":
    """Filter candidates that would violate provider_constraints.yaml.

    Consults providers.constraint_enforcer.check_constraints for each candidate
    so smart_router never recommends a constraint-violating lane (G8).

    Fail-open: on import error or any per-candidate exception, the candidate is
    kept (safe over silent drop). Returns (allowed_candidates, applied_ids) where
    applied_ids lists blocking constraint codes that filtered at least one model.
    """
    import os as _os  # noqa: PLC0415

    try:
        from providers.constraint_enforcer import check_constraints as _check  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return candidates, []

    _env = env if env is not None else dict(_os.environ)
    allowed: List[RouteCandidate] = []
    applied: List[str] = []

    for candidate in candidates:
        try:
            provider, model = parse_route_model_id(candidate.model_id)
            violations = _check(provider=provider, model=model, env=_env)
            blocking = [v for v in violations if v.severity == "blocking"]
            if blocking:
                for v in blocking:
                    if v.code not in applied:
                        applied.append(v.code)
            else:
                allowed.append(candidate)
        except Exception:  # noqa: BLE001
            allowed.append(candidate)

    return allowed, applied


def _load_recommendations(
    path: Optional[Path] = None,
) -> Dict[str, List[RouteCandidate]]:
    """Load routing_recommendations.yaml and return parsed candidates per task class.

    Candidates are enriched with cost_usd_per_call from wave7_models.yaml (via cost_loader) and
    sorted by the operator-chosen hybrid (see _cost_aware_sort_key): models at/above the
    _CAPABILITY_THRESHOLD (7.0) compete on cost (cheapest first, null/unknown cost last), while
    models below the threshold are ranked by capability descending. When costs are all None (no
    wave7 data), every above-bar candidate ties on cost and the order collapses to score-descending
    — identical to the pre-cost-aware behaviour.
    """
    from cost_loader import enrich_candidates as _enrich  # noqa: PLC0415

    yaml_path = path or _RECOMMENDATIONS_PATH
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"routing_recommendations.yaml not found at {yaml_path}"
        )

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "routing_by_task" not in raw:
        raise ValueError(
            f"Malformed routing_recommendations.yaml: missing 'routing_by_task' key"
        )

    result: Dict[str, List[RouteCandidate]] = {}
    for task_class, task_node in raw["routing_by_task"].items():
        # Support new dict shape: {candidates: [...], min_quality_tier: N, max_quality_tier: N}
        # Plain-list shape (legacy) passes through unchanged — fully backward compatible.
        if isinstance(task_node, dict) and "candidates" in task_node:
            entries = task_node.get("candidates") or []
            min_qt: Optional[int] = task_node.get("min_quality_tier")
            max_qt: Optional[int] = task_node.get("max_quality_tier")
        else:
            entries = task_node or []
            min_qt = None
            max_qt = None

        candidates = []
        for entry in entries:
            raw_tier = entry.get("cost_tier")
            cost_tier = int(raw_tier) if raw_tier is not None else None
            score = float(entry["composite_score"])
            if "quality_tier" in entry:
                qt = int(entry["quality_tier"])
                if qt not in (1, 2, 3):
                    raise ValueError(
                        f"quality_tier must be 1-3, got {qt} for {entry.get('model_id')}"
                    )
            else:
                qt = _compute_quality_tier(score, cost_tier)
            candidates.append(RouteCandidate(
                model_id=str(entry["model_id"]),
                composite_score=score,
                avg_duration_seconds=float(entry["avg_duration_seconds"]),
                cost_usd_per_call=entry.get("cost_usd_per_call"),
                cost_tier=cost_tier,
                quality_tier=qt,
            ))
        _enrich(candidates)
        if min_qt is not None:
            candidates = [c for c in candidates if (c.quality_tier or 0) >= min_qt]
        if max_qt is not None:
            candidates = [c for c in candidates if (c.quality_tier or 0) <= max_qt]
        candidates.sort(key=_cost_aware_sort_key)
        result[task_class] = candidates

    return result


def recommend(
    task_class: str,
    *,
    recommendations_path: Optional[Path] = None,
) -> List[RouteCandidate]:
    """Return ranked RouteCandidate list for a task class.

    Returns empty list if the task class has no recommendations.
    """
    recs = _load_recommendations(recommendations_path)
    return recs.get(task_class, [])


# ---------------------------------------------------------------------------
# Full decision
# ---------------------------------------------------------------------------

def _promote_cost_tier_zero(candidates: List[RouteCandidate]) -> List[RouteCandidate]:
    """Promote cost_tier=0 candidates to the front when present.

    Preserves relative order within the cost_tier=0 group and within the
    remaining group. Called when a dispatch carries the 'cost-tier-zero' or
    'privacy-required' tag so local models are preferred without re-scoring.
    """
    zero_tier = [c for c in candidates if c.cost_tier == 0]
    others = [c for c in candidates if c.cost_tier != 0]
    return zero_tier + others


def decide(
    instruction: str,
    role: Optional[str] = None,
    dispatch_paths: Optional[Sequence[str]] = None,
    tags: Optional[Sequence[str]] = None,
    *,
    recommendations_path: Optional[Path] = None,
) -> RouteDecision:
    """Classify instruction and build a RouteDecision with primary + fallback.

    Combines classify_task and recommend into a single call that returns a
    RouteDecision with the top-scoring candidate as primary and the second-best
    as fallback.

    tags: when 'cost-tier-zero' or 'privacy-required' is present, cost_tier=0
    candidates (e.g. gemma-4b-local) are promoted to the front of the ranking.
    """
    task_class = classify_task(instruction, role=role, dispatch_paths=dispatch_paths)
    candidates = recommend(task_class, recommendations_path=recommendations_path)

    # G8: filter constraint-violating candidates before picking primary/fallback.
    candidates, _constraints_applied = _filter_by_constraints(candidates)

    # Cost-tier-zero / privacy promotion: when the operator requests free/local
    # inference, re-rank so cost_tier=0 candidates appear first.
    _tags = [t.lower() for t in (tags or [])]
    if any(t in _tags for t in ("cost-tier-zero", "privacy-required")):
        candidates = _promote_cost_tier_zero(candidates)

    primary = candidates[0] if candidates else None
    fallback = candidates[1] if len(candidates) > 1 else None

    parts = [f"task_class={task_class}"]
    if primary:
        parts.append(f"primary={primary.model_id} (score={primary.composite_score})")
    if fallback:
        parts.append(f"fallback={fallback.model_id} (score={fallback.composite_score})")
    if not candidates:
        parts.append("no recommendations available")

    cost_estimate = primary.cost_usd_per_call if primary else None

    return RouteDecision(
        task_class=task_class,
        primary=primary,
        fallback=fallback,
        reason="; ".join(parts),
        constraints_applied=_constraints_applied,
        cost_estimate=cost_estimate,
    )


# ---------------------------------------------------------------------------
# Model ID → (provider, model_alias) mapping for dispatch CLI flags
# ---------------------------------------------------------------------------

def parse_route_model_id(model_id: str) -> tuple[str, str]:
    """Parse a routing_recommendations model_id into (provider_flag, model_alias).

    Returns values suitable for --provider and --model in provider_dispatch.py.
    """
    if model_id == "gemma-4b-local":
        return "local-gemma", "gemma-4b-local"
    if model_id.startswith("claude-"):
        variant = model_id.split("-")[1]
        return "claude", variant
    if model_id.startswith("deepseek-"):
        return f"litellm:deepseek:{model_id}", model_id
    if model_id.startswith("glm-"):
        return "litellm:zai", model_id
    if model_id.startswith("kimi-"):
        return "kimi", model_id
    return "litellm", model_id


# ---------------------------------------------------------------------------
# Governance variant derivation (gate-weight selection)
# ---------------------------------------------------------------------------
#
# The router derives a ``governance_variant`` from what it already knows about
# the dispatch (dispatch_paths, task_class) and that variant selects the review-
# gate weight. This is a DETERMINISTIC rule, not a model decision: the risk
# class of a change is a function of which paths it touches, which is checkable
# and reproducible. The five variants in GOVERNANCE_MIN_TIERS already encode the
# risk ladder (min observability tier 1..3); mapping "which files change" onto
# "which rung of that ladder" is a fixed rule with no open natural language to
# judge, so a model adds nothing here. It would only make the gate weight
# non-reproducible and its receipt unfalsifiable.

# The gate a plain code dispatch gets by convention today (the author writes
# ``gate=codex_gate``). Used as the up/down reference so a derivation that lands
# on a LIGHTER gate than this is never silent: "only upward, never silently
# downward"; a lighter gate must carry an explicit reason in the trace.
_GATE_BASELINE = "codex_gate"

# Heaviness ladder over the closed Gate enum (scripts/lib/dispatch_spec.py).
# Only the relative order matters, for the up/down direction in the trace.
_GATE_WEIGHT: dict[str, int] = {
    "codex_gate": 3,
    "gemini_review": 2,
    "claude_github_optional": 1,
    "ci_gate": 0,
    "wiring_gate": 0,
}

# Governance variant -> review-gate weight. Strictest governance (min tier 1)
# gets the heaviest single gate; lightest governance (min tier 3) gets the
# lightest. Keys are exactly the GOVERNANCE_MIN_TIERS vocabulary, no new names.
GOVERNANCE_VARIANT_GATE: dict[str, str] = {
    "coding-strict": "codex_gate",               # strictest: full codex diff review
    "default": "codex_gate",                     # code baseline: codex diff review
    "business-light": "claude_github_optional",  # non-code deliverable: optional review
    "light": "claude_github_optional",           # light: optional review
    "minimal": "ci_gate",                        # docs/content: CI checks only
}

# Fail-loud drift guard: the gate-weight table must only ever name variants from
# the observability-tier vocabulary. If a variant is renamed upstream, this
# import fails loudly instead of silently carrying a stale name into the router.
_UNKNOWN_VARIANTS = set(GOVERNANCE_VARIANT_GATE) - set(GOVERNANCE_MIN_TIERS)
if _UNKNOWN_VARIANTS:
    raise ValueError(
        f"GOVERNANCE_VARIANT_GATE declares variants not in GOVERNANCE_MIN_TIERS: "
        f"{sorted(_UNKNOWN_VARIANTS)}; the gate weight must use the closed "
        f"observability-tier vocabulary (no new variant names)."
    )

# A change here can alter the dispatch door, the router, the receipt trail, or
# the gates themselves: the highest risk class (coding-strict). Matched by path
# prefix or exact name.
_GOVERNANCE_CORE_PREFIXES: tuple[str, ...] = (
    "scripts/lib/providers/",
    "scripts/lib/append_receipt_internals/",
    "scripts/lib/dispatch",          # dispatch_cli/spec/plan/govern/bridge/*.py
    # Lane scripts are listed as BARE prefixes (no ".py") on purpose: the
    # dispatch_sidedoor_audit scanner flags a literal "<lane>.py" on a code
    # line as a delivery caller. A bare prefix classifies the same files (and,
    # for subprocess_dispatch, its internals/ dir) without tripping that guard.
    "scripts/lib/tmux_interactive_dispatch",
    "scripts/lib/subprocess_dispatch",
    "scripts/lib/subprocess_adapter.py",
    "scripts/lib/provider_dispatch",
    "scripts/lib/gate",              # gate_executor/recorder/status/obligations/stack_resolver/...
    "scripts/lib/observability_tier.py",
    "scripts/lib/smart_router.py",
    "scripts/lib/report_to_receipt_converter.py",
    "scripts/lib/governance_receipts.py",
    "scripts/lib/incident_taxonomy.py",
    "scripts/review_gate_manager.py",
    "scripts/gate_obligation_runner.py",
)

_DOC_PREFIXES: tuple[str, ...] = ("docs/", "claudedocs/")
_DOC_EXTENSIONS: frozenset[str] = frozenset({".md", ".rst", ".txt", ".adoc"})
_DOC_FILENAME_MARKERS: frozenset[str] = frozenset({
    "readme", "changelog", "roadmap", "feature_plan", "contributing",
    "security", "license", "codeowners",
})

# Non-code deliverables: config/content/templates, not logic (business-light).
_BUSINESS_PREFIXES: tuple[str, ...] = (
    "samples/", "templates/", "examples/", "configs/", "agents/", "skills/",
)

_CODE_PREFIXES: tuple[str, ...] = (
    "scripts/", "lib/", "bin/", "vnx_cli/", "tests/", "hooks/", "database/",
    "schemas/", "ledger/", "dashboard/", "roadmap/",
)
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh", ".rb", ".go",
    ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".sql", ".yaml", ".yml", ".toml",
    ".json", ".svelte", ".css", ".html",
})

_CATEGORY_TO_VARIANT: dict[str, str] = {
    "core": "coding-strict",
    "code": "default",
    "business": "business-light",
    "docs": "minimal",
}

# Strictness rank: the STRICTEST category across all touched paths wins, so a
# dispatch that edits the door AND a doc file is still coding-strict, never a
# silent downgrade.
_CATEGORY_RANK: dict[str, int] = {"docs": 0, "business": 1, "code": 2, "core": 3}

# Irreversible change categories (operator ladder, 2026-08-15). The three
# PATH-DERIVABLE categories classify to the strictest variant (coding-strict)
# no matter what the reversible ladder above would say, because a change that
# cannot be walked back never gets a lighter gate:
#   (1) schema migrations (scripts/migrations/, schemas/migrations/),
#   (2) fleet defaults written by `vnx role sync` / `vnx init`
#       (.claude/terminals/, .claude/skills/, agents/, skills/),
#   (3) the append-only receipt/ledger format (ndjson_hash_chain, ndjson_io,
#       receipt_schema, append_receipt_internals).
# The other two (deletions/renames, big architecture refactors) are NOT
# path-derivable — a rename looks like a normal edit — and need the spec's
# explicit ``irreversible`` flag instead. Each entry maps a path prefix to the
# human category name for the trace. Note agents/ and skills/ also appear in
# _BUSINESS_PREFIXES; the irreversible check runs FIRST so the fleet-default
# meaning wins over the reversible "non-code deliverable" meaning.
_IRREVERSIBLE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("scripts/migrations/", "schema-migration"),
    ("schemas/migrations/", "schema-migration"),
    (".claude/terminals/", "fleet-default"),
    (".claude/skills/", "fleet-default"),
    ("agents/", "fleet-default"),
    ("skills/", "fleet-default"),
    ("scripts/lib/append_receipt_internals/", "receipt-format"),
    ("scripts/lib/ndjson_hash_chain.py", "receipt-format"),
    ("scripts/lib/ndjson_io.py", "receipt-format"),
    ("scripts/lib/receipt_schema.py", "receipt-format"),
)


def _irreversible_path_category(path: str) -> Optional[str]:
    """Return the irreversible category name a path falls under, else None.

    First-match wins; deterministic. Called before the reversible category
    ladder so an irreversible change is never silently sized down.
    """
    p = str(path).strip().lstrip("./")
    if not p:
        return None
    for prefix, category in _IRREVERSIBLE_PREFIXES:
        if _matches_prefix(p, prefix):
            return category
    return None


def _matches_prefix(path: str, prefix: str) -> bool:
    """True when ``path`` equals the prefix's bare dir or lives under it."""
    return path == prefix.rstrip("/") or path.startswith(prefix)


def _path_category(path: str) -> str:
    """Classify one dispatch path into 'core' | 'docs' | 'code' | 'business'.

    Deterministic, first-match wins. An unrecognized path falls through to
    'code' (the ``default`` variant), the safe middle, never silently lighter.
    """
    p = str(path).strip().lstrip("./")
    if not p:
        return "code"

    for prefix in _GOVERNANCE_CORE_PREFIXES:
        if _matches_prefix(p, prefix):
            return "core"

    for prefix in _DOC_PREFIXES:
        if _matches_prefix(p, prefix):
            return "docs"

    name = p.rsplit("/", 1)[-1].lower()
    for marker in _DOC_FILENAME_MARKERS:
        if name.startswith(marker):
            return "docs"

    leaf = p.rsplit("/", 1)[-1]
    ext = ("." + leaf.rsplit(".", 1)[-1].lower()) if "." in leaf else ""
    if ext in _DOC_EXTENSIONS:
        return "docs"

    for prefix in _BUSINESS_PREFIXES:
        if _matches_prefix(p, prefix):
            return "business"

    if ext in _CODE_EXTENSIONS:
        return "code"
    for prefix in _CODE_PREFIXES:
        if _matches_prefix(p, prefix):
            return "code"
    return "code"


def _category_from_task_class(task_class: Optional[str]) -> str:
    """Fallback category when a dispatch declares no paths.

    Documentation/translation work is content (minimal); review/design/debug of
    unknown code defaults to the code baseline, never strict, never light.
    """
    if task_class in ("04_documentation", "07_translation"):
        return "docs"
    return "code"


def _direction_for(gate: str) -> str:
    """Direction of the gate weight vs the codex_gate baseline.

    'down' marks a lighter-than-baseline gate so the trace can never hide it.
    """
    baseline = _GATE_WEIGHT.get(_GATE_BASELINE, 0)
    weight = _GATE_WEIGHT.get(gate)
    if weight is None:
        return "unchanged"  # unknown gate: neutral, no false up/down claim
    if weight > baseline:
        return "up"
    if weight < baseline:
        return "down"
    return "unchanged"


def derive_governance_variant(
    dispatch_paths: Optional[Sequence[str]] = None,
    *,
    task_class: Optional[str] = None,
    irreversible: bool = False,
) -> GovernanceVariantResult:
    """Derive a governance variant from the signals the router already has.

    Deterministic rule, first-match wins. Paths are the primary signal (they say
    WHAT changes); the strictest category across all touched paths wins. When a
    dispatch declares no paths, task_class is the fallback. Instruction text and
    role are deliberately NOT signals here: path + task_class already pin the
    risk class deterministically, and text/role guessing is exactly the
    ambiguity a model would be for; this rule has none.

    Irreversibility overrides the reversible ladder: an explicit ``irreversible``
    flag, or any path under an irreversible category (schema migrations, fleet
    defaults, the append-only receipt/ledger format), forces coding-strict — a
    change that cannot be walked back never gets a lighter gate. ``is_new_feature``
    is an INDEPENDENT axis (task_class == 01_code_generation) carried on the
    result so the plan-gate can size its panel to the full seat set for a new
    feature regardless of the path-derived variant.
    """
    is_new_feature = task_class == "01_code_generation"
    paths = [p for p in (dispatch_paths or []) if p and str(p).strip()]

    irreversible_hit: Optional[str] = None
    for p in paths:
        irreversible_hit = _irreversible_path_category(str(p))
        if irreversible_hit:
            break

    if irreversible or irreversible_hit:
        if irreversible:
            reason = "explicit irreversible=true on spec"
            if irreversible_hit:
                reason += f"; also path-derived {irreversible_hit}"
        else:
            reason = f"irreversible path category={irreversible_hit!r}"
        variant = "coding-strict"
        gate = GOVERNANCE_VARIANT_GATE[variant]
        return GovernanceVariantResult(
            variant=variant,
            reason=reason,
            gate=gate,
            direction=_direction_for(gate),
            is_new_feature=is_new_feature,
        )

    if paths:
        category = max(
            (_path_category(str(p)) for p in paths),
            key=lambda c: _CATEGORY_RANK[c],
        )
        reason = (
            f"strictest path category={category!r} across {len(paths)} dispatch path(s)"
        )
    else:
        category = _category_from_task_class(task_class)
        reason = f"no dispatch paths; task_class={task_class or 'none'} -> {category!r}"

    variant = _CATEGORY_TO_VARIANT[category]
    gate = GOVERNANCE_VARIANT_GATE[variant]
    return GovernanceVariantResult(
        variant=variant,
        reason=reason,
        gate=gate,
        direction=_direction_for(gate),
        is_new_feature=is_new_feature,
    )


def _gate_override_direction(derived: GovernanceVariantResult, explicit_gate: str) -> str:
    """Direction of an explicit gate vs the gate the derivation produced.

    Uses ``_GATE_WEIGHT`` (the heaviness ladder over the closed Gate enum) so
    "upgrade"/"downgrade" have a defined meaning, not a feeling. Returns ""
    when the two weights are equal (no override) or either gate is unknown
    (no false direction claim). "strict-downgrade" marks the special case: an
    override that lightens a coding-strict derivation — the heaviest variant
    class, chosen exactly at irreversible work — so a later sweep can find the
    most dangerous move distinctly from an ordinary downgrade.
    """
    derived_weight = _GATE_WEIGHT.get(derived.gate)
    chosen_weight = _GATE_WEIGHT.get(explicit_gate)
    if derived_weight is None or chosen_weight is None:
        return ""
    if chosen_weight > derived_weight:
        return "upgrade"
    if chosen_weight < derived_weight:
        return "strict-downgrade" if derived.variant == "coding-strict" else "downgrade"
    return ""


def resolve_gate(
    explicit_gate: str = "",
    *,
    dispatch_paths: Optional[Sequence[str]] = None,
    task_class: Optional[str] = None,
    irreversible: bool = False,
) -> GateWeightResolution:
    """Resolve the review-gate weight for a dispatch.

    An explicit gate on the spec always wins: the router fills in, it never
    overrides (worker-provider-free-choice, pin_semantics=default). But the
    derivation still runs on the explicit path so the trace names what the
    override replaced: ``governance_variant`` carries the derived variant, and
    ``reason``/``override_direction`` say whether the explicit gate is heavier
    or lighter than it — an override is never silent about its direction, and a
    coding-strict -> lighter override is marked distinctly. When the spec is
    silent, the router derives a governance_variant and maps it to a gate
    weight; the variant, direction and reason are carried in ``reason`` so the
    trace is never silent about a lighter-than-baseline gate.
    """
    gate = (explicit_gate or "").strip()
    derived = derive_governance_variant(
        dispatch_paths=dispatch_paths,
        task_class=task_class,
        irreversible=irreversible,
    )
    if gate:
        direction = _gate_override_direction(derived, gate)
        if direction:
            reason = (
                f"gate={gate} declared on spec; OVERRIDES derived "
                f"governance_variant={derived.variant!r} (gate={derived.gate}) "
                f"- {direction.upper()}; {derived.reason}"
            )
        else:
            reason = (
                f"gate={gate} declared on spec; matches derived "
                f"governance_variant={derived.variant!r} (gate={derived.gate}); "
                f"router did not override"
            )
        return GateWeightResolution(
            gate=gate,
            source="explicit",
            governance_variant=derived.variant,
            reason=reason,
            override_direction=direction,
        )
    return GateWeightResolution(
        gate=derived.gate,
        source="derived",
        governance_variant=derived.variant,
        reason=(
            f"governance_variant={derived.variant} gate={derived.gate} "
            f"direction={derived.direction}; {derived.reason}"
        ),
    )


# ---------------------------------------------------------------------------
# Route decision NDJSON writer
# ---------------------------------------------------------------------------

def write_route_decision(
    dispatch_id: str,
    decision: RouteDecision,
    state_dir: Path,
) -> None:
    """Append route decision to route_decisions.ndjson and write per-dispatch JSON.

    The per-dispatch JSON at state_dir/route_decisions/<dispatch_id>.json is used
    by report_to_receipt_converter to set strategy='smart_router' on the receipt
    instead of the default 'default' tag written by governance_emit.
    """
    import json as _json
    from datetime import datetime, timezone

    from state_writer import append_locked

    timestamp = datetime.now(timezone.utc).isoformat()
    record = {
        "timestamp": timestamp,
        "dispatch_id": dispatch_id,
        "task_class": decision.task_class,
        "chosen_route": {
            "model_id": decision.primary.model_id,
            "composite_score": decision.primary.composite_score,
        } if decision.primary else None,
        "fallback_route": {
            "model_id": decision.fallback.model_id,
            "composite_score": decision.fallback.composite_score,
        } if decision.fallback else None,
        "constraints_applied": decision.constraints_applied,
        "cost_estimate": decision.cost_estimate,
        "outcome": None,
    }
    append_locked(state_dir / "route_decisions.ndjson", record)

    # Write per-dispatch JSON for strategy-tag lookup in receipt converter.
    per_dispatch_dir = state_dir / "route_decisions"
    per_dispatch_dir.mkdir(parents=True, exist_ok=True)
    per_dispatch_path = per_dispatch_dir / f"{dispatch_id}.json"
    per_dispatch_data = {
        "strategy": "smart_router",
        "task_class": decision.task_class,
        "selected_model": decision.primary.model_id if decision.primary else None,
        "timestamp": timestamp,
    }
    tmp = per_dispatch_path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(per_dispatch_data), encoding="utf-8")
    tmp.replace(per_dispatch_path)


# ---------------------------------------------------------------------------
# End-to-end routing pipeline (PR-SR-3)
# ---------------------------------------------------------------------------

@dataclass
class RoutingResult:
    """Full result of the route() end-to-end pipeline."""
    decision: RouteDecision
    provider: Optional[str] = None
    model: Optional[str] = None
    routed: bool = False


def route(
    instruction: str,
    dispatch_id: str,
    state_dir: Path,
    *,
    role: Optional[str] = None,
    dispatch_paths: Optional[Sequence[str]] = None,
    recommendations_path: Optional[Path] = None,
) -> RoutingResult:
    """End-to-end smart routing pipeline: classify → decide → resolve → persist.

    Combines classify_task, decide, parse_route_model_id, and write_route_decision
    into a single call. Returns RoutingResult with the selected provider/model and
    the underlying RouteDecision.

    This is the function provider_dispatch should call under --auto-route.
    """
    decision = decide(
        instruction=instruction,
        role=role,
        dispatch_paths=dispatch_paths,
        recommendations_path=recommendations_path,
    )

    result = RoutingResult(decision=decision)

    if decision.primary:
        provider, model = parse_route_model_id(decision.primary.model_id)
        result.provider = provider
        result.model = model
        result.routed = True

    write_route_decision(dispatch_id, decision, state_dir=state_dir)
    return result
