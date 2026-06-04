"""conftest — provides the broken Supabase mock.

THIS FILE HAS THE BUG. _make_supabase_mock returns the same response on
every paginated call → infinite loop. Worker must fix it to return
stateful responses (see instruction.md).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_supabase_mock() -> MagicMock:
    """Build a mock Supabase client. BROKEN: returns same response every call."""
    response = MagicMock()
    response.data = [{"id": i, "name": f"row_{i}"} for i in range(10)]
    response.next_cursor = "page-2"

    client = MagicMock()
    client.table.return_value.select.return_value.range.return_value.execute.return_value = response
    return client


@pytest.fixture
def supabase_mock():
    return _make_supabase_mock()
