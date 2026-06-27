"""
Shared types, constants, and utilities for intelligence_sources modules.

Extracted from intelligence_selector.py (2511 LOC → per-source split).
All public symbols are re-exported via intelligence_selector.py for backward compat.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
MAX_PAYLOAD_CHARS = 2000
MIN_EVIDENCE_COUNT = 1
MAX_CODE_ANCHOR_CHARS = 1500

_DIRECT_INJECTION_CLASSES = frozenset({
    "code_anchor",
    "prior_round_finding",
    "adr_relevant",
    "operator_memory",
    "schema_section",
    "doc_relevant",
    "scout_sketch",
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
    "coding_sql",
    "coding_runtime",
    "coding_intelligence",
    "coding_test",
    "coding_ui",
    "research_structured",
    "docs_synthesis",
    "ops_watchdog",
    "channel_response",
})

# Maps subclass names to their component keywords for scope expansion in _scope_matches.
# When a query contains "coding_sql", items tagged "sql", "schema", or "migration" also match.
_SUBCLASS_TO_KEYWORDS: Dict[str, frozenset] = {
    "coding_sql": frozenset(["sql", "schema", "migration"]),
    "coding_runtime": frozenset(["runtime", "dispatch", "receipt"]),
    "coding_intelligence": frozenset(["intelligence", "pattern"]),
    "coding_test": frozenset(["test"]),
    "coding_ui": frozenset(["ui", "html", "css", "dashboard"]),
}

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


def _merge_stored_tags(category: str, raw_tags: Any) -> List[str]:
    """Build an item's scope_tags from its category + the persisted ``tags`` column.

    The stored tags (deterministic floor + optional LLM enrichment from vnx_tagger,
    a JSON array string) feed the rank-then-budget tag_overlap so a pattern with no
    derivable tags is still matchable. Category leads; stored tags are appended,
    deduped. Malformed/empty tags degrade to category-only.
    """
    out: List[str] = [category] if category else []
    if not raw_tags:
        return out
    parsed: Any = raw_tags
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
        except (json.JSONDecodeError, TypeError, ValueError):
            return out
    if isinstance(parsed, list):
        for t in parsed:
            ts = str(t).strip()
            if ts and ts not in out:
                out.append(ts)
    return out


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


def infer_task_subclass(
    skill_name: Optional[str],
    dispatch_paths: Optional[List[str]],
    instruction_text: Optional[str],
) -> str:
    """Infer a fine-grained task subclass from dispatch paths and instruction text.

    Priority order: sql > runtime > intelligence > test > ui > coding_interactive.
    Returns one of the VALID_TASK_CLASSES subclass names.
    """
    paths_lower = [p.lower() for p in (dispatch_paths or [])]
    instruction = (instruction_text or "").lower()

    if (
        any(p.endswith(".sql") or "migrat" in p or "/schemas/" in p or p.startswith("schemas/") for p in paths_lower)
        or re.search(r"\b(migration|schema|table)\b", instruction)
    ):
        return "coding_sql"

    if any(re.search(r"(?:^|/)(?:runtime_|dispatch_|receipt_)", p) for p in paths_lower):
        return "coding_runtime"

    if any(re.search(r"(?:^|/)intelligence_", p) for p in paths_lower):
        return "coding_intelligence"

    if any("/tests/" in p or p.startswith("tests/") for p in paths_lower):
        return "coding_test"

    if any(
        "/dashboard/" in p or p.startswith("dashboard/") or p.endswith(".html") or p.endswith(".tsx")
        for p in paths_lower
    ):
        return "coding_ui"

    return "coding_interactive"


def resolve_task_class(
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
    dispatch_paths: Optional[List[str]] = None,
    instruction_text: Optional[str] = None,
) -> str:
    """Resolve task class from explicit value, skill name, or inferred from paths/instruction."""
    if task_class and task_class in VALID_TASK_CLASSES:
        return task_class
    base_class = SKILL_TO_TASK_CLASS.get(skill_name or "", "coding_interactive") if skill_name else "coding_interactive"
    if base_class == "coding_interactive" and (dispatch_paths or instruction_text):
        return infer_task_subclass(skill_name, dispatch_paths, instruction_text)
    return base_class


def _expand_scope_tags(tags: List[str]) -> frozenset:
    """Expand subclass scope names to include component keywords for matching."""
    expanded: set = set(tags)
    for tag in tags:
        expanded.update(_SUBCLASS_TO_KEYWORDS.get(tag, frozenset()))
    return frozenset(expanded)


def _scope_matches(item_scope_tags: List[str], query_scope_tags: List[str]) -> bool:
    """Match item scope against query scope.

    Empty query scope always matches. In strict mode (VNX_INTEL_STRICT_SCOPE=1),
    an item with no scope tags does NOT match a non-empty query scope.
    Subclass names in the query are expanded to include component keywords.
    """
    if not query_scope_tags:
        return True
    strict = os.environ.get("VNX_INTEL_STRICT_SCOPE", "0") == "1"
    if not item_scope_tags:
        return not strict
    expanded_query = _expand_scope_tags(query_scope_tags)
    return bool(set(item_scope_tags) & expanded_query)


def _task_class_matches(item_filter: List[str], task_class: str) -> bool:
    """Empty filter = matches all task classes."""
    if not item_filter:
        return True
    return task_class in item_filter


_ALLOWED_TABLES = frozenset({
    "success_patterns",
    "antipatterns",
    "prevention_rules",
    "dispatch_metadata",
    "code_snippets",
    "intelligence_injections",
    "pattern_usage",
    "dispatch_pattern_offered",
})


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """PRAGMA-based column probe. Table name validated against static allowlist."""
    if table not in _ALLOWED_TABLES:
        raise ValueError(
            f"Table {table!r} not in allowed set: {sorted(_ALLOWED_TABLES)}"
        )
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
