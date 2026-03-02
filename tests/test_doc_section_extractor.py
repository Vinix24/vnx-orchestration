#!/usr/bin/env python3
"""CI tests for doc_section_extractor — parsing, scoring, FTS5 storage."""

import os
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

# Ensure scripts dir is importable
TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from doc_section_extractor import (
    DocFrontmatter,
    DocSection,
    DocSectionExtractor,
    _resolve_docs_dirs,
    categorize_doc,
    extract_tags,
    parse_frontmatter,
    score_doc_section,
    split_sections,
)

SCHEMA_PATH = VNX_ROOT / "schemas" / "quality_intelligence.sql"


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test database with the FTS5 schema."""
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(db_path)
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)
    conn.close()
    return db_path


# ── Frontmatter Tests ──────────────────────────────────────────────


def test_parse_frontmatter_standard():
    content = textwrap.dedent("""\
        ---
        title: API Reference
        status: current
        owner: backend-team
        summary: Complete REST API documentation
        ---
        # API Reference
        Content here.
    """)
    fm = parse_frontmatter(content)
    assert fm is not None
    assert fm.title == "API Reference"
    assert fm.status == "current"
    assert fm.owner == "backend-team"
    assert fm.summary == "Complete REST API documentation"


def test_parse_frontmatter_none():
    content = "# Just a heading\n\nNo frontmatter here."
    fm = parse_frontmatter(content)
    assert fm is None


# ── Section Splitting Tests ────────────────────────────────────────


def test_split_sections_basic():
    content = textwrap.dedent("""\
        # Main Title

        Intro paragraph.

        ## First Section

        Line one of first section.
        Line two of first section.
        Line three of first section.
        Line four of first section.

        ## Second Section

        Line one of second section.
        Line two of second section.
        Line three of second section.
        Line four of second section.

        ## Too Short

        One line only.
    """)
    sections = split_sections(content, None)
    assert len(sections) == 2
    assert sections[0].title == "First Section"
    assert sections[1].title == "Second Section"


def test_section_metadata_detection():
    content = textwrap.dedent("""\
        ## Code Example

        Here is some code:

        ```python
        def hello():
            pass
        ```

        And a table:

        | Col1 | Col2 |
        |------|------|
        | a    | b    |

        And a cross-reference to [API docs](API_REFERENCE.md).
    """)
    sections = split_sections(content, None)
    assert len(sections) == 1
    s = sections[0]
    assert s.has_code_blocks is True
    assert s.code_block_count == 1
    assert s.has_tables is True
    assert s.cross_ref_count == 1


# ── Scoring Tests ──────────────────────────────────────────────────


def test_score_doc_section_rich():
    """Section with code + tables should score higher than plain text."""
    rich = DocSection(
        title="API Endpoints",
        body="Word " * 100 + "\n```python\ncode\n```\n```bash\nmore\n```\n| A | B |\n|---|---|\n| 1 | 2 |",
        line_start=1,
        line_end=20,
        has_code_blocks=True,
        has_tables=True,
        code_block_count=2,
        cross_ref_count=0,
    )
    plain = DocSection(
        title="Overview",
        body="Word " * 100,
        line_start=1,
        line_end=10,
        has_code_blocks=False,
        has_tables=False,
        code_block_count=0,
        cross_ref_count=0,
    )
    rich_score = score_doc_section(rich, None)
    plain_score = score_doc_section(plain, None)
    assert rich_score > plain_score


def test_score_doc_section_archived():
    """Archived doc gets 50% penalty."""
    section = DocSection(
        title="Old API",
        body="Word " * 100,
        line_start=1,
        line_end=10,
        has_code_blocks=False,
        has_tables=False,
        code_block_count=0,
        cross_ref_count=0,
    )
    fm_current = DocFrontmatter(status="current")
    fm_archived = DocFrontmatter(status="archived")
    score_current = score_doc_section(section, fm_current)
    score_archived = score_doc_section(section, fm_archived)
    assert score_archived < score_current
    # Archived should be roughly half
    assert score_archived <= score_current * 0.55


# ── Categorization Tests ──────────────────────────────────────────


def test_categorize_doc_numbered():
    path = Path("/docs/10_ARCHITECTURE.md")
    cat = categorize_doc(path, None)
    assert cat == "architecture"


def test_categorize_doc_api_range():
    path = Path("/docs/25_API_ENDPOINTS.md")
    cat = categorize_doc(path, None)
    assert cat == "api"


def test_categorize_doc_fallback_subdir():
    path = Path("/docs/production/some_guide.md")
    cat = categorize_doc(path, None)
    assert cat == "operations"


# ── Tag Extraction Tests ──────────────────────────────────────────


def test_extract_tags_has_documentation():
    section = DocSection(
        title="Browser Pool Config",
        body="Content",
        line_start=1,
        line_end=5,
        has_code_blocks=False,
        has_tables=False,
        code_block_count=0,
        cross_ref_count=0,
    )
    tags = extract_tags(section, "architecture")
    assert "documentation" in tags
    assert "architecture" in tags
    assert "browser-pool" in tags


# ── Environment Variable Tests ────────────────────────────────────


def test_docs_dirs_from_env(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    monkeypatch.setenv("VNX_DOCS_DIRS", str(docs_dir))
    dirs = _resolve_docs_dirs()
    assert len(dirs) == 1
    assert dirs[0] == docs_dir


def test_docs_dirs_empty_when_unset(monkeypatch):
    monkeypatch.delenv("VNX_DOCS_DIRS", raising=False)
    dirs = _resolve_docs_dirs()
    assert dirs == []


# ── Full Pipeline E2E Test ────────────────────────────────────────


def test_full_extraction_pipeline(tmp_path, monkeypatch):
    """E2E: temp markdown -> extraction -> FTS5 query retrieves section."""
    # Create test database
    db_path = _create_test_db(tmp_path)

    # Create test markdown file
    docs_dir = tmp_path / "test_docs"
    docs_dir.mkdir()
    md_file = docs_dir / "20_API_REFERENCE.md"
    md_file.write_text(textwrap.dedent("""\
        ---
        title: API Reference
        status: current
        summary: REST API documentation
        ---
        # API Reference

        ## SSE Streaming Endpoints

        The SSE streaming system provides real-time updates during crawl operations.
        Each event follows the standard EventSource protocol.
        Events are delivered as newline-delimited JSON objects.
        The connection remains open until the crawl completes.

        ```python
        async def stream_events():
            yield {"event": "progress", "data": {"percent": 50}}
        ```

        See also [architecture](10_ARCHITECTURE.md) for details.

        ## Authentication

        API keys are validated on each request.
        Use the X-API-Key header for authentication.
        Keys can be rotated without downtime.
        Rate limiting applies per key.
    """))

    # Patch PROJECT_ROOT to avoid git calls failing
    monkeypatch.setattr("doc_section_extractor.PROJECT_ROOT", tmp_path)

    extractor = DocSectionExtractor(db_path, [docs_dir])
    extractor.connect()
    try:
        success = extractor.run_extraction()
        assert success is True
        assert extractor.stats["files_processed"] == 1
        assert extractor.stats["sections_stored"] >= 2

        # Verify FTS5 query finds the section
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT title, language, framework FROM code_snippets WHERE language = 'markdown'"
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        assert len(rows) >= 2
        titles = {r["title"] for r in rows}
        assert "SSE Streaming Endpoints" in titles
        assert "Authentication" in titles
        assert all(r["language"] == "markdown" for r in rows)
        assert any(r["framework"] == "api" for r in rows)
    finally:
        extractor.close()
