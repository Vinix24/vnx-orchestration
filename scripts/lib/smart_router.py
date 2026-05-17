"""smart_router.py — Task classifier + recommendation lookup for cost-aware routing.

Pure-function module. Classifies dispatch instructions into one of 7 task classes
via heuristic regex + tag matching, then looks up ranked model recommendations
from routing_recommendations.yaml.

No side effects beyond reading the YAML file. No wiring into provider_dispatch —
that is PR-SR-4 scope.
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

def _load_recommendations(
    path: Optional[Path] = None,
) -> Dict[str, List[RouteCandidate]]:
    """Load routing_recommendations.yaml and return parsed candidates per task class."""
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
    for task_class, entries in raw["routing_by_task"].items():
        candidates = []
        for entry in (entries or []):
            candidates.append(RouteCandidate(
                model_id=str(entry["model_id"]),
                composite_score=float(entry["composite_score"]),
                avg_duration_seconds=float(entry["avg_duration_seconds"]),
                cost_usd_per_call=entry.get("cost_usd_per_call"),
            ))
        candidates.sort(key=lambda c: c.composite_score, reverse=True)
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

def decide(
    instruction: str,
    role: Optional[str] = None,
    dispatch_paths: Optional[Sequence[str]] = None,
    *,
    recommendations_path: Optional[Path] = None,
) -> RouteDecision:
    """Classify instruction and build a RouteDecision with primary + fallback.

    Combines classify_task and recommend into a single call that returns a
    RouteDecision with the top-scoring candidate as primary and the second-best
    as fallback.
    """
    task_class = classify_task(instruction, role=role, dispatch_paths=dispatch_paths)
    candidates = recommend(task_class, recommendations_path=recommendations_path)

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
        cost_estimate=cost_estimate,
    )
