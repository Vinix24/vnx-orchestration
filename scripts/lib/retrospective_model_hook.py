#!/usr/bin/env python3
"""Optional local-model-assisted retrospective analysis hook (Feature 18, PR-3).

Provides an optional hook interface so VNX can invoke a local model to
summarize repeated failures and propose candidate guardrails — without
granting that model any governance authority.

Architecture invariants:
- Hook is OPTIONAL. Fallback behavior is explicit and clean.
- Model output is NEVER authoritative. RetroAnalysisSummary.authoritative
  is always False; setting it True raises ValueError.
- Evidence pointers in model output must originate from the input digest.
  The hook contract specifies this; validate_summary() enforces it at runtime.
- Candidate guardrails are PROPOSALS only. They require T0 review before
  any effect. The field name makes this explicit.
- Confidence annotation is required. The model (or fallback) must declare
  its confidence level explicitly.

Data flow:
  RetroDigest (PR-2)
    → RetroAnalysisInput
    → LocalModelHook.analyze()   (optional; fallback if absent)
    → RetroAnalysisSummary       (non-authoritative, evidence-linked)

Usage:
  hook = MyLocalHook(model_path="...")   # optional
  summary = run_retrospective_hook(input_data, hook=hook)
  if summary.fallback:
      # no model configured; summary is rule-based
  # always: summary.authoritative is False
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})

# Maximum candidate guardrails a hook may propose per analysis
MAX_CANDIDATE_GUARDRAILS = 10

# Fallback confidence when no model is configured
FALLBACK_CONFIDENCE = "low"


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------

@dataclass
class RetroAnalysisInput:
    """Structured input bundle for local-model retrospective analysis.

    Attributes:
        digest:            RetroDigest from retrospective_digest.build_digest().
                           Provides recurring patterns, evidence, and counts.
        context_hint:      Optional domain context (e.g. "Feature 18 PRs").
                           Helps model produce more relevant summaries.
        max_summary_chars: Bound on model-produced summary text.
    """
    digest: Any              # RetroDigest (duck-typed to avoid circular import)
    context_hint: str = ""
    max_summary_chars: int = 500

    def evidence_pool(self) -> List[str]:
        """Collect all evidence pointers from the digest's recurring patterns."""
        pool: List[str] = []
        for pattern in getattr(self.digest, "recurring_patterns", []):
            pool.extend(getattr(pattern, "evidence_pointers", []))
        seen: set = set()
        return [p for p in pool if p and not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

@dataclass
class RetroAnalysisSummary:
    """Non-authoritative model-produced retrospective summary.

    INVARIANT: authoritative is always False. The model proposes; T0 decides.

    Attributes:
        summary:              Human-readable summary of repeated failures.
        candidate_guardrails: Proposed (NOT enacted) guardrail candidates.
        evidence_pointers:    Evidence this summary is grounded in.
        confidence:           Model's declared confidence: low / medium / high.
        model_id:             Identifier of the model that produced this.
        fallback:             True when no model was configured.
        authoritative:        Always False. Raises ValueError if set True.
    """
    summary: str
    candidate_guardrails: List[str] = field(default_factory=list)
    evidence_pointers: List[str] = field(default_factory=list)
    confidence: str = FALLBACK_CONFIDENCE
    model_id: str = ""
    fallback: bool = False
    authoritative: bool = False  # always False — not configurable

    def __post_init__(self) -> None:
        if self.authoritative:
            raise ValueError(
                "RetroAnalysisSummary.authoritative must be False. "
                "Model output never carries governance authority."
            )
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"Invalid confidence level: {self.confidence!r}. "
                f"Valid: {sorted(CONFIDENCE_LEVELS)}"
            )
        if len(self.candidate_guardrails) > MAX_CANDIDATE_GUARDRAILS:
            raise ValueError(
                f"Too many candidate guardrails ({len(self.candidate_guardrails)}). "
                f"Max: {MAX_CANDIDATE_GUARDRAILS}"
            )

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "candidate_guardrails": self.candidate_guardrails,
            "evidence_pointers": self.evidence_pointers,
            "confidence": self.confidence,
            "model_id": self.model_id,
            "fallback": self.fallback,
            "authoritative": self.authoritative,
        }


# ---------------------------------------------------------------------------
# Hook protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class LocalModelHook(Protocol):
    """Protocol for optional local-model retrospective analysis.

    Implementors must produce a RetroAnalysisSummary with:
    - authoritative=False
    - evidence_pointers that are a subset of the input digest's evidence pool
    - a valid confidence level
    """

    def is_available(self) -> bool:
        """Return True if the local model is configured and reachable."""
        ...

    def analyze(self, input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
        """Run retrospective analysis and return a non-authoritative summary."""
        ...


# ---------------------------------------------------------------------------
# Fallback analysis (rule-based, no model)
# ---------------------------------------------------------------------------

def _fallback_summary(input_data: RetroAnalysisInput) -> RetroAnalysisSummary:
    """Produce a rule-based summary when no local model is configured.

    Summarizes recurring patterns from the digest without model assistance.
    confidence=low, fallback=True, candidate_guardrails=[].
    """
    patterns = getattr(input_data.digest, "recurring_patterns", [])
    evidence = input_data.evidence_pool()

    if not patterns:
        summary = "No recurring failure patterns detected in digest."
    else:
        lines = [f"Detected {len(patterns)} recurring failure pattern(s):"]
        for pat in patterns[:5]:  # cap at 5 for readability
            content = getattr(pat, "representative_content", "")
            count = getattr(pat, "count", 0)
            sev = getattr(pat, "severity", "info")
            lines.append(f"  [{sev}] x{count}: {content}")
        if len(patterns) > 5:
            lines.append(f"  ... and {len(patterns) - 5} more.")
        summary = "\n".join(lines)

    summary = summary[:input_data.max_summary_chars]
    return RetroAnalysisSummary(
        summary=summary,
        candidate_guardrails=[],
        evidence_pointers=evidence[:10],
        confidence=FALLBACK_CONFIDENCE,
        model_id="",
        fallback=True,
        authoritative=False,
    )


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def validate_summary(
    summary: RetroAnalysisSummary,
    input_data: RetroAnalysisInput,
) -> List[str]:
    """Validate that a model-produced summary satisfies the output contract.

    Returns a list of violation strings (empty = valid).

    Checks:
    - authoritative is False
    - confidence is a valid level
    - evidence_pointers are a subset of the input digest's evidence pool
    - candidate_guardrails count is within bound
    """
    violations: List[str] = []
    if summary.authoritative:
        violations.append("authoritative must be False")
    if summary.confidence not in CONFIDENCE_LEVELS:
        violations.append(f"invalid confidence: {summary.confidence!r}")
    if len(summary.candidate_guardrails) > MAX_CANDIDATE_GUARDRAILS:
        violations.append(
            f"too many candidate_guardrails: {len(summary.candidate_guardrails)}")
    pool = set(input_data.evidence_pool())
    stray = [p for p in summary.evidence_pointers if p and p not in pool]
    if stray:
        violations.append(f"evidence_pointers not in digest pool: {stray}")
    return violations


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_retrospective_hook(
    input_data: RetroAnalysisInput,
    hook: Optional[Any] = None,
) -> RetroAnalysisSummary:
    """Run optional local-model retrospective analysis.

    If hook is None or unavailable, returns a rule-based fallback summary.
    Otherwise delegates to hook.analyze() and validates the output contract.

    The caller retains full governance authority regardless of what the hook
    returns. Model output is advisory only.
    """
    if hook is None or not (
        hasattr(hook, "is_available") and hook.is_available()
    ):
        return _fallback_summary(input_data)

    summary = hook.analyze(input_data)
    violations = validate_summary(summary, input_data)
    if violations:
        raise ValueError(
            f"Hook returned invalid RetroAnalysisSummary: {'; '.join(violations)}"
        )
    return summary
