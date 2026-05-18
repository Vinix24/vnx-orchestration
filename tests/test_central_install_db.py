"""tests/test_central_install_db.py — migration 0021 + helper coverage.

Five test cases covering pin roundtrip, event roundtrip, upsert semantics,
history ordering/limit, and rollback event chains.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_MIGRATION_SQL = (
    Path(__file__).parent.parent / "schemas" / "migrations" / "0021_central_install_metadata.sql"
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Fresh in-memory DB with the 0021 schema applied."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_MIGRATION_SQL.read_text())
    return db


def test_pin_insert_and_read_roundtrip(conn: sqlite3.Connection) -> None:
    from central_install_db import set_project_pin, get_project_pin

    set_project_pin(conn, "proj-a", "/opt/a", "1.2.3", pinned_by="ci", pin_source="auto")
    pin = get_project_pin(conn, "proj-a", "/opt/a")
    assert pin is not None
    assert pin.pin_version == "1.2.3"
    assert pin.pinned_by == "ci"
    assert pin.pin_source == "auto"


def test_install_event_roundtrip_with_json_details(conn: sqlite3.Connection) -> None:
    from central_install_db import record_install_event, list_install_history

    eid = record_install_event(
        conn,
        project_id="proj-b",
        event_type="install",
        from_version="1.0.0",
        to_version="1.1.0",
        actor="deploy-bot",
        details={"host": "srv-01", "duration_sec": 42},
    )
    assert eid > 0
    history = list_install_history(conn, "proj-b", limit=5)
    assert len(history) == 1
    assert history[0].event_type == "install"
    assert history[0].to_version == "1.1.0"
    assert history[0].details_json is not None


def test_pin_update_overwrites_previous(conn: sqlite3.Connection) -> None:
    from central_install_db import set_project_pin, get_project_pin

    set_project_pin(conn, "proj-c", "/opt/c", "1.0.0")
    set_project_pin(conn, "proj-c", "/opt/c", "2.0.0", pinned_by="human")
    pin = get_project_pin(conn, "proj-c", "/opt/c")
    assert pin is not None
    assert pin.pin_version == "2.0.0"
    assert pin.pinned_by == "human"


def test_history_query_limit_and_ordering(conn: sqlite3.Connection) -> None:
    from central_install_db import record_install_event, list_install_history

    for i in range(5):
        record_install_event(conn, "proj-d", event_type="verify", actor=f"bot-{i}")
    history = list_install_history(conn, "proj-d", limit=3)
    assert len(history) == 3
    # Newest first because of ORDER BY occurred_at DESC
    assert history[0].actor == "bot-4"
    assert history[1].actor == "bot-3"
    assert history[2].actor == "bot-2"


def test_rollback_event_chain(conn: sqlite3.Connection) -> None:
    from central_install_db import record_install_event, list_install_history

    record_install_event(conn, "proj-e", "install", to_version="2.0.0")
    record_install_event(conn, "proj-e", "update", from_version="2.0.0", to_version="2.1.0")
    record_install_event(
        conn, "proj-e", "rollback", from_version="2.1.0", to_version="2.0.0", success=True
    )
    history = list_install_history(conn, "proj-e", limit=10)
    assert len(history) == 3
    types = [h.event_type for h in reversed(history)]  # oldest → newest
    assert types == ["install", "update", "rollback"]
    rollback = history[0]
    assert rollback.from_version == "2.1.0"
    assert rollback.to_version == "2.0.0"
    assert rollback.success is True
