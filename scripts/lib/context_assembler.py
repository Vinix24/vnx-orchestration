#!/usr/bin/env python3
"""Bounded context assembly for autonomous dispatch prompts.

Implements the Context Injection Contract (docs/CONTEXT_INJECTION_CONTRACT.md):
  - 7-priority component model (P0-P7)
  - Budget enforcement: P3-P7 overhead < 20% target, 25% hard limit
  - Stale-context rejection with per-component max age
  - Reverse-priority trimming when budget exceeded
  - Freshness metadata for post-hoc auditing

Context components:
  P0  Dispatch Identity    (mandatory)
  P1  Task Specification   (mandatory)
  P2  Mandatory Code Ctx   (mandatory)
  P3  Chain Position       (mandatory-when-chained)
  P4  Intelligence Payload (optional-bounded, max 2000 chars)
  P5  Prior PR Evidence    (optional, max 1000 tokens)
  P6  Open Items Digest    (optional, max 500 tokens)
  P7  Reusable Signals     (optional, max 500 tokens)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from result_contract import Result, result_error, result_ok


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUDGET_TARGET_RATIO = 0.20
BUDGET_HARD_LIMIT_RATIO = 0.25

INTELLIGENCE_CHAR_LIMIT = 2000
CHAIN_POSITION_TOKEN_LIMIT = 750
PRIOR_PR_TOKEN_LIMIT = 1000
OPEN_ITEMS_TOKEN_LIMIT = 500
REUSABLE_SIGNALS_TOKEN_LIMIT = 500

DISPATCH_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-.+$")
PR_ID_PATTERN = re.compile(r"^PR-\d+$")
VALID_TRACKS = frozenset({"A", "B", "C"})
VALID_STATUSES = frozenset({"success", "failed", "partial"})
VALID_ACTIONS = frozenset({"advance", "review", "fix", "block", "escalate"})
VALID_SEVERITIES = frozenset({"blocker", "warn", "info"})
VALID_CHANGE_TYPES = frozenset({"created", "modified", "deleted"})

# Intelligence item class priority (highest first); overflow drops lowest first
INTELLIGENCE_CLASS_PRIORITY = ("proven_pattern", "failure_prevention", "recent_comparable")

# Max age in seconds per component (0 = must re-derive at assembly)
STALENESS_MAX_AGE: Dict[str, int] = {
    "chain_position": 0,
    "carry_forward_summary": 0,
    "prior_pr_evidence": 0,
    "open_items_digest": 3600,
    "intelligence_payload": 86400,
    "reusable_signals": 1209600,
}

# Overhead components in trim order (last trimmed first during assembly,
# but when budget exceeded we trim P7 first = reverse priority)
OVERHEAD_PRIORITIES = ("chain_position", "intelligence_payload",
                       "prior_pr_evidence", "open_items_digest",
                       "reusable_signals")

# Trim order: P7 -> P6 -> P5 -> P4 (P3 is mandatory-when-chained, not trimmed)
TRIM_ORDER = ("reusable_signals", "open_items_digest",
              "prior_pr_evidence", "intelligence_payload")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (GPT-family heuristic)."""
    return max(1, len(text) // 4) if text else 0


# ---------------------------------------------------------------------------
# Freshness checking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FreshnessRecord:
    source_updated_at: str
    is_fresh: bool


def check_freshness(
    component_name: str,
    source_updated_at: datetime,
    assembly_time: datetime,
) -> FreshnessRecord:
    """Check whether a component is fresh enough per staleness rules."""
    max_age = STALENESS_MAX_AGE.get(component_name)
    if max_age is None:
        return FreshnessRecord(
            source_updated_at=source_updated_at.isoformat(),
            is_fresh=True,
        )
    age_seconds = (assembly_time - source_updated_at).total_seconds()
    is_fresh = age_seconds <= max_age if max_age > 0 else age_seconds <= 0
    return FreshnessRecord(
        source_updated_at=source_updated_at.isoformat(),
        is_fresh=is_fresh,
    )


# ---------------------------------------------------------------------------
# Context components
# ---------------------------------------------------------------------------

@dataclass
class ContextComponent:
    """A single context component with priority and content."""
    priority: int
    name: str
    content: str
    token_estimate: int = 0
    freshness: Optional[FreshnessRecord] = None

    def __post_init__(self) -> None:
        if self.token_estimate == 0 and self.content:
            self.token_estimate = estimate_tokens(self.content)


@dataclass
class BundleFreshness:
    assembled_at: str
    main_sha_at_assembly: str
    component_freshness: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class ContextBundle:
    """Assembled context bundle with budget metadata."""
    components: List[ContextComponent]
    total_tokens: int
    overhead_tokens: int
    overhead_ratio: float
    budget_status: str  # "within_target" | "over_target" | "over_hard_limit"
    trimmed_components: List[str]
    freshness: BundleFreshness
    stale_rejections: List[str]

    def render(self) -> str:
        """Render the bundle as a single prompt string."""
        return "\n\n".join(c.content for c in self.components if c.content)


# ---------------------------------------------------------------------------
# Context Assembler
# ---------------------------------------------------------------------------

class ContextAssembler:
    """Assembles bounded context bundles per the Context Injection Contract."""

    def __init__(self, *, main_sha: str = "", assembly_time: Optional[datetime] = None) -> None:
        self._main_sha = main_sha
        self._assembly_time = assembly_time or datetime.now(timezone.utc)
        self._components: List[ContextComponent] = []
        self._stale_rejections: List[str] = []
        self._freshness_records: Dict[str, Dict[str, Any]] = {}

    def _set_component(self, component: ContextComponent) -> None:
        """Replace existing component of same name, or append if new."""
        for i, c in enumerate(self._components):
            if c.name == component.name:
                self._components[i] = component
                return
        self._components.append(component)

    def add_dispatch_identity(
        self, dispatch_id: str, pr_id: str, track: str,
        gate: str, feature_name: str,
    ) -> Result:
        """Add P0: Dispatch Identity (mandatory)."""
        if not DISPATCH_ID_PATTERN.match(dispatch_id):
            return result_error("invalid_argument", f"Invalid dispatch_id format: {dispatch_id}")
        if not PR_ID_PATTERN.match(pr_id):
            return result_error("invalid_argument", f"Invalid pr_id format: {pr_id}")
        if track not in VALID_TRACKS:
            return result_error("invalid_argument", f"Invalid track: {track}")
        if not gate:
            return result_error("invalid_argument", "Gate must be non-empty")
        if not feature_name:
            return result_error("invalid_argument", "Feature name must be non-empty")

        content = (
            f"Dispatch: {dispatch_id}\n"
            f"PR: {pr_id} | Track: {track} | Gate: {gate}\n"
            f"Feature: {feature_name}"
        )
        self._set_component(ContextComponent(priority=0, name="dispatch_identity", content=content))
        return result_ok()

    def add_task_specification(
        self, skill_command: str, task_description: str,
        deliverables: List[str], success_criteria: List[str],
        quality_gate_checklist: List[str],
    ) -> Result:
        """Add P1: Task Specification (mandatory)."""
        if not skill_command:
            return result_error("invalid_argument", "Skill command must be non-empty")
        if not task_description:
            return result_error("invalid_argument", "Task description must be non-empty")
        if not deliverables:
            return result_error("invalid_argument", "At least one deliverable required")
        if not success_criteria:
            return result_error("invalid_argument", "At least one success criterion required")

        parts = [
            f"Skill: {skill_command}",
            f"\n{task_description}",
            "\nDeliverables:\n" + "\n".join(f"- {d}" for d in deliverables),
            "\nSuccess Criteria:\n" + "\n".join(f"- {c}" for c in success_criteria),
        ]
        if quality_gate_checklist:
            parts.append("\nQuality Gate:\n" + "\n".join(f"- [ ] {g}" for g in quality_gate_checklist))

        self._set_component(ContextComponent(priority=1, name="task_specification", content="\n".join(parts)))
        return result_ok()

    def add_code_context(self, file_contents: Dict[str, str]) -> Result:
        """Add P2: Mandatory Code Context (mandatory when dispatch references files)."""
        if not file_contents:
            return result_ok()
        parts = []
        for path, content in file_contents.items():
            parts.append(f"--- {path} ---\n{content}")
        self._set_component(ContextComponent(priority=2, name="code_context", content="\n\n".join(parts)))
        return result_ok()

    def add_chain_position(
        self, current_feature_index: int, total_features: int,
        carry_forward_summary: Dict[str, int],
        blocking_items: List[Dict[str, Any]],
        dependency_status: str,
        *,
        source_updated_at: Optional[datetime] = None,
    ) -> Result:
        """Add P3: Chain Position (mandatory-when-chained)."""
        updated_at = source_updated_at or self._assembly_time
        freshness = check_freshness("chain_position", updated_at, self._assembly_time)
        if not freshness.is_fresh:
            self._stale_rejections.append("chain_position")
            return result_error("stale_context", "Chain position is stale (max age 0, must re-derive)")

        self._freshness_records["chain_position"] = {
            "source_updated_at": freshness.source_updated_at,
            "is_fresh": freshness.is_fresh,
        }

        summary = carry_forward_summary
        parts = [
            f"Chain Position: Feature {current_feature_index + 1} of {total_features}",
            f"Carry-Forward: {summary.get('blocker_count', 0)} blockers, "
            f"{summary.get('warn_count', 0)} warnings, "
            f"{summary.get('deferred_count', 0)} deferred, "
            f"{summary.get('residual_risk_count', 0)} residual risks",
            f"Dependencies: {dependency_status}",
        ]
        if blocking_items:
            parts.append("Blocking Items:")
            for item in blocking_items:
                parts.append(f"  - [{item.get('severity', '?')}] {item.get('title', 'untitled')}")

        content = "\n".join(parts)
        if estimate_tokens(content) > CHAIN_POSITION_TOKEN_LIMIT:
            return result_error(
                "component_too_large",
                f"Chain position ({estimate_tokens(content)} tokens) exceeds "
                f"hard limit ({CHAIN_POSITION_TOKEN_LIMIT} tokens)",
            )
        self._set_component(ContextComponent(
            priority=3, name="chain_position", content=content, freshness=freshness,
        ))
        return result_ok()

    def add_intelligence_payload(
        self, items: List[Dict[str, str]],
        *,
        source_updated_at: Optional[datetime] = None,
    ) -> Result:
        """Add P4: Intelligence Payload (optional-bounded, max 3 items, 2000 chars)."""
        updated_at = source_updated_at or self._assembly_time
        freshness = check_freshness("intelligence_payload", updated_at, self._assembly_time)
        if not freshness.is_fresh:
            self._stale_rejections.append("intelligence_payload")
            return result_error("stale_context", "Intelligence payload is stale (>24h)")

        self._freshness_records["intelligence_payload"] = {
            "source_updated_at": freshness.source_updated_at,
            "is_fresh": freshness.is_fresh,
        }

        bounded_items = items[:3]
        # Sort by FPC class priority (highest first) for overflow dropping
        class_rank = {cls: i for i, cls in enumerate(INTELLIGENCE_CLASS_PRIORITY)}
        bounded_items.sort(key=lambda it: class_rank.get(it.get("type", "proven_pattern"), 0))

        # Drop lowest-priority items first when payload exceeds char limit
        while bounded_items:
            content = "\n".join(
                f"[{it.get('type', 'pattern')}] {it.get('content', '')}"
                for it in bounded_items
            )
            if len(content) <= INTELLIGENCE_CHAR_LIMIT:
                break
            bounded_items.pop()  # drop lowest-priority (last) item

        if not bounded_items:
            content = ""

        self._set_component(ContextComponent(
            priority=4, name="intelligence_payload", content=content, freshness=freshness,
        ))
        return result_ok()

    def add_prior_pr_evidence(
        self, findings: List[Dict[str, str]],
        *,
        source_updated_at: Optional[datetime] = None,
    ) -> Result:
        """Add P5: Prior PR Evidence (optional, immediate predecessor only)."""
        updated_at = source_updated_at or self._assembly_time
        freshness = check_freshness("prior_pr_evidence", updated_at, self._assembly_time)
        if not freshness.is_fresh:
            self._stale_rejections.append("prior_pr_evidence")
            return result_error("stale_context", "Prior PR evidence is stale (max age 0)")

        self._freshness_records["prior_pr_evidence"] = {
            "source_updated_at": freshness.source_updated_at,
            "is_fresh": freshness.is_fresh,
        }

        parts = ["Prior PR Findings:"]
        for f in findings:
            parts.append(f"  - [{f.get('severity', 'info')}] {f.get('description', '')}")
        content = "\n".join(parts)

        if estimate_tokens(content) > PRIOR_PR_TOKEN_LIMIT:
            return result_error(
                "component_too_large",
                f"Prior PR evidence ({estimate_tokens(content)} tokens) exceeds "
                f"hard limit ({PRIOR_PR_TOKEN_LIMIT} tokens)",
            )
        self._set_component(ContextComponent(
            priority=5, name="prior_pr_evidence", content=content, freshness=freshness,
        ))
        return result_ok()

    def add_open_items_digest(
        self, items: List[Dict[str, Any]],
        *,
        source_updated_at: Optional[datetime] = None,
    ) -> Result:
        """Add P6: Open Items Digest (optional, severity >= warn)."""
        updated_at = source_updated_at or self._assembly_time
        freshness = check_freshness("open_items_digest", updated_at, self._assembly_time)
        if not freshness.is_fresh:
            self._stale_rejections.append("open_items_digest")
            return result_error("stale_context", "Open items digest is stale (>1h)")

        self._freshness_records["open_items_digest"] = {
            "source_updated_at": freshness.source_updated_at,
            "is_fresh": freshness.is_fresh,
        }

        filtered = [i for i in items if i.get("severity") in ("blocker", "warn")]
        if not filtered:
            return result_ok()

        parts = ["Open Items:"]
        for item in filtered:
            parts.append(f"  - [{item.get('severity')}] {item.get('title', '')} ({item.get('status', 'open')})")
        content = "\n".join(parts)

        if estimate_tokens(content) > OPEN_ITEMS_TOKEN_LIMIT:
            return result_error(
                "component_too_large",
                f"Open items digest ({estimate_tokens(content)} tokens) exceeds "
                f"hard limit ({OPEN_ITEMS_TOKEN_LIMIT} tokens)",
            )
        self._set_component(ContextComponent(
            priority=6, name="open_items_digest", content=content, freshness=freshness,
        ))
        return result_ok()

    def add_reusable_signals(
        self, signals: List[Dict[str, str]],
        *,
        source_updated_at: Optional[datetime] = None,
    ) -> Result:
        """Add P7: Reusable Signals (optional, 14-day recency window)."""
        updated_at = source_updated_at or self._assembly_time
        freshness = check_freshness("reusable_signals", updated_at, self._assembly_time)
        if not freshness.is_fresh:
            self._stale_rejections.append("reusable_signals")
            return result_error("stale_context", "Reusable signals are stale (>14d)")

        self._freshness_records["reusable_signals"] = {
            "source_updated_at": freshness.source_updated_at,
            "is_fresh": freshness.is_fresh,
        }

        parts = ["Reusable Signals:"]
        for sig in signals:
            parts.append(f"  - [{sig.get('type', 'outcome')}] {sig.get('content', '')}")
        content = "\n".join(parts)

        if estimate_tokens(content) > REUSABLE_SIGNALS_TOKEN_LIMIT:
            return result_error(
                "component_too_large",
                f"Reusable signals ({estimate_tokens(content)} tokens) exceeds "
                f"hard limit ({REUSABLE_SIGNALS_TOKEN_LIMIT} tokens)",
            )
        self._set_component(ContextComponent(
            priority=7, name="reusable_signals", content=content, freshness=freshness,
        ))
        return result_ok()

    def assemble(self) -> Result:
        """Assemble the context bundle with budget enforcement.

        Returns Result with ContextBundle on success, or error if:
          - Mandatory components missing (P0, P1)
          - Budget hard limit exceeded after trimming
        """
        component_names = {c.name for c in self._components}
        if "dispatch_identity" not in component_names:
            return result_error("missing_argument", "P0 (dispatch_identity) is mandatory")
        if "task_specification" not in component_names:
            return result_error("missing_argument", "P1 (task_specification) is mandatory")

        sorted_components = sorted(self._components, key=lambda c: c.priority)
        trimmed: List[str] = []

        overhead_tokens, total_tokens = self._compute_budget(sorted_components)
        overhead_ratio = overhead_tokens / total_tokens if total_tokens > 0 else 0.0

        if overhead_ratio > BUDGET_HARD_LIMIT_RATIO:
            sorted_components, trimmed = self._trim_to_budget(sorted_components)
            overhead_tokens, total_tokens = self._compute_budget(sorted_components)
            overhead_ratio = overhead_tokens / total_tokens if total_tokens > 0 else 0.0

        if overhead_ratio > BUDGET_HARD_LIMIT_RATIO:
            return result_error(
                "budget_exceeded",
                f"Context overhead {overhead_ratio:.1%} exceeds hard limit "
                f"{BUDGET_HARD_LIMIT_RATIO:.0%} even after trimming {trimmed}",
            )

        if overhead_ratio > BUDGET_TARGET_RATIO:
            budget_status = "over_target"
        else:
            budget_status = "within_target"

        bundle = ContextBundle(
            components=sorted_components,
            total_tokens=total_tokens,
            overhead_tokens=overhead_tokens,
            overhead_ratio=overhead_ratio,
            budget_status=budget_status,
            trimmed_components=trimmed,
            freshness=BundleFreshness(
                assembled_at=self._assembly_time.isoformat(),
                main_sha_at_assembly=self._main_sha,
                component_freshness=self._freshness_records,
            ),
            stale_rejections=self._stale_rejections,
        )
        return result_ok(bundle)

    def _compute_budget(self, components: List[ContextComponent]) -> tuple[int, int]:
        """Return (overhead_tokens, total_tokens) for the given component list."""
        total = sum(c.token_estimate for c in components)
        overhead = sum(c.token_estimate for c in components if c.priority >= 3)
        return overhead, total

    def _trim_to_budget(
        self, components: List[ContextComponent],
    ) -> tuple[List[ContextComponent], List[str]]:
        """Trim optional components in reverse priority order (P7 first)."""
        trimmed: List[str] = []
        remaining = list(components)

        for trim_name in TRIM_ORDER:
            overhead, total = self._compute_budget(remaining)
            ratio = overhead / total if total > 0 else 0.0
            if ratio <= BUDGET_HARD_LIMIT_RATIO:
                break
            before_len = len(remaining)
            remaining = [c for c in remaining if c.name != trim_name]
            if len(remaining) < before_len:
                trimmed.append(trim_name)

        return remaining, trimmed
