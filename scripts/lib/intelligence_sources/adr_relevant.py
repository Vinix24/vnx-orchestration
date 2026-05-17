"""
adr_relevant source — auto-match ADRs to touched files + schema section grounding.

Two direct-injection item builders:
  - build_adr_item: fetches relevant ADR sections via adr_indexer
  - build_schema_section_item: fetches relevant CREATE TABLE blocks via schema_section_indexer

Both produce IntelligenceItem with class 'adr_relevant' / 'schema_section', confidence 1.0.
"""
from __future__ import annotations

from typing import List, Optional

from ._common import (
    MAX_CODE_ANCHOR_CHARS,
    PATTERN_CATEGORY_CODE,
    IntelligenceItem,
    _now_utc,
)

try:
    import adr_indexer as _adr_indexer
except ImportError:
    _adr_indexer = None  # type: ignore[assignment]

try:
    import schema_section_indexer as _schema_section_indexer
except ImportError:
    _schema_section_indexer = None  # type: ignore[assignment]


def build_adr_item(
    dispatch_id: str,
    dispatch_paths: List[str],
    now_ts: str,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
) -> Optional[IntelligenceItem]:
    """Return an adr_relevant IntelligenceItem, or None when no ADRs match.

    Delegates to adr_indexer.fetch_relevant_adrs. Returns None when the
    indexer is unavailable or no ADRs are relevant to the dispatch paths.
    """
    if not dispatch_paths or _adr_indexer is None:
        return None
    relevant_adrs = _adr_indexer.fetch_relevant_adrs(dispatch_paths, max_chars=max_chars)
    if not relevant_adrs:
        return None
    return IntelligenceItem(
        item_id=f"intel_adr_{'_'.join(a.adr_id for a in relevant_adrs[:3])}",
        item_class="adr_relevant",
        title=f"Relevant ADRs for {len(dispatch_paths)} touched files",
        content=_adr_indexer.format_adrs_section(relevant_adrs),
        confidence=1.0,
        evidence_count=len(relevant_adrs),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[a.adr_id for a in relevant_adrs],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )


def build_schema_section_item(
    dispatch_id: str,
    dispatch_paths: List[str],
    instruction_text: str,
    now_ts: str,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
) -> Optional[IntelligenceItem]:
    """Return a schema_section IntelligenceItem, or None when no schemas match.

    Delegates to schema_section_indexer.fetch_relevant_schema_sections.
    Pre-grounds the worker on actual CREATE TABLE / ALTER TABLE statements
    so they don't need to grep schemas/ before acting.
    """
    if (not dispatch_paths and not instruction_text) or _schema_section_indexer is None:
        return None
    schema_sections = _schema_section_indexer.fetch_relevant_schema_sections(
        dispatch_paths or [],
        instruction_text or "",
        max_chars=max_chars,
    )
    if not schema_sections:
        return None
    unique_tables = len({s.table_name for s in schema_sections})
    return IntelligenceItem(
        item_id=f"intel_ss_{dispatch_id}",
        item_class="schema_section",
        title=f"Schema sections for {unique_tables} touched table(s)",
        content=_schema_section_indexer.format_schema_sections(schema_sections),
        confidence=1.0,
        evidence_count=len(schema_sections),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[f"{s.file_path}:{s.table_name}" for s in schema_sections],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )
