"""Approved proposals plan work once, including recovery after a ledger append failure."""
import json
import sqlite3
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from daily_metrics import proposal_ledger as ledger
from test_planning_deliverables import _build_db, tracks_lib
from .test_propose_command import NOW, _args, _events, _ledger, _run

PID = "a" * 12
SPEC = dict(track="ci-work", title="Reduce CI time", output_kind="pr", task_class="01_code_generation")


def seed(root, *, spec=SPEC, terminal="approved", project="p", pid=PID):
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / "t0_receipts.ndjson").touch(exist_ok=True)
    path = _ledger(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [dict(event="proposed", proposal_id=pid, project_id=project, recorded_at=NOW)]
    if spec != "absent":
        events[0]["deliverable"] = spec
    if terminal:
        events.append(dict(event=terminal, proposal_id=pid, project_id=project, recorded_at=NOW))
    with path.open("a") as handle:
        handle.write("".join(json.dumps(e) + "\n" for e in events))


def track(state, project="p"):
    tracks_lib.create_track(state, SPEC["track"], project, title="CI work", goal_state="Faster CI")


def rows(state):
    with sqlite3.connect(state / tracks_lib.DB_FILENAME) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM dispatches")]


@pytest.mark.parametrize("kind", ["pr", "doc"])
def test_approved_creates_once_in_named_track(tmp_path, kind):
    state = _build_db(tmp_path)
    track(state)
    seed(tmp_path, spec={**SPEC, "output_kind": kind})
    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(lambda _: _run(tmp_path, ["propose", *_args(tmp_path)]), range(2)))
    assert all(p.returncode == 0 for p in runs), [p.stderr for p in runs]
    for _ in range(2):
        proc = _run(tmp_path, ["propose", *_args(tmp_path)])
        assert proc.returncode == 0, proc.stderr
        records = rows(state)
        assert len(records) == 1
        row = records[0]
        assert (row["state"], row["track"], row["output_kind"], row["project_id"]) == ("proposed", SPEC["track"], kind, "p")
        assert row["operator_approved_at"] is None
        assert json.loads(row["metadata_json"])["task_class"] == SPEC["task_class"]
        created = [e for e in _events(tmp_path) if e["event"] == "deliverable_created"]
        assert len(created) == 1
        assert (created[0]["proposal_id"], created[0]["deliverable_id"]) == (PID, row["dispatch_id"])


def test_approved_without_deliverable_is_named(tmp_path):
    state = _build_db(tmp_path)
    seed(tmp_path, spec="absent")
    before = _ledger(tmp_path).read_bytes()
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert PID in proc.stdout and "no deliverable named" in proc.stdout
    assert not rows(state) and _ledger(tmp_path).read_bytes() == before


@pytest.mark.parametrize("terminal", [None, "rejected", "expired"])
def test_unapproved_never_creates(tmp_path, terminal):
    state = _build_db(tmp_path)
    track(state)
    seed(tmp_path, terminal=terminal)
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0, proc.stderr
    assert not rows(state)
    assert not any(e["event"] == "deliverable_created" for e in _events(tmp_path))


def test_unknown_track_fails_then_retries_exact_project(tmp_path):
    state = _build_db(tmp_path)
    track(state, project="other")
    seed(tmp_path)
    before = _ledger(tmp_path).read_bytes()
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode != 0 and SPEC["track"] in proc.stderr
    assert not rows(state) and _ledger(tmp_path).read_bytes() == before
    track(state)
    assert _run(tmp_path, ["propose", *_args(tmp_path)]).returncode == 0
    assert len(rows(state)) == 1


@pytest.mark.parametrize("bad", [None, [], {}, "unknown", *[
    {**SPEC, field: value} for field in SPEC for value in (None, [], "")
], {k: v for k, v in SPEC.items() if k != "task_class"},
    {**SPEC, "output_kind": "post"}, {**SPEC, "task_class": "unknown"}])
def test_malformed_deliverable_is_named_and_never_written(tmp_path, bad):
    seed(tmp_path, spec=bad)
    before = _ledger(tmp_path).read_bytes()
    with pytest.raises(ledger.LedgerUnreadable, match="deliverable"):
        ledger.read_events(_ledger(tmp_path))
    assert _ledger(tmp_path).read_bytes() == before
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode != 0 and "deliverable" in proc.stderr and PID in proc.stderr


@pytest.mark.parametrize("field,bad", [("output_kind", "doc"), ("metadata_json", "null"), ("metadata_json", "{}")])
def test_creation_recovers_after_append_failure(tmp_path, monkeypatch, capsys, field, bad):
    from daily_metrics.commands.propose import run
    state = _build_db(tmp_path)
    track(state)
    seed(tmp_path)
    args = Namespace(project_id="p", now=NOW, store=tmp_path / "store", state_dir=state)
    def fail_append(path, events):
        raise OSError("injected ledger append failure")
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_write", fail_append)
        assert run(args) == 1
    assert "injected ledger append failure" in capsys.readouterr().err
    assert len(rows(state)) == 1
    saved = rows(state)[0][field]
    with sqlite3.connect(state / tracks_lib.DB_FILENAME) as conn:
        conn.execute(f"UPDATE dispatches SET {field} = ?", (bad,))
    assert run(args) == 1 and "conflict" in capsys.readouterr().err
    assert not any(e["event"] == "deliverable_created" for e in _events(tmp_path))
    with sqlite3.connect(state / tracks_lib.DB_FILENAME) as conn:
        conn.execute(f"UPDATE dispatches SET {field} = ?", (saved,))
    assert run(args) == 0
    assert len(rows(state)) == 1
    assert len([e for e in _events(tmp_path) if e["event"] == "deliverable_created"]) == 1


@pytest.mark.parametrize("project,pid", [("other", PID), ("p", PID + "b")])
def test_approval_requires_exact_identity(tmp_path, project, pid):
    state = _build_db(tmp_path)
    track(state)
    seed(tmp_path, terminal=None)
    ledger._write(_ledger(tmp_path), [dict(event="approved", project_id=project, proposal_id=pid)])
    assert _run(tmp_path, ["propose", *_args(tmp_path)]).returncode == 0
    assert not rows(state)


@pytest.mark.parametrize("fault", [None, {}, "unknown", float("nan"), float("inf")])
@pytest.mark.parametrize("kind", ["proposed", "deliverable_created"])
def test_writer_rejects_invalid_payload_before_append(tmp_path, fault, kind):
    path = tmp_path / "proposals.ndjson"
    event = dict(event="proposed", proposal_id=PID, deliverable=fault)
    if isinstance(fault, float):
        event = dict(event="proposed", value=fault)
    if kind == "deliverable_created":
        event = dict(event=kind, proposal_id=PID, deliverable_id=fault)
    with pytest.raises(ledger.LedgerUnreadable):
        ledger._write(path, [event])
    assert not path.exists()


def test_rule_candidate_keeps_deliverable(tmp_path):
    from .test_proposal_ledger import _candidate, _append, DAY
    candidate = _candidate("merges")
    object.__setattr__(candidate, "deliverable", SPEC)
    path = tmp_path / "proposals.ndjson"
    _append(path, [candidate], day=DAY)
    assert ledger.read_events(path)[0]["deliverable"] == SPEC


def test_unreadable_planning_store_is_failure(tmp_path):
    state = _build_db(tmp_path)
    seed(tmp_path)
    db = state / tracks_lib.DB_FILENAME
    db.rename(state / "saved.db")
    db.mkdir()
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode != 0 and "unable to open database" in proc.stderr
    assert not any(e["event"] == "deliverable_created" for e in _events(tmp_path))
