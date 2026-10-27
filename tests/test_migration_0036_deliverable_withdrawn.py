"""Migration 0036: the deliverables view knows withdrawn; up, down and the manifest (ADR-049, PR-2). Temp stores only."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from pathlib import Path

import pytest

import schema_manifest
from migrations import apply_0036

from fixtures.closure_track_fixture import build_tracks_db
from fixtures.live_store_fixture import drop_state_check

SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0036_deliverable_withdrawn.sql"
PID = "proj-a"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """A store below 36 with the v27 view and no state CHECK (the live shape)."""
    build_tracks_db(tmp_path / "state")
    path = tmp_path / "state" / "runtime_coordination.db"
    drop_state_check(path)
    conn = sqlite3.connect(str(path))
    conn.execute(schema_manifest.view_at(35, "deliverables").sql)
    conn.commit()
    conn.close()
    return path


def _q(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(sql, params).fetchall()
        conn.commit()
    finally:
        conn.close()
    return rows


def _rows(path: Path, *states: str, ref: str = "pr:dlv-1") -> None:
    for i, state in enumerate(states):
        _q(path, "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, metadata_json) "
                 "VALUES (?, ?, ?, 't', ?, '{\"deliverable\": true}')", (f"{ref}-{i}", PID, state, ref))


def _view_sql(path: Path) -> str:
    return _q(path, "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'deliverables'")[0][0]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_up_creates_the_v36_view_and_stamps_36(db):
    assert apply_0036.apply_migration(db, SQL) is True
    assert _q(db, "PRAGMA user_version") == [(36,)]
    conn = sqlite3.connect(str(db))
    try:
        assert schema_manifest.validate_view(conn, schema_manifest.view_at(36, "deliverables")) == []
    finally:
        conn.close()
    before = _hash(db)
    assert apply_0036.apply_migration(db, SQL) is False
    assert _hash(db) == before


def test_a_failed_post_condition_leaves_the_store_at_its_version_with_its_view(db, monkeypatch):
    view_before, version_before = _view_sql(db), _q(db, "PRAGMA user_version")
    monkeypatch.setattr(schema_manifest, "validate_view", lambda conn, view, later=(): ["injected mismatch"])
    with pytest.raises(apply_0036.Migration0036Error, match="injected mismatch"):
        apply_0036.apply_migration(db, SQL)
    assert (_view_sql(db), _q(db, "PRAGMA user_version")) == (view_before, version_before)


def test_down_turns_withdrawn_rows_expired_with_one_revert_event_each(db):
    apply_0036.apply_migration(db, SQL)
    _rows(db, "withdrawn", ref="pr:gone")
    _rows(db, "completed", ref="pr:done")
    _rows(db, "proposed", ref="pr:open")
    _q(db, "INSERT INTO coordination_events (event_id, event_type, entity_id, from_state, to_state, actor, reason, "
           "occurred_at, project_id) VALUES ('e1', 'deliverable_withdrawn', 'pr:gone-0', 'ready', 'withdrawn', "
           "'operator', 'P23 not wanted', '2026-10-27T08:00:00Z', ?)", (PID,))

    assert apply_0036.revert_migration(db) is True

    assert dict(_q(db, "SELECT dispatch_id, state FROM dispatches")) == {
        "pr:gone-0": "expired", "pr:done-0": "completed", "pr:open-0": "proposed"}
    [(entity, frm, to, actor, meta, pid)] = _q(
        db, "SELECT entity_id, from_state, to_state, actor, metadata_json, project_id FROM coordination_events "
            "WHERE event_type = 'deliverable_withdrawn_reverted'")
    assert (entity, frm, to, actor, pid) == ("pr:gone-0", "withdrawn", "expired", "migration", PID)
    assert json.loads(meta) == {"withdrawn_from": "ready", "withdraw_reason": "P23 not wanted"}
    assert _q(db, "PRAGMA user_version") == [(35,)]
    conn = sqlite3.connect(str(db))
    try:
        assert schema_manifest.validate_view(conn, schema_manifest.view_at(35, "deliverables")) == []
    finally:
        conn.close()
    assert apply_0036.revert_migration(db) is False


def test_down_without_a_withdraw_event_still_reverts_and_says_so(db):
    apply_0036.apply_migration(db, SQL)
    _rows(db, "withdrawn")
    assert apply_0036.revert_migration(db) is True
    [(meta,)] = _q(db, "SELECT metadata_json FROM coordination_events WHERE event_type = 'deliverable_withdrawn_reverted'")
    assert json.loads(meta) == {"withdrawn_from": None, "withdraw_reason": None}


def test_down_refuses_a_store_that_is_not_at_36(db):
    _q(db, "PRAGMA user_version = 34")
    before = _hash(db)
    with pytest.raises(apply_0036.Migration0036Error, match="found 34"):
        apply_0036.revert_migration(db)
    assert _hash(db) == before


_DERIVED = [
    (("withdrawn",), "withdrawn", (0, 0, 0, 1)),
    (("withdrawn", "withdrawn"), "withdrawn", (0, 0, 0, 2)),
    (("withdrawn", "proposed"), "proposed", (0, 0, 1, 1)),
    (("withdrawn", "ready"), "ready", (0, 0, 1, 1)),
    (("withdrawn", "completed"), "done", (1, 0, 0, 1)),
    (("withdrawn", "running"), "in_progress", (0, 1, 0, 1)),
    (("withdrawn", "expired"), "failed", (0, 0, 0, 1)),
    (("withdrawn", "completed", "proposed"), "proposed", (1, 0, 1, 1)),
]


@pytest.mark.parametrize("states, status, counts", _DERIVED)
def test_the_view_derives_withdrawn_and_counts_it_nowhere_else(db, states, status, counts):
    apply_0036.apply_migration(db, SQL)
    _rows(db, *states)
    [row] = _q(db, "SELECT derived_status, completed_count, in_flight_count, planned_count, withdrawn_count "
                   "FROM deliverables")
    assert (row[0], row[1:]) == (status, counts)


_STATES = ("proposed", "ready", "queued", "active", "running", "claimed", "accepted", "delivering", "completed",
           "failed", "failed_delivery", "dead_letter", "expired", "timed_out", "recovered")


def test_without_withdrawn_rows_the_v36_view_derives_what_the_v35_view_derives(db):
    apply_0036.apply_migration(db, SQL)
    for n, states in enumerate(itertools.combinations_with_replacement(_STATES, 2)):
        _rows(db, *states, ref=f"pr:dlv-{n}")
    _q(db, schema_manifest.view_at(35, "deliverables").sql.replace("VIEW deliverables", "VIEW deliverables_v35"))
    columns = "deliverable_ref, dispatch_count, completed_count, in_flight_count, planned_count, derived_status"
    old = _q(db, f"SELECT {columns} FROM deliverables_v35 ORDER BY deliverable_ref")
    assert len(old) == 120 and _q(db, f"SELECT {columns} FROM deliverables ORDER BY deliverable_ref") == old


def test_the_manifest_moves_to_36_with_the_v35_tables():
    assert schema_manifest.TERMINAL_VERSION == 36
    assert schema_manifest.SCHEMA_MANIFEST[36].tables == schema_manifest.SCHEMA_MANIFEST[35].tables
    assert schema_manifest.view_at(36, "deliverables") != schema_manifest.view_at(35, "deliverables")


def test_a_v36_view_holds_for_an_earlier_version_but_a_v35_view_does_not_hold_36(db):
    apply_0036.apply_migration(db, SQL)
    conn = sqlite3.connect(str(db))
    try:
        assert schema_manifest.validate_view(
            conn, schema_manifest.view_at(35, "deliverables"), (schema_manifest.view_at(36, "deliverables"),)) == []
        conn.execute("DROP VIEW deliverables")
        conn.execute(schema_manifest.view_at(35, "deliverables").sql)
        assert schema_manifest.validate_view(conn, schema_manifest.view_at(36, "deliverables")) == [
            "view 'deliverables': normalized SQL mismatch"]
    finally:
        conn.close()


def test_auto_apply_reaches_36_from_35(db):
    from migrations.auto_apply import auto_apply
    _q(db, "PRAGMA user_version = 35")
    assert auto_apply(db) == [36]
    assert _q(db, "PRAGMA user_version") == [(36,)]
