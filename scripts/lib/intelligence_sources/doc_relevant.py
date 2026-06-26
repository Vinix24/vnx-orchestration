"""doc_relevant source — inject POINTERS to relevant documentation sections.

Queries the markdown sections that ``doc_section_extractor`` indexes into the
``code_snippets`` FTS5 table (language='markdown'), matched to the dispatch's
instruction terms + touched-file stems, and injects compact ``file:line — heading``
pointers — never doc bodies. The worker (or a scout) opens the referenced range.
This reuses the code-anchor pointer pattern so docs survive the payload budget.

The selector passes its quality_intelligence.db connection (unlike the file-based
adr/code-anchor indexers, the doc index lives in the DB).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import List, Optional

from ._common import (
    PATTERN_CATEGORY_CODE,
    IntelligenceItem,
)

try:
    import code_anchor_finder as _code_anchor_finder
except ImportError:
    _code_anchor_finder = None  # type: ignore[assignment]

MAX_DOC_SECTIONS = 5
_MAX_QUERY_TOKENS = 12
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def _extract_terms(instruction_text: str) -> List[str]:
    """Reuse the code-anchor term extractor when available; fall back to a simple
    identifier scan."""
    if _code_anchor_finder is not None:
        return _code_anchor_finder.extract_terms(instruction_text or "")
    seen: set = set()
    out: List[str] = []
    for m in _IDENT_RE.finditer(instruction_text or ""):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _fts5_match_expr(tokens: List[str]) -> str:
    """Build an OR-of-quoted-terms FTS5 MATCH expression (capped)."""
    safe = []
    for t in tokens[:_MAX_QUERY_TOKENS]:
        cleaned = t.replace('"', "").strip()
        if cleaned:
            safe.append(f'"{cleaned}"')
    return " OR ".join(safe)


def fetch_relevant_doc_sections(
    conn: "Optional[sqlite3.Connection]",
    dispatch_paths: Optional[List[str]],
    instruction_text: str,
    limit: int = MAX_DOC_SECTIONS,
) -> List[tuple]:
    """Return up to ``limit`` (title, file_path, line_range) doc-section pointers
    from the markdown FTS5 index, ranked by FTS5 relevance. Empty on any error or
    when nothing matches."""
    if conn is None:
        return []
    terms = _extract_terms(instruction_text)
    path_stems = [Path(p).stem for p in (dispatch_paths or []) if p]
    tokens = [t for t in (terms + path_stems) if t]
    if not tokens:
        return []
    match_expr = _fts5_match_expr(tokens)
    if not match_expr:
        return []
    try:
        rows = conn.execute(
            "SELECT title, file_path, line_range FROM code_snippets "
            "WHERE language = 'markdown' AND code_snippets MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match_expr, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [tuple(r) for r in rows]


def format_doc_refs(sections: List[tuple]) -> str:
    """Compact pointer format: ``file:line — heading``. No doc bodies."""
    if not sections:
        return ""
    lines = [
        "## RELEVANT DOCS (pointers — open the section before deciding)",
        "",
        "> Pointers to the live docs, not copies. Open each range; re-read the "
        "file if it looks stale.",
        "",
    ]
    for title, file_path, line_range in sections:
        lines.append(f"- `{file_path}:{line_range}` — {title}")
    return "\n".join(lines)


def build_doc_relevant_item(
    conn: "Optional[sqlite3.Connection]",
    dispatch_id: str,
    dispatch_paths: Optional[List[str]],
    instruction_text: str,
    now_ts: str,
) -> Optional[IntelligenceItem]:
    """Return a doc_relevant IntelligenceItem, or None when nothing matches.

    Queries the markdown FTS5 index for sections relevant to the dispatch and
    injects them as compact file:line pointers.
    """
    if conn is None or not (dispatch_paths or instruction_text):
        return None
    sections = fetch_relevant_doc_sections(conn, dispatch_paths, instruction_text or "")
    if not sections:
        return None
    return IntelligenceItem(
        item_id=f"intel_doc_{dispatch_id}",
        item_class="doc_relevant",
        title=f"Relevant docs ({len(sections)} sections)",
        content=format_doc_refs(sections),
        confidence=1.0,
        evidence_count=len(sections),
        last_seen=now_ts,
        scope_tags=[],
        source_refs=[f"{fp}:{lr}" for _, fp, lr in sections],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )
