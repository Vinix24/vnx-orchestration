"""The deliverable-dependencies DAO (ADR-096 §1-2, B1b), called directly on a live-shaped store."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import deliverable_dependencies as dd
from fixtures.live_store_fixture import build_live_store

PID, OTHER = "test-proj", "other-proj"
NOW = "2026-11-20T10:00:00Z"


@pytest.fixture()
def conn(tmp_path: Path):
    db = build_live_store(tmp_path / "state")
    c = sqlite3.connect(str(db), isolation_level=None)
    for pid in (PID, OTHER):
        for dlv in ("dlv-a", "dlv-b", "dlv-c"):
            c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, metadata_json) VALUES (?, ?, "
                      "'proposed', ?)", (dlv, pid, json.dumps({"title": dlv, "deliverable": True})))
    c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, metadata_json) VALUES "
              "('dlv-plain', ?, 'proposed', '{}'), ('dlv-bad', ?, 'proposed', 'not json')", (PID, PID))
    yield c
    c.close()


def _depend(conn, dlv, on, pid=PID, **kw):
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = dd.depend(conn, pid, dlv, on, created_by=kw.pop("created_by", "t0"), now=kw.pop("now", NOW), **kw)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return out


def _undepend(conn, dlv, on, reason, pid=PID, now="2026-11-20T11:00:00Z"):
    conn.execute("BEGIN IMMEDIATE")
    try:
        dd.undepend(conn, pid, dlv, on, reason=reason, removed_by="t0", now=now)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _events(conn, pid=PID):
    return conn.execute("SELECT event_type, entity_type, entity_id, actor, reason, metadata_json FROM "
                        "coordination_events WHERE project_id = ? AND event_type LIKE 'deliverable_dependency_%' "
                        "ORDER BY rowid", (pid,)).fetchall()


def _rows(conn):
    return conn.execute(f"SELECT COUNT(*) FROM {dd.TABLE}").fetchone()[0]


def test_depend_records_a_hard_edge_in_both_directions_with_an_event(conn):
    assert _depend(conn, "dlv-b", "dlv-a") == "added"
    assert _depend(conn, "dlv-c", "dlv-b", kind="soft") == "added"
    b = dd.dependencies(conn, PID, "dlv-b")
    assert set(b) == {"deliverable", "project_id", "depends_on", "depended_on_by"}
    [up], [down] = b["depends_on"], b["depended_on_by"]
    assert (up["depends_on_id"], up["kind"], up["active"], up["removed_reason"]) == ("dlv-a", "hard", True, None)
    assert (up["created_at"], up["created_by"]) == (NOW, "t0")
    assert (down["deliverable_id"], down["kind"]) == ("dlv-c", "soft")
    [(etype, entity, eid, actor, reason, meta), (etype2, _, eid2, *_rest)] = _events(conn)
    assert (etype, entity, eid, actor, reason) == (dd.EVENT_ADDED, "deliverable", "dlv-b", "t0", None)
    assert json.loads(meta) == {"depends_on_id": "dlv-a", "kind": "hard"}
    assert (etype2, eid2) == (dd.EVENT_ADDED, "dlv-c")


@pytest.mark.parametrize("edges, new, cycle", [
    ([], ("dlv-a", "dlv-a"), "dlv-a -> dlv-a"),
    ([("dlv-b", "dlv-a")], ("dlv-a", "dlv-b"), "dlv-a -> dlv-b -> dlv-a"),
    ([("dlv-b", "dlv-a"), ("dlv-c", "dlv-b")], ("dlv-a", "dlv-c"), "dlv-a -> dlv-c -> dlv-b -> dlv-a"),
])
def test_a_cycle_is_refused_and_named(conn, edges, new, cycle):
    for dlv, on in edges:
        _depend(conn, dlv, on, kind="soft")
    before_rows, before_events = _rows(conn), len(_events(conn))
    with pytest.raises(dd.DependencyRefused, match=cycle):
        _depend(conn, *new)
    assert (_rows(conn), len(_events(conn))) == (before_rows, before_events)


def test_a_removed_edge_closes_no_cycle(conn):
    _depend(conn, "dlv-b", "dlv-a")
    _undepend(conn, "dlv-b", "dlv-a", "merged together")
    assert _depend(conn, "dlv-a", "dlv-b") == "added"


@pytest.mark.parametrize("dlv, on, unknown", [
    ("dlv-a", "dlv-x", "dlv-x"), ("dlv-x", "dlv-a", "dlv-x"),
    ("dlv-a", "dlv-plain", "dlv-plain"), ("dlv-bad", "dlv-a", "dlv-bad"),
])
def test_an_unknown_id_on_either_side_is_refused(conn, dlv, on, unknown):
    with pytest.raises(dd.DependencyRefused, match=f"unknown deliverable '{unknown}'"):
        _depend(conn, dlv, on)
    assert _rows(conn) == 0 and _events(conn) == []


def test_dependencies_of_an_unknown_id_is_refused(conn):
    with pytest.raises(dd.DependencyRefused, match="unknown deliverable 'dlv-x'"):
        dd.dependencies(conn, PID, "dlv-x")


@pytest.mark.parametrize("kw, match", [
    ({"kind": "medium"}, "kind 'medium'"), ({"kind": None}, "kind None"),
    ({"created_by": ""}, "created_by"), ({"now": None}, "now"),
])
def test_bad_kind_or_identity_is_refused(conn, kw, match):
    with pytest.raises(dd.DependencyRefused, match=match):
        _depend(conn, "dlv-b", "dlv-a", **kw)
    assert _rows(conn) == 0


def test_a_duplicate_active_edge_is_refused(conn):
    _depend(conn, "dlv-b", "dlv-a")
    with pytest.raises(dd.DependencyRefused, match="already depends on"):
        _depend(conn, "dlv-b", "dlv-a", kind="soft")


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_undepend_refuses_an_empty_reason_and_keeps_the_edge(conn, reason):
    _depend(conn, "dlv-b", "dlv-a")
    with pytest.raises(dd.DependencyRefused, match="--reason"):
        _undepend(conn, "dlv-b", "dlv-a", reason)
    assert dd.dependencies(conn, PID, "dlv-b")["depends_on"][0]["active"] is True
    assert len(_events(conn)) == 1


def test_undepend_keeps_the_row_and_restore_reuses_it(conn):
    _depend(conn, "dlv-b", "dlv-a")
    _undepend(conn, "dlv-b", "dlv-a", "  B2 no longer needs it ")
    [row] = dd.dependencies(conn, PID, "dlv-b")["depends_on"]
    assert (row["active"], row["removed_reason"], row["removed_at"]) == (False, "B2 no longer needs it",
                                                                         "2026-11-20T11:00:00Z")
    with pytest.raises(dd.DependencyRefused, match="no active dependency"):
        _undepend(conn, "dlv-b", "dlv-a", "again")
    assert _depend(conn, "dlv-b", "dlv-a", kind="soft", now="2026-11-20T12:00:00Z") == "restored"
    [row] = dd.dependencies(conn, PID, "dlv-b")["depends_on"]
    assert (row["active"], row["removed_at"], row["removed_reason"], row["kind"]) == (True, None, None, "soft")
    assert _rows(conn) == 1
    trail = _events(conn)
    assert [e[0] for e in trail] == [dd.EVENT_ADDED, dd.EVENT_REMOVED, dd.EVENT_RESTORED]
    assert trail[1][4] == "B2 no longer needs it" and json.loads(trail[1][5])["kind"] == "hard"


def test_undepend_of_a_missing_edge_is_refused(conn):
    with pytest.raises(dd.DependencyRefused, match="no active dependency"):
        _undepend(conn, "dlv-b", "dlv-a", "never there")
    assert _events(conn) == []


def test_two_projects_with_the_same_ids_do_not_see_each_other(conn):
    _depend(conn, "dlv-b", "dlv-a")
    assert dd.dependencies(conn, OTHER, "dlv-b")["depends_on"] == []
    assert dd.dependencies(conn, OTHER, "dlv-a")["depended_on_by"] == []
    assert _depend(conn, "dlv-a", "dlv-b", pid=OTHER) == "added"  # no cycle across tenants
    assert len(_events(conn, OTHER)) == 1 and len(_events(conn, PID)) == 1
    with pytest.raises(dd.DependencyRefused, match="no active dependency"):
        _undepend(conn, "dlv-b", "dlv-a", "wrong tenant", pid=OTHER)


def test_a_change_outside_a_transaction_is_refused(conn):
    with pytest.raises(dd.DependencyRefused, match="BEGIN IMMEDIATE"):
        dd.depend(conn, PID, "dlv-b", "dlv-a", kind="hard", created_by="t0", now=NOW)
    with pytest.raises(dd.DependencyRefused, match="BEGIN IMMEDIATE"):
        dd.undepend(conn, PID, "dlv-b", "dlv-a", reason="x", removed_by="t0", now=NOW)
    assert _rows(conn) == 0


def test_the_edge_rolls_back_with_the_callers_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, metadata_json) VALUES "
                 "('dlv-new', ?, 'proposed', ?)", (PID, json.dumps({"deliverable": True})))
    assert dd.depend(conn, PID, "dlv-new", "dlv-a", created_by="t0", now=NOW) == "added"
    with pytest.raises(dd.DependencyRefused, match="unknown deliverable 'dlv-x'"):
        dd.depend(conn, PID, "dlv-new", "dlv-x", created_by="t0", now=NOW)
    conn.execute("ROLLBACK")
    assert _rows(conn) == 0 and _events(conn) == []
    assert conn.execute("SELECT 1 FROM dispatches WHERE dispatch_id = 'dlv-new'").fetchone() is None


def test_a_store_without_the_table_is_refused(conn):
    conn.execute(f"DROP TABLE {dd.TABLE}")
    for call in (lambda: dd.dependencies(conn, PID, "dlv-a"),
                 lambda: _depend(conn, "dlv-b", "dlv-a")):
        with pytest.raises(dd.DependencyRefused, match="migration 0039"):
            call()


def test_a_store_without_coordination_events_is_refused(conn):
    conn.execute("DROP TABLE coordination_events")
    with pytest.raises(dd.DependencyRefused, match="coordination_events"):
        _depend(conn, "dlv-b", "dlv-a")
    assert _rows(conn) == 0
