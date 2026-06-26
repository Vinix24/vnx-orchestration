"""Tests for the doc_relevant intelligence source (build-step 2).

Queries the markdown sections doc_section_extractor indexes into code_snippets
FTS5 and injects compact file:line pointers (not bodies).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

from intelligence_sources import doc_relevant  # noqa: E402


_FTS5_COLS = (
    "title, description, code, file_path, line_range, tags, language, "
    "framework, dependencies, quality_score, usage_count, last_updated"
)


def _make_db(sections: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE VIRTUAL TABLE code_snippets USING fts5({_FTS5_COLS}, "
        "tokenize = 'porter unicode61')"
    )
    for s in sections:
        conn.execute(
            "INSERT INTO code_snippets "
            "(title, description, code, file_path, line_range, tags, language, "
            " framework, dependencies, quality_score, usage_count, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["title"], s.get("description", ""), s.get("code", ""),
                s["file_path"], s["line_range"], s.get("tags", ""),
                s.get("language", "markdown"), "", "", "80", "0", "now",
            ),
        )
    conn.commit()
    return conn


_DOCS = [
    {"title": "Lane selection", "file_path": "docs/core/DISPATCH_RULES.md",
     "line_range": "112-130", "code": "Two lanes ship on main; the door selects the lane for dispatch routing.",
     "language": "markdown"},
    {"title": "Receipt format", "file_path": "docs/core/11_RECEIPT_FORMAT.md",
     "line_range": "1-40", "code": "NDJSON receipts carry the governance audit trail.",
     "language": "markdown"},
    {"title": "A python helper", "file_path": "scripts/x.py",
     "line_range": "1-10", "code": "def dispatch(): pass",
     "language": "python"},  # not markdown — must never match
]


def test_fetch_matches_markdown_only():
    conn = _make_db(_DOCS)
    rows = doc_relevant.fetch_relevant_doc_sections(
        conn, ["scripts/foo.py"], "edit the dispatch routing lane selection"
    )
    conn.close()
    paths = {r[1] for r in rows}
    assert "docs/core/DISPATCH_RULES.md" in paths
    assert "scripts/x.py" not in paths  # python row never returned


def test_fetch_no_match_returns_empty():
    conn = _make_db(_DOCS)
    rows = doc_relevant.fetch_relevant_doc_sections(conn, [], "zzzznomatchterm")
    conn.close()
    assert rows == []


def test_fetch_none_conn_safe():
    assert doc_relevant.fetch_relevant_doc_sections(None, ["a.py"], "x") == []


def test_format_doc_refs_are_pointers_not_bodies():
    sections = [("Lane selection", "docs/core/DISPATCH_RULES.md", "112-130")]
    out = doc_relevant.format_doc_refs(sections)
    assert "docs/core/DISPATCH_RULES.md:112-130" in out
    assert "Lane selection" in out
    # No body content injected — only the pointer + heading.
    assert "Two lanes ship" not in out


def test_build_item():
    conn = _make_db(_DOCS)
    item = doc_relevant.build_doc_relevant_item(
        conn, "d-1", ["scripts/foo.py"], "dispatch routing lane selection", "now"
    )
    conn.close()
    assert item is not None
    assert item.item_class == "doc_relevant"
    assert item.confidence == 1.0
    assert "docs/core/DISPATCH_RULES.md:112-130" in item.content
    assert any("DISPATCH_RULES.md" in ref for ref in item.source_refs)


def test_build_item_none_when_no_match():
    conn = _make_db(_DOCS)
    item = doc_relevant.build_doc_relevant_item(conn, "d-2", [], "zzzznomatch", "now")
    conn.close()
    assert item is None


def test_doc_relevant_in_direct_injection_set():
    from intelligence_sources._common import _DIRECT_INJECTION_CLASSES
    assert "doc_relevant" in _DIRECT_INJECTION_CLASSES


def test_doc_relevant_renders_to_worker():
    """Regression: doc_relevant content must reach the worker via the renderer."""
    from intelligence_injection import format_intelligence_items
    from intelligence_sources._common import IntelligenceItem
    item = IntelligenceItem(
        item_id="doc", item_class="doc_relevant", title="docs",
        content="## RELEVANT DOCS\n- `docs/core/DISPATCH_RULES.md:112-130` — Lane selection",
        confidence=1.0, evidence_count=1, last_seen="now", scope_tags=[],
    )
    section = format_intelligence_items([item])
    assert "docs/core/DISPATCH_RULES.md:112-130" in section
