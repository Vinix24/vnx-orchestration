"""builders — sample query-building functions for inspector tests."""
from __future__ import annotations


def build_user_query(user_id: int) -> str:
    """UNSAFE: uses f-string SQL — query_inspector should flag this."""
    return f"SELECT * FROM users WHERE id = {user_id}"


def build_user_query_safe(user_id: int) -> tuple[str, tuple]:
    """SAFE: parameterized — query_inspector should NOT flag this."""
    return "SELECT * FROM users WHERE id = ?", (user_id,)
