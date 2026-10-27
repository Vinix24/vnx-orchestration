"""Migration 0036: the deliverables view knows withdrawn; up, down and the manifest (ADR-049, PR-2). Temp stores only."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import append_receipt
import schema_manifest
from migrations import apply_0036

from fixtures.closure_track_fixture import build_tracks_db
from fixtures.live_store_fixture import drop_state_check

SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0036_deliverable_withdrawn.sql"
PID = "proj-a"
INTENT, ABORTED, LEDGER = "migration_0036_revert", "migration_0036_revert_aborted", "t0_receipts.ndjson"


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
    assert _ledger(db) == []


# --- the down path books the ledger first (ADR-005) ----------------------------

def _ledger(path: Path) -> list[dict]:
    ledger = path.parent / LEDGER
    if not ledger.is_file():
        return []
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in lines if r.get("trigger") in (INTENT, ABORTED)]


def _states(path: Path) -> dict:
    return dict(_q(path, "SELECT dispatch_id, state FROM dispatches"))


def _reverted_events(path: Path) -> list[tuple]:
    return _q(path, "SELECT entity_id FROM coordination_events WHERE event_type = 'deliverable_withdrawn_reverted'")


def _assert_v35_view(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        assert schema_manifest.validate_view(conn, schema_manifest.view_at(35, "deliverables")) == []
    finally:
        conn.close()


def _two_withdrawn(path: Path) -> None:
    apply_0036.apply_migration(path, SQL)
    _rows(path, "withdrawn", ref="pr:a")
    _rows(path, "withdrawn", ref="pr:b")


def test_down_books_one_intent_line_naming_both_rows_before_they_change(db, monkeypatch):
    _two_withdrawn(db)
    real, seen = append_receipt.append_receipt_payload, []

    def _record(receipt, **kwargs):
        seen.append((receipt["trigger"], _states(db), _q(db, "PRAGMA user_version")))
        return real(receipt, **kwargs)

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _record)
    assert apply_0036.revert_migration(db) is True
    assert seen == [(INTENT, {"pr:a-0": "withdrawn", "pr:b-0": "withdrawn"}, [(36,)])]
    [line] = _ledger(db)
    assert (line["event_type"], line["receipt_kind"], line["trigger"]) == ("state_mutation", "state_mutation", INTENT)
    assert line["rows"] == [{"dispatch_id": "pr:a-0", "project_id": PID}, {"dispatch_id": "pr:b-0", "project_id": PID}]
    assert (line["from_state"], line["to_state"], line["status"]) == ("withdrawn", "expired", f"{INTENT}:{line['revert_id']}")
    assert _states(db) == {"pr:a-0": "expired", "pr:b-0": "expired"}
    assert sorted(_reverted_events(db)) == [("pr:a-0",), ("pr:b-0",)]
    assert _q(db, "PRAGMA user_version") == [(35,)]
    _assert_v35_view(db)


def test_two_reverts_each_book_their_own_line(db):
    _two_withdrawn(db)
    assert apply_0036.revert_migration(db) is True
    apply_0036.apply_migration(db, SQL)
    _q(db, "UPDATE dispatches SET state = 'withdrawn'")
    assert apply_0036.revert_migration(db) is True
    first, second = _ledger(db)
    assert first["rows"] == second["rows"] and first["revert_id"] != second["revert_id"]


@pytest.mark.parametrize("result", [None, SimpleNamespace(), SimpleNamespace(status=None),
                                    SimpleNamespace(status="duplicate"), SimpleNamespace(status="Appended"),
                                    SimpleNamespace(status="weird"), RuntimeError("injected: ledger down")])
def test_a_ledger_that_does_not_take_the_line_refuses_the_revert_with_nothing_changed(db, monkeypatch, result):
    _two_withdrawn(db)
    before = _hash(db)

    def _refuse(receipt, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _refuse)
    with pytest.raises(apply_0036.Migration0036Error, match=f"the {INTENT} receipt was not written to the ledger"):
        apply_0036.revert_migration(db)
    assert _hash(db) == before
    assert _q(db, "PRAGMA user_version") == [(36,)]
    assert _states(db) == {"pr:a-0": "withdrawn", "pr:b-0": "withdrawn"} and _reverted_events(db) == []


def test_a_transaction_that_fails_after_the_intent_line_books_the_aborted_line(db, monkeypatch):
    _two_withdrawn(db)
    view_before = _view_sql(db)

    def _broken(conn, version):
        raise sqlite3.OperationalError("injected: view creation fails")

    monkeypatch.setattr(apply_0036, "_create_view", _broken)
    with pytest.raises(sqlite3.OperationalError, match="injected: view creation fails"):
        apply_0036.revert_migration(db)
    assert _q(db, "PRAGMA user_version") == [(36,)] and _view_sql(db) == view_before
    assert _states(db) == {"pr:a-0": "withdrawn", "pr:b-0": "withdrawn"} and _reverted_events(db) == []
    intent, aborted = _ledger(db)
    assert (intent["trigger"], aborted["trigger"], aborted["revert_id"]) == (INTENT, ABORTED, intent["revert_id"])
    assert aborted["abort_cause"] == "OperationalError: injected: view creation fails"
    assert aborted["status"] == f"{ABORTED}:{intent['revert_id']}"


def test_a_withdraw_between_the_intent_line_and_the_transaction_refuses_the_revert(db, monkeypatch):
    _two_withdrawn(db)
    _rows(db, "proposed", ref="pr:late")
    real = append_receipt.append_receipt_payload

    def _then_withdraw(receipt, **kwargs):
        result = real(receipt, **kwargs)
        if receipt["trigger"] == INTENT:
            _q(db, "UPDATE dispatches SET state = 'withdrawn' WHERE dispatch_id = 'pr:late-0'")
        return result

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _then_withdraw)
    with pytest.raises(apply_0036.Migration0036Error, match="withdrawn rows changed after they were read"):
        apply_0036.revert_migration(db)
    assert _q(db, "PRAGMA user_version") == [(36,)] and _reverted_events(db) == []
    assert set(_states(db).values()) == {"withdrawn"}
    intent, aborted = _ledger(db)
    assert (len(intent["rows"]), aborted["trigger"]) == (2, ABORTED)
    assert aborted["abort_cause"].startswith("Migration0036Error: apply_0036 down: the withdrawn rows changed")


def test_a_store_reverted_between_the_intent_line_and_the_transaction_books_the_aborted_line(db, monkeypatch):
    _two_withdrawn(db)
    real = append_receipt.append_receipt_payload

    def _then_revert_elsewhere(receipt, **kwargs):
        result = real(receipt, **kwargs)
        if receipt["trigger"] == INTENT:
            _q(db, "PRAGMA user_version = 35")
        return result

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _then_revert_elsewhere)
    assert apply_0036.revert_migration(db) is False
    assert _states(db) == {"pr:a-0": "withdrawn", "pr:b-0": "withdrawn"} and _reverted_events(db) == []
    intent, aborted = _ledger(db)
    assert (aborted["trigger"], aborted["revert_id"]) == (ABORTED, intent["revert_id"])
    assert aborted["abort_cause"].startswith("already_reverted")


def test_an_aborted_line_the_ledger_refuses_goes_to_stderr_and_the_cause_is_raised(db, monkeypatch, capsys):
    _two_withdrawn(db)
    real = append_receipt.append_receipt_payload

    def _intent_only(receipt, **kwargs):
        return real(receipt, **kwargs) if receipt["trigger"] == INTENT else SimpleNamespace(status="duplicate")

    def _broken(conn, version):
        raise sqlite3.OperationalError("injected: view creation fails")

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _intent_only)
    monkeypatch.setattr(apply_0036, "_create_view", _broken)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        apply_0036.revert_migration(db)
    [intent] = _ledger(db)
    assert f"intent line of revert {intent['revert_id']} without its abort" in capsys.readouterr().err
    assert _states(db) == {"pr:a-0": "withdrawn", "pr:b-0": "withdrawn"}


def test_down_without_withdrawn_rows_books_nothing_and_brings_the_v35_view_back(db):
    apply_0036.apply_migration(db, SQL)
    _rows(db, "completed", ref="pr:done")
    assert apply_0036.revert_migration(db) is True
    assert _ledger(db) == [] and not (db.parent / LEDGER).exists()
    assert _q(db, "PRAGMA user_version") == [(35,)] and _states(db) == {"pr:done-0": "completed"}
    _assert_v35_view(db)


def test_a_withdraw_that_lands_before_a_rowless_revert_refuses_it(db, monkeypatch):
    apply_0036.apply_migration(db, SQL)
    _rows(db, "proposed", ref="pr:late")
    real = apply_0036._read_for_revert

    def _then_withdraw(path):
        rows = real(path)
        _q(db, "UPDATE dispatches SET state = 'withdrawn'")
        return rows

    monkeypatch.setattr(apply_0036, "_read_for_revert", _then_withdraw)
    with pytest.raises(apply_0036.Migration0036Error, match="withdrawn rows changed"):
        apply_0036.revert_migration(db)
    assert _q(db, "PRAGMA user_version") == [(36,)] and _states(db) == {"pr:late-0": "withdrawn"}
    assert _ledger(db) == []


def test_down_at_35_books_nothing(db):
    apply_0036.apply_migration(db, SQL)
    assert apply_0036.revert_migration(db) is True
    _rows(db, "proposed")
    assert apply_0036.revert_migration(db) is False
    assert _ledger(db) == []


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
