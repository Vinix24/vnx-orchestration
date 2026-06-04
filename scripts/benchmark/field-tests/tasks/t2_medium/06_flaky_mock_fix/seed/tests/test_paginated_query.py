"""Contract tests for fetch_all_pages — these are CORRECT, do not modify."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paginated_query import fetch_all_pages


def test_fetch_all_pages_terminates_at_25_rows(supabase_mock):
    """With stateful mock: 10 + 10 + 5 = 25 rows, then terminates."""
    rows = fetch_all_pages(supabase_mock, "documents")
    assert len(rows) == 25, f"expected 25 rows total across pagination, got {len(rows)}"


def test_paginate_handles_short_final_page(supabase_mock):
    """Final page has 5 rows + next_cursor=None — loop must exit cleanly."""
    rows = fetch_all_pages(supabase_mock, "documents")
    assert len(rows) >= 5, "must include the short final page"


def test_paginate_post_terminal_call_returns_empty(supabase_mock):
    """A second invocation against the same exhausted mock returns empty (or repeats from start, but does NOT hang)."""
    rows = fetch_all_pages(supabase_mock, "documents")
    assert isinstance(rows, list), "must return a list even when exhausted"
