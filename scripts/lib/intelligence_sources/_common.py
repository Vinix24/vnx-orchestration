"""
Shared types, constants, and utilities for intelligence_sources modules.

Extracted from intelligence_selector.py (2511 LOC → per-source split).
All public symbols are re-exported via intelligence_selector.py for backward compat.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from project_scope import current_project_id, project_filter_enabled
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from project_scope import current_project_id, project_filter_enabled

# ---------------------------------------------------------------------------
# FP-C Intelligence Contract constants
# ---------------------------------------------------------------------------

MAX_ITEMS_PER_INJECTION = 3
MAX_CONTENT_CHARS_PER_ITEM = 500
MAX_PAYLOAD_CHARS = 4000
MIN_EVIDENCE_COUNT = 1
MAX_CODE_ANCHOR_CHARS = 1500

_DIRECT_INJECTION_CLASSES = frozenset({
    "code_anchor",
    "prior_round_finding",
    "adr_relevant",
    "operator_memory",
    "schema_section",
})

CONFIDENCE_THRESHOLDS = {
    "proven_pattern": 0.6,
    "failure_prevention": 0.5,
    "recent_comparable": 0.4,
}

EVIDENCE_THRESHOLDS = {
    "proven_pattern": 2,
    "failure_prevention": 1,
    "recent_comparable": 1,
}

ITEM_CLASS_PRIORITY = ["proven_pattern", "failure_prevention", "recent_comparable"]

PATTERN_CATEGORY_CODE = "code"
PATTERN_CATEGORY_GOVERNANCE = "governance"
PATTERN_CATEGORY_PROCESS = "process"
PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE = "antipattern_evidence"

MAX_GOVERNANCE_PER_BATCH = 1
GOVERNANCE_CONFIDENCE_PENALTY = 0.7
RECENT_COMPARABLE_DAYS = 14

VALID_INJECTION_POINTS = frozenset({"dispatch_create", "dispatch_resume"})

VALID_TASK_CLASSES = frozenset({
    "coding_interactive",
    "research_structured",
    "docs_synthesis",
    "ops_watchdog",
    "channel_response",
})

SKILL_TO_TASK_CLASS = {
    "backend-developer": "coding_interactive",
    "frontend-developer": "coding_interactive",
    "api-developer": "coding_interactive",
    "python-optimizer": "coding_interactive",
    "supabase-expert": "coding_interactive",
    "monitoring-specialist": "coding_interactive",
    "vnx-manager": "coding_interactive",
    "debugger": "coding_interactive",
    "test-engineer": "coding_interactive",
    "quality-engineer": "coding_interactive",
    "architect": "research_structured",
    "reviewer": "research_structured",
    "planner": "research_structured",
    "data-analyst": "research_structured",
    "performance-profiler": "research_structured",
    "security-engineer": "research_structured",
    "t0-orchestrator": "research_structured",
    "excel-reporter": "docs_synthesis",
    "technical-writer": "docs_synthesis",
}

SUCCESS_PATTERN_CONTENT_HASH_LEN = 16

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IntelligenceItem:
    """A single intelligence item conforming to the FP-C schema."""
    item_id: str
    item_class: str
    title: str
    content: str
    confidence: float
    evidence_count: int
    last_seen: str
    scope_tags: List[str]
    source_refs: List[str] = field(default_factory=list)
    task_class_filter: List[str] = field(default_factory=list)
    pattern_category: str = PATTERN_CATEGORY_CODE
    content_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        content_cap = (
            MAX_CODE_ANCHOR_CHARS
            if self.item_class in _DIRECT_INJECTION_CLASSES
            else MAX_CONTENT_CHARS_PER_ITEM
        )
        return {
            "item_id": self.item_id,
            "item_class": self.item_class,
            "title": self.title,
            "content": self.content[:content_cap],
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "last_seen": self.last_seen,
            "scope_tags": self.scope_tags,
            "source_refs": self.source_refs,
            "task_class_filter": self.task_class_filter,
            "pattern_category": self.pattern_category,
            "content_hash": self.content_hash,
        }


@dataclass
class SuppressionRecord:
    """Records why an item class slot was not filled."""
    item_class: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"item_class": self.item_class, "reason": self.reason}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _new_id() -> str:
    return str(uuid.uuid4())


def _normalize_for_hash(text: str) -> str:
    return " ".join((text or "").lower().split())


def _content_hash(*parts: str) -> str:
    joined = "\n".join(_normalize_for_hash(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _short_content_hash(*parts: str) -> str:
    return _content_hash(*parts)[:SUCCESS_PATTERN_CONTENT_HASH_LEN]


def _item_hash(item_id: str) -> str:
    return hashlib.sha1(item_id.encode("utf-8")).hexdigest()


def _stable_item_id(prefix: str, source_key: str) -> str:
    """Build a deterministic, content-derived item_id.

    The id encodes the originating table via prefix (e.g. sp, ap, pr, dm)
    and a stable per-row key so pattern_usage rows aggregate instead of
    fragmenting across dispatches.
    """
    safe_key = str(source_key).strip().lower().replace(" ", "_")
    return f"intel_{prefix}_{safe_key}"


def classify_pattern_category(title: str, description: str) -> str:
    """Mirror of pattern_dedup.classify_pattern, kept local to avoid import cycle."""
    haystack = f"{_normalize_for_hash(title)} :: {_normalize_for_hash(description)}"
    if "gate " in haystack and "passed" in haystack:
        return PATTERN_CATEGORY_GOVERNANCE
    if any(token in haystack for token in (
        "receipt processor",
        "dispatch lifecycle",
        "lease release",
    )):
        return PATTERN_CATEGORY_PROCESS
    return PATTERN_CATEGORY_CODE


def resolve_task_class(
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> str:
    """Resolve task class from explicit value or skill name. Defaults to coding_interactive."""
    if task_class and task_class in VALID_TASK_CLASSES:
        return task_class
    if skill_name:
        return SKILL_TO_TASK_CLASS.get(skill_name, "coding_interactive")
    return "coding_interactive"


def _scope_matches(item_scope_tags: List[str], query_scope_tags: List[str]) -> bool:
    """Empty item scope = matches everything. Empty query scope = matches everything."""
    if not item_scope_tags or not query_scope_tags:
        return True
    return bool(set(item_scope_tags) & set(query_scope_tags))


def _task_class_matches(item_filter: List[str], task_class: str) -> bool:
    """Empty filter = matches all task classes."""
    if not item_filter:
        return True
    return task_class in item_filter


VALID_TABLES = frozenset({
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "dispatch_metadata",
    "intelligence_injections",
    "pattern_usage",
    "dispatch_pattern_offered",
})


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """PRAGMA-based column probe. table must be in VALID_TABLES."""
    if table not in VALID_TABLES:
        raise ValueError(f"Unknown table: {table!r}; allowed: {sorted(VALID_TABLES)}")
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        if name == column:
            return True
    return False


def _project_scope_clause(column_present: bool) -> Tuple[str, tuple]:
    """Return the AND project_id = ? fragment + bind params, or empty."""
    if not column_present or not project_filter_enabled():
        return "", ()
    return "AND project_id = ?", (current_project_id(),)



def apply_candidate_diversity(
    candidates: Dict[str, List["IntelligenceItem"]],
    task_class: str,
) -> Dict[str, List["IntelligenceItem"]]:
    """Collapse same-hash duplicates and re-rank governance vs. code patterns."""
    adjusted: Dict[str, List["IntelligenceItem"]] = {}
    for cls, items in candidates.items():
        collapsed: Dict[str, "IntelligenceItem"] = {}
        unhashed: List["IntelligenceItem"] = []
        for item in items:
            if not item.content_hash:
                unhashed.append(item)
                continue
            existing = collapsed.get(item.content_hash)
            if existing is None or item.confidence > existing.confidence:
                collapsed[item.content_hash] = item
        merged = list(collapsed.values()) + unhashed
        if cls == "proven_pattern":
            merged = [_apply_governance_penalty(item, task_class) for item in merged]
        merged.sort(key=lambda i: i.confidence, reverse=True)
        adjusted[cls] = merged
    return adjusted


def _apply_governance_penalty(item: "IntelligenceItem", task_class: str) -> "IntelligenceItem":
    """Down-weight governance proven_patterns for code-context dispatches."""
    if item.pattern_category != PATTERN_CATEGORY_GOVERNANCE:
        return item
    if task_class != "coding_interactive":
        return item
    return IntelligenceItem(
        item_id=item.item_id,
        item_class=item.item_class,
        title=item.title,
        content=item.content,
        confidence=item.confidence * GOVERNANCE_CONFIDENCE_PENALTY,
        evidence_count=item.evidence_count,
        last_seen=item.last_seen,
        scope_tags=item.scope_tags,
        source_refs=item.source_refs,
        task_class_filter=item.task_class_filter,
        pattern_category=item.pattern_category,
        content_hash=item.content_hash,
    )
