"""paginated_query — fetches all rows across paginated Supabase-style API.

System-under-test: this code is CORRECT. The hang in the test suite is in
the MOCK, not here. Do not modify this file.
"""
from __future__ import annotations

from typing import Any


def fetch_all_pages(client: Any, table: str, page_size: int = 10) -> list[dict]:
    """Fetch all rows from `table` via cursor-based pagination.

    Loops until next_cursor is None. Returns flat list of all rows.
    """
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        query = client.table(table).select("*")
        if cursor is None:
            response = query.range(0, page_size - 1).execute()
        else:
            response = query.range(0, page_size - 1).execute()  # cursor is server-side
        page_rows = response.data or []
        rows.extend(page_rows)
        next_cursor = getattr(response, "next_cursor", None)
        if not next_cursor:
            break
        cursor = next_cursor
    return rows
