"""
intelligence_sources — per-source intelligence query and injection modules.

Public re-exports for backward compatibility through intelligence_selector.py.
"""
from ._common import (
    CONFIDENCE_THRESHOLDS,
    EVIDENCE_THRESHOLDS,
    GOVERNANCE_CONFIDENCE_PENALTY,
    ITEM_CLASS_PRIORITY,
    MAX_CODE_ANCHOR_CHARS,
    MAX_CONTENT_CHARS_PER_ITEM,
    MAX_GOVERNANCE_PER_BATCH,
    MAX_ITEMS_PER_INJECTION,
    MAX_PAYLOAD_CHARS,
    MIN_EVIDENCE_COUNT,
    PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE,
    PATTERN_CATEGORY_CODE,
    PATTERN_CATEGORY_GOVERNANCE,
    PATTERN_CATEGORY_PROCESS,
    RECENT_COMPARABLE_DAYS,
    SKILL_TO_TASK_CLASS,
    SUCCESS_PATTERN_CONTENT_HASH_LEN,
    VALID_INJECTION_POINTS,
    VALID_TABLES,
    VALID_TASK_CLASSES,
    IntelligenceItem,
    SuppressionRecord,
    _content_hash,
    _item_hash,
    _new_id,
    _normalize_for_hash,
    _now_utc,
    _project_scope_clause,
    _scope_matches,
    _short_content_hash,
    _stable_item_id,
    _table_has_column,
    _task_class_matches,
    _apply_governance_penalty,
    apply_candidate_diversity,
    classify_pattern_category,
    resolve_task_class,
)
from ._models import InjectionResult, IntelligenceContext
from ._recording import record_injection_audit, record_pattern_usage, stamp_source_dispatch_ids
from .proven_pattern import query_proven_patterns
from .failure_prevention import query_failure_prevention
from .recent_comparable import query_recent_comparable
from .adr_relevant import build_adr_item, build_schema_section_item
from .code_anchor import build_code_anchor_item, build_operator_memory_item, build_prior_round_item

__all__ = [
    "CONFIDENCE_THRESHOLDS", "EVIDENCE_THRESHOLDS", "GOVERNANCE_CONFIDENCE_PENALTY",
    "ITEM_CLASS_PRIORITY", "MAX_CODE_ANCHOR_CHARS", "MAX_CONTENT_CHARS_PER_ITEM",
    "MAX_GOVERNANCE_PER_BATCH", "MAX_ITEMS_PER_INJECTION", "MAX_PAYLOAD_CHARS",
    "MIN_EVIDENCE_COUNT", "PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE", "PATTERN_CATEGORY_CODE",
    "PATTERN_CATEGORY_GOVERNANCE", "PATTERN_CATEGORY_PROCESS", "RECENT_COMPARABLE_DAYS",
    "SKILL_TO_TASK_CLASS", "SUCCESS_PATTERN_CONTENT_HASH_LEN", "VALID_INJECTION_POINTS",
    "VALID_TABLES", "VALID_TASK_CLASSES",
    "IntelligenceItem", "SuppressionRecord", "InjectionResult", "IntelligenceContext",
    "_content_hash", "_item_hash", "_new_id", "_normalize_for_hash",
    "_now_utc", "_project_scope_clause", "_scope_matches",
    "_short_content_hash", "_stable_item_id", "_table_has_column", "_task_class_matches",
    "_apply_governance_penalty", "apply_candidate_diversity", "classify_pattern_category",
    "resolve_task_class",
    "record_injection_audit", "record_pattern_usage", "stamp_source_dispatch_ids",
    "query_proven_patterns", "query_failure_prevention", "query_recent_comparable",
    "build_adr_item", "build_schema_section_item",
    "build_code_anchor_item", "build_operator_memory_item", "build_prior_round_item",
]
