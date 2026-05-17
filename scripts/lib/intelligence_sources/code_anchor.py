"""
code_anchor source — code-snippet grounding + operator memory + prior-round findings.

Three direct-injection item builders that provide current-state grounding:
  - build_prior_round_item: prior review gate findings for the current PR
  - build_code_anchor_item: file:line snippets matching dispatch keywords
  - build_operator_memory_item: curated operator lessons relevant to this dispatch
"""
from __future__ import annotations

from typing import List, Optional

from ._common import (
    MAX_CODE_ANCHOR_CHARS,
    PATTERN_CATEGORY_CODE,
    IntelligenceItem,
)

try:
    import prior_round_injector as _prior_round_injector
except ImportError:
    _prior_round_injector = None  # type: ignore[assignment]

try:
    import code_anchor_finder as _code_anchor_finder
except ImportError:
    _code_anchor_finder = None  # type: ignore[assignment]

try:
    import operator_memory_indexer as _operator_memory_indexer
except ImportError:
    _operator_memory_indexer = None  # type: ignore[assignment]


def build_prior_round_item(
    pr_id: str,
    dispatch_paths: List[str],
    now_ts: str,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
) -> Optional[IntelligenceItem]:
    """Return a prior_round_finding IntelligenceItem, or None when no findings exist.

    Fetches prior review-gate findings for the given PR. Confidence is always
    1.0 since these are direct evidence from a completed gate run.
    """
    if not pr_id or _prior_round_injector is None:
        return None
    prior_findings = _prior_round_injector.fetch_prior_findings(
        pr_id,
        dispatch_paths=dispatch_paths,
        max_chars=max_chars,
    )
    if not prior_findings:
        return None
    return IntelligenceItem(
        item_id=f"intel_prf_{pr_id}",
        item_class="prior_round_finding",
        title=f"Prior-round review findings on PR #{pr_id}",
        content=_prior_round_injector.format_findings_section(prior_findings),
        confidence=1.0,
        evidence_count=len(prior_findings),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[f"pr-{pr_id}-{f.gate}" for f in prior_findings],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )


def build_code_anchor_item(
    dispatch_id: str,
    dispatch_paths: List[str],
    instruction_text: str,
    now_ts: str,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
) -> Optional[IntelligenceItem]:
    """Return a code_anchor IntelligenceItem, or None when no anchors match.

    Extracts file:line snippets from dispatch_paths matching keywords in
    instruction_text. Provides current-state grounding so workers don't
    need to grep before acting.
    """
    if not dispatch_paths or not instruction_text or _code_anchor_finder is None:
        return None
    anchors = _code_anchor_finder.fetch_code_anchors(
        dispatch_paths,
        instruction_text,
        max_chars=max_chars,
    )
    if not anchors:
        return None
    return IntelligenceItem(
        item_id=f"intel_ca_{dispatch_id}",
        item_class="code_anchor",
        title=f"Code anchors for {len(dispatch_paths)} touched files",
        content=_code_anchor_finder.format_code_anchors_section(anchors),
        confidence=1.0,
        evidence_count=len(anchors),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[
            f"{a.file_path}:{a.line_start}-{a.line_end}" for a in anchors
        ],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )


def build_operator_memory_item(
    dispatch_id: str,
    skill_name: Optional[str],
    dispatch_paths: List[str],
    instruction_text: str,
    now_ts: str,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
) -> Optional[IntelligenceItem]:
    """Return an operator_memory IntelligenceItem, or None when no memories match.

    Fetches curated operator lessons relevant to the current skill, paths,
    and instruction text. Provides accumulated operator feedback so workers
    inherit hard-won lessons without discovering them the hard way.
    """
    if not (skill_name or dispatch_paths or instruction_text):
        return None
    if _operator_memory_indexer is None:
        return None
    memories = _operator_memory_indexer.fetch_relevant_memories(
        skill_name,
        dispatch_paths,
        instruction_text,
        max_chars=max_chars,
    )
    if not memories:
        return None
    return IntelligenceItem(
        item_id=f"intel_om_{dispatch_id}",
        item_class="operator_memory",
        title=f"Relevant operator memories ({len(memories)})",
        content=_operator_memory_indexer.format_memories_section(memories),
        confidence=1.0,
        evidence_count=len(memories),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[m.name for m in memories],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )
