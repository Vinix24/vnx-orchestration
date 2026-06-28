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
    "security-engineer": "02_code_review",
    "architect": "06_design",
    "planner": "06_design",
    "technical-writer": "04_documentation",
    "debugger": "05_debugging",
    "performance-profiler": "05_debugging",
}


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_task(
    instruction: str,
    role: Optional[str] = None,
    dispatch_paths: Optional[Sequence[str]] = None,
) -> str:
    """Classify a dispatch instruction into one of the 7 task classes.

    Priority:
      1. Instruction text matched against heuristic regex patterns (first match wins,
         ordered by task class number — code_gen checked before review, etc.)
      2. Role-based fallback if no regex matches
      3. Default: 01_code_generation (safest default — most dispatches are code work)

    dispatch_paths is reserved for future signal enrichment (e.g. docs-only paths
    → documentation class) but not used in the heuristic yet.
    """
    normalized = (instruction or "").strip()

    for task_class, pattern in _TASK_CLASS_PATTERNS:
        if pattern.search(normalized):
            return task_class

    if role:
        role_key = role.strip().lstrip("/").lower()
        mapped = ROLE_TO_TASK_CLASS.get(role_key)
        if mapped:
            return mapped

    return "01_code_generation"


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
