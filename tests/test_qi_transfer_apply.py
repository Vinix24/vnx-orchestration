#!/usr/bin/env python3
"""Tests for qi_transfer_apply.py (reattr L3b): apply and resume move the row, the source last; undo puts it back.

Every database, receipt store, plan and journal lives under tmp_path. The script only runs as a
subprocess, so on a tree without it a test fails on an assertion, never on an ImportError.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pattern_reattribution as pr
import pytest
import qi_transfer_plan as qtp
from test_pattern_reattribution import _db_in, _insert_success_pattern, _offered_to, _receipt
from test_qi_transfer_plan import _junction, _sql

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
FIVE = ("success_patterns", "antipatterns") + qtp.JUNCTION_TABLES
A, B = "alpha", "beta"  # the row belongs to A and sits in B's own file
TITLE = "mis filed"  # U+2028 ends a line for str.splitlines, not for the journal
_CRASH = """import os, signal, sys
sys.path.insert(0, {lib!r})
import qi_transfer_apply as qa
real = qa._journal
def crash(path, line):
    if line["state"] == {state!r} and not {after}:
        os.kill(os.getpid(), signal.SIGKILL)
    real(path, line)
    if line["state"] == {state!r} and {after}:
        os.kill(os.getpid(), signal.SIGKILL)
qa._journal = crash
sys.exit(qa.main(sys.argv[1:]))
"""
_STEP_3 = """import itertools, sqlite3, sys
sys.path.insert(0, {lib!r})
import qi_transfer_apply as qa
qa._WAIT, real, calls = 0.05, qa.read_pre_image, itertools.count(1)
def writer(conn, table, row_id):  # step 3 reads the source after the copy verified: a second writer on the copy
    try:  # the writer's connection closes as soon as the statement ends
        next(calls) == 2 and {sql!r} and sqlite3.connect({dest!r}, timeout=0, isolation_level=None).execute({sql!r})
    except sqlite3.OperationalError as exc:
        print(exc, file=sys.stderr)
    return real(conn, table, row_id)
qa.read_pre_image = writer
sys.exit(qa.main(sys.argv[1:]))
"""


def _rows(db: Path, table: str):
    with closing(qtp.open_readonly(db)) as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _snapshot(world):
    return [{t: _rows(db, t) for t in FIVE + ("pattern_activation",)} for db in (world["src"], world["dest"])]


def _script(name: str, *args, code: str = ""):
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS")}
    head = ["-c", code] if code else [str(LIB / name)]
    return subprocess.run([sys.executable, *head, *map(str, args)], capture_output=True, text=True, env=env)


@pytest.fixture
def world(tmp_path: Path):
    """d-1: receipt in B's store, body and report path name A, so A wins; the row cites d-1."""
    root, plan_path = tmp_path / "vnx-data", tmp_path / "plans" / "plan.json"
    src, dest = _db_in(tmp_path, B), _db_in(tmp_path, A)
    _receipt(root, B, "d-1", project_id=A, report_path=f"/u/.vnx-data/{A}/unified_reports/d-1.md")
    row = _insert_success_pattern(src, project_id=B, source_dispatch_ids=["d-1"], title=TITLE)
    _junction(src, qtp.pattern_id_for("success_patterns", row), B)
    _sql(src, "UPDATE pattern_injection_outcome SET evidence = ?", b"\x00\xff")  # bytes through apply and undo
    for db in (src, dest):
        _sql(db, "CREATE TABLE pattern_activation (pattern_id TEXT, state TEXT, blob BLOB)")
        _sql(db, "INSERT INTO pattern_activation VALUES (?, 'active', ?)", f"intel_sp_{row}", b"\x00\xff")
    proc = _script("qi_transfer_plan.py", "--db", src, "--vnx-data-root", root, "--json", "--out", plan_path)
    assert proc.returncode == 0, proc.stderr
    assert (plan := json.loads(plan_path.read_text()))["moves"] == 1
    return {"root": root, "src": src, "dest": dest, "row": row, "plan_path": plan_path, "plan": plan,
            "journal": qtp.journal_path(src, plan["plan_id"])}


def _apply(world, plan=None, reason="test transfer", code=""):
    return _script("qi_transfer_apply.py", "apply", "--plan", plan or world["plan_path"], "--reason", reason,
                   "--vnx-data-root", world["root"], code=code)


def _resume(world, plan_id=None, code=""):
    return _script("qi_transfer_apply.py", "resume", "--plan-id", plan_id or world["plan"]["plan_id"],
                   "--vnx-data-root", world["root"], code=code)


def _undo(world, code=""):
    return _script("qi_transfer_apply.py", "undo", "--plan-id", world["plan"]["plan_id"], "--reason", "test undo",
                   "--vnx-data-root", world["root"], code=code)


def _crash(world, state: str, after: bool = True, run=_apply) -> None:
    """An apply (or ``run``) killed with SIGKILL right after (or right before) it journals ``state``."""
    proc = run(world, code=_CRASH.format(lib=str(LIB), state=state, after=after))
    assert proc.returncode == -signal.SIGKILL, proc.stderr


def _preexisting(world) -> int:
    """The destination already holds the row's content under A (another id, no junction rows)."""
    keep = _insert_success_pattern(world["dest"], project_id=A, source_dispatch_ids=["d-1"], title=TITLE)
    first_seen = world["plan"]["planned_moves"][0]["pre_image"]["row"]["first_seen"]
    _sql(world["dest"], "UPDATE success_patterns SET first_seen = ? WHERE id = ?", first_seen, keep)
    return keep


def _outcomes(proc) -> list:
    assert proc.stdout, proc.stderr
    return sorted(json.loads(proc.stdout)["outcomes"].values())


def _journal(world) -> list:
    return [json.loads(x) for x in world["journal"].read_bytes().splitlines() if json.loads(x)["move"] != "*"]


def _rewrite(world, state, edit, keep):
    """The last ``state`` line edited (a dict merges and "missing" drops a key, a callable maps the line);
    only ``keep`` of the lines after it stay."""
    raw = world["journal"].read_bytes().splitlines()
    i = max(n for n, x in enumerate(raw) if json.loads(x)["state"] == state)
    line = json.loads(raw[i])
    line = edit(line) if callable(edit) else {k: v for k, v in {**line, **edit}.items() if v != "missing"}
    raw = raw[:i] + [json.dumps(line).encode()] + raw[i + 1:][:keep]
    world["journal"].write_bytes(b"".join(x + b"\n" for x in raw))


def _pre(**kw):
    """A planned-line edit: these pre_image fields replaced ("missing" drops one, a callable maps the old value)."""
    def edit(line):
        pre = {**line["pre_image"], **{k: v(line["pre_image"][k]) if callable(v) else v for k, v in kw.items()}}
        return {**line, "pre_image": {k: v for k, v in pre.items() if v != "missing"}}
    return edit


def test_apply_moves_the_row_and_removes_it_from_the_source(world):
    outside = [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])]
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["done"], proc.stderr
    assert json.loads(proc.stdout)["notes"] == {}  # step 3 ran: no move keeps its row in the source
    assert all(_rows(world["src"], t) == [] for t in FIVE)
    [copy] = _rows(world["dest"], "success_patterns")
    assert copy["project_id"] == A and qtp.content_key(copy) == world["plan"]["planned_moves"][0]["content_key"]
    for t in qtp.JUNCTION_TABLES:
        assert [(j["pattern_id"], j["project_id"]) for j in _rows(world["dest"], t)] == [(f"intel_sp_{copy['id']}", A)]
    lines = _journal(world)
    assert [x["state"] for x in lines] == ["planned", "copied", "verified", "source_removed", "done"]
    assert lines[0]["pre_image"]["row"]["id"] == world["row"] and all(x["ts"] for x in lines)
    assert lines[1]["created"] is True and [j[2] for j in lines[1]["junction"]] == [True, True, True]
    assert [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])] == outside  # outside the five


@pytest.mark.parametrize("outcome,edit", [
    ("drift", lambda w: _sql(w["src"], "UPDATE success_patterns SET description = 'edited'")),
    ("evidence_changed", lambda w: _receipt(w["root"], B, "d-1", project_id=B)),  # body B in B's store: a tie
    ("user_version_changed", lambda w: _sql(w["dest"], "PRAGMA user_version = 99")),
    ("schema_gap", lambda w: _sql(w["dest"], "ALTER TABLE success_patterns DROP COLUMN description")),
    ("refused: UNIQUE constraint failed", lambda w: _sql(w["dest"], "INSERT INTO pattern_usage (pattern_id, "
     "pattern_title, pattern_hash, project_id) VALUES ('intel_sp_1', 'other', 'other', ?)", A)),  # the copy's id
], ids=["drift", "evidence_changed", "user_version_changed", "schema_gap", "junction_key_held"])
def test_a_refused_move_changes_nothing(world, outcome, edit):
    edit(world)
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 1 and _outcomes(proc)[0].startswith(outcome) and _snapshot(world) == before
    assert json.loads(proc.stdout)["notes"] == {}


def test_content_the_destination_holds_is_not_inserted_again(world):
    keep = _preexisting(world)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"]
    assert [r["id"] for r in _rows(world["dest"], "success_patterns")] == [keep]
    assert [(x["dest_id"], x["created"]) for x in _journal(world) if x["state"] == "copied"] == [(keep, False)]
    assert _rows(world["src"], "success_patterns") == []  # the content stands in the destination: step 3 runs


_MALFORMED = [("created", "true"), ("created", 1), ("dest_id", True), ("dest_id", "1"), ("row_key", 7),
              ("pattern_id", "intel_sp_9"), ("cols", ["title"]), ("cols", {"success_patterns": "title"}),
              ("junction", [["pattern_usage", "k"]]), ("junction", [["sqlite_master", "k", True]]),
              ("junction", [["pattern_usage", "k", "true"]])]
_MALFORMED += [(f, v) for f in dict(_MALFORMED) for v in (None, "missing")]  # None and missing for every field
_PLANNED = [("reason", {"reason": v}) for v in (" ", 7, None, "missing")]
_PLANNED += [("pre_image", {"pre_image": v}) for v in ([], None, "missing")]
_PLANNED += [(f"pre_image.{f}", _pre(**{f: v})) for f, v in [
    ("table", "antipatterns"), ("table", "missing"), ("row", None), ("row", "missing"),
    ("row", lambda r: {**r, "id": 99}), ("row", lambda r: {**r, "id": True}),  # a bool is no id
    ("row", lambda r: {**r, "title": "other"}),  # not the content the plan moves
    ("junction", None), ("junction", "missing"), ("junction", lambda j: {"pattern_usage": j["pattern_usage"]}),
    ("junction", lambda j: {**j, "pattern_usage": [{**j["pattern_usage"][0], "pattern_id": "intel_sp_9"}]}),
    ("pattern_id", "intel_sp_9"), ("pattern_id", None), ("pattern_id", "missing")]]
_GAMMA = "UPDATE success_patterns SET project_id = 'gamma'"


@pytest.mark.parametrize("line,edit,keep,damage,outcome,logged", [
    ("copied", {}, 0, "", "done", ("verified", "source_removed", "done")),  # the source row is already gone
    ("copied", {}, 0, "UPDATE success_patterns SET title = 'edited'", "verify_failed", ("verify_failed",)),
    ("copied", {}, 0, "UPDATE pattern_usage SET pattern_title = 'edited'", "verify_failed", ("verify_failed",)),
    ("copied", {}, 1, "", "done", ("source_removed", "done")),
    ("copied", {}, 1, _GAMMA, "destination_missing", ("destination_missing",)),  # step 3 reads the copy again
    ("copied", {}, 2, "", "done", ("done",)), ("copied", {}, 3, "", "already_present", ()),
    ("copied", {}, 3, _GAMMA, "destination_missing", ()),
    ("copied", {}, 2, _GAMMA, "destination_missing", ("destination_missing",)),  # source_removed: the copy read again
    ("copied", {}, 2, "DELETE FROM success_patterns", "destination_missing", ("destination_missing",)),
    ("copied", {"state": "source_removed"}, 0, "", "refused", ()), ("copied", {"state": "done"}, 0, "", "refused", ()),
    ("copied", {"state": "planned"}, 1, "", "refused", ()),  # verified, and no copied line before it
    ("planned", {}, 0, "", "drift", ("drift",)),  # a valid planned line: step 1 again, on a source row that is gone
] + [("copied", {f: v}, keep, "", f"valid {f}", ()) for f, v in _MALFORMED for keep in (0, 1)]
  + [("copied", {f: v}, keep, "", f"valid {f}", ()) for f, v in _MALFORMED[:2] for keep in (2, 3)]
  + [("planned", e, 2, "", f"valid {f}", ()) for f, e in _PLANNED]  # at verified: the line step 3 reads
  + [("planned", e, keep, "", f"valid {f}", ()) for f, e in (_PLANNED[3], _PLANNED[-1]) for keep in (0, 1, 3, 4)])
def test_a_second_apply_finishes_from_the_journal_and_refuses_what_it_cannot_prove(
        world, line, edit, keep, damage, outcome, logged):
    assert _apply(world).returncode == 0
    _rewrite(world, line, edit, keep)
    if damage:
        _sql(world["dest"], damage)
    before, lines = _snapshot(world), _journal(world)
    for retry in (_resume, _apply)[:1 if logged else 2]:  # a run that journals nothing answers the same again
        proc = retry(world)
        assert proc.returncode == int(outcome not in ("done", "already_present")), proc.stdout + proc.stderr
        assert outcome in _outcomes(proc)[0] and _snapshot(world) == before
        gone = "source row is removed" in str(json.loads(proc.stdout)["notes"])  # only the journal has it
        assert gone == (outcome == "destination_missing" and keep > 1)
    assert tuple(x["state"] for x in _journal(world)[len(lines):]) == logged


@pytest.mark.parametrize("state,after,in_dest,in_src,pre", [
    ("copied", True, 0, 1, False),  # journaled, the destination transaction never committed
    ("copied", True, 1, 1, True),  # same, onto content the destination held: its junction rows rolled back
    ("verified", True, 1, 1, False),  # between the two transactions: the row is in both files
    ("source_removed", False, 1, 0, False),  # the source deleted, its journal line never written
])
def test_a_crash_loses_no_row_and_resume_finishes_at_done(world, state, after, in_dest, in_src, pre):
    if pre:
        _preexisting(world)
    _crash(world, state, after)
    assert [len(_rows(world[db], "success_patterns")) for db in ("dest", "src")] == [in_dest, in_src]
    proc = _resume(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present" if pre else "done"], proc.stderr
    assert all(_rows(world["src"], t) == [] for t in FIVE)
    assert [len(_rows(world["dest"], t)) for t in FIVE] == [1, 0, 1, 1, 1]
    assert _journal(world)[-1]["state"] == "done"


@pytest.mark.parametrize("after,where,damage,outcome", [
    (True, "src", "UPDATE success_patterns SET description = 'edited'", "drift"),
    (True, "src", "UPDATE pattern_usage SET pattern_title = 'edited'", "drift"),  # a junction row counts too
    (True, "dest", "UPDATE success_patterns SET title = 'edited'", "destination_missing"),
    (True, "planned", {"pre_image": "missing"}, "valid pre_image"),
    (False, "dest", "UPDATE success_patterns SET title = 'edited'", "verify_failed"),  # copied, never verified
    (True, "dest", "BEGIN IMMEDIATE", "destination_locked"), (True, "src", "BEGIN IMMEDIATE", "source_locked"),
], ids=["source_row", "source_junction", "copy_changed", "planned_malformed", "not_verified", "dest_lock", "src_lock"])
def test_step_3_deletes_nothing_unless_the_source_is_as_journaled_and_the_copy_verifies(
        world, after, where, damage, outcome):
    _crash(world, "verified", after)  # the row stands in both files
    if where == "planned":
        _rewrite(world, where, damage, 2)
    elif damage == "BEGIN IMMEDIATE":  # another connection holds the lock while resume runs: refused once, no retry
        world["lock"] = sqlite3.connect(world[where], isolation_level=None).execute(damage)  # the cursor keeps it open
    else:
        _sql(world[where], damage)
    before = _snapshot(world)
    proc = _resume(world, code=_STEP_3.format(lib=str(LIB), dest="", sql=""))  # a held lock waits 0.05 s, not 30
    assert proc.returncode == 1 and outcome in _outcomes(proc)[0] and _snapshot(world) == before, proc.stdout
    assert "source_removed" not in [x["state"] for x in _journal(world)]
    kept = ["step 3 deleted nothing" in n for n in json.loads(proc.stdout)["notes"].values()]
    assert kept == [True] * (outcome in ("drift", "destination_missing") or outcome.endswith("_locked"))


@pytest.mark.parametrize("mode", ["delete", "wal"])
@pytest.mark.parametrize("sql", ["UPDATE success_patterns SET title = 'edited'", "DELETE FROM success_patterns"])
def test_no_writer_changes_the_copy_between_its_check_and_the_source_delete(world, mode, sql):
    _sql(world["dest"], f"PRAGMA journal_mode={mode}")
    proc = _apply(world, code=_STEP_3.format(lib=str(LIB), dest=str(world["dest"]), sql=sql))
    intact = [r["title"] for r in _rows(world["dest"], "success_patterns")] == [TITLE]  # neither edited nor deleted
    assert intact or _rows(world["src"], "success_patterns"), "source row deleted, the copy no longer the verified one"
    assert "database is locked" in proc.stderr and proc.returncode == 0 and _outcomes(proc) == ["done"], proc.stderr


def test_misroute_replay(world):
    """Body and report path name A, the store is B, the row sits in B's file."""
    index = pr.build_attribution_index(world["root"])
    with pytest.raises(pr.CrossDatabaseMove):  # the relabel tool reaches no consumer of A
        pr.apply_decisions(world["src"], pr.scan_database(world["src"], index))
    assert _offered_to(world["dest"], A) == [] and [i.title for i in _offered_to(world["src"], B)] == [TITLE]
    proc = _apply(world)
    assert proc.returncode == 0, proc.stderr
    assert [i.title for i in _offered_to(world["dest"], A)] == [TITLE] and _offered_to(world["src"], B) == []


@pytest.mark.parametrize("edit", ["plan_id", "pre_image", "table", "plan_version", "missing_plan", "blank_reason",
                                  "unknown_plan_id", "not_a_plan_id"])
def test_a_tampered_or_missing_plan_or_a_blank_reason_exits_2_before_any_write(world, edit):
    plan, move = world["plan"], world["plan"]["planned_moves"][0]
    if edit == "pre_image":
        move["pre_image"]["row"]["title"] = "other"
    elif edit == "table":
        move["table"] = "sqlite_master"
    elif edit in ("plan_id", "plan_version"):
        plan[edit] = None
    world["plan_path"].write_text(json.dumps(plan))
    before = _snapshot(world)
    if edit in ("unknown_plan_id", "not_a_plan_id"):
        proc = _resume(world, "0123456789abcdef" if edit == "unknown_plan_id" else "../x")
    else:
        proc = _apply(world, plan=world["plan_path"].with_name("missing.json") if edit == "missing_plan" else None,
                      reason=" " if edit == "blank_reason" else "test transfer")
    assert proc.returncode == 2 and proc.stdout == "" and _snapshot(world) == before
    assert not world["journal"].parent.exists()


@pytest.mark.parametrize("damage", ["no_apply_run", "other_plan_in_the_copy", "torn_last_line"])
def test_resume_refuses_a_journal_it_cannot_trust_and_changes_nothing(world, damage):
    _crash(world, "verified")  # the row stands in both files: a resume that trusted this journal would delete
    path, plan_id = world["journal"], world["plan"]["plan_id"]
    if damage == "no_apply_run":
        path.write_bytes(b"".join(x + b"\n" for x in path.read_bytes().splitlines() if b'"move":"*"' not in x))
    elif damage == "other_plan_in_the_copy":  # a valid plan filed under another plan id
        plan_id = "0123456789abcdef"
        (path.parent.parent / plan_id).mkdir()
        (path.parent / "plan.json").rename(path.parent.parent / plan_id / "plan.json")
    else:  # a crash in the middle of a journal write: the last line has no end
        with open(path, "ab") as fh:
            fh.write(b'{"move":"success_patterns:1","sta')
    before, journal = _snapshot(world), path.read_bytes()
    procs = [_resume(world, plan_id)] + ([_apply(world)] if damage == "torn_last_line" else [])
    for proc in procs:  # every move of the journal refused: exit 2, nothing ran
        assert proc.returncode == 2 and proc.stdout == "", proc.stderr
        assert damage != "torn_last_line" or "is not JSON" in proc.stderr
    assert _snapshot(world) == before and path.read_bytes() == journal


def _content_back(world) -> dict:
    """The one source row of the plan's content, under the source project."""
    [back] = [r for r in _rows(world["src"], "success_patterns") if r["title"] == TITLE]
    assert back["project_id"] == B and qtp.content_key(back) == world["plan"]["planned_moves"][0]["content_key"]
    return back


def test_apply_then_undo_gives_the_source_its_row_back_and_leaves_no_copy(world):
    before = _snapshot(world)
    assert _apply(world).returncode == 0
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"], proc.stdout + proc.stderr
    assert _content_back(world)["id"] == world["row"] and _snapshot(world) == before  # every row as before
    lines = _journal(world)
    assert [x["state"] for x in lines][-2:] == ["restored", "undone"] and lines[-2]["restored_id"] == world["row"]
    proc = _apply(world)  # this plan never moves an undone move again
    assert proc.returncode == 1 and _outcomes(proc) == ["undo_started"] and _snapshot(world) == before
    proc = _undo(world)  # a second undo writes nothing
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _snapshot(world) == before
    assert _journal(world) == lines


@pytest.mark.parametrize("run,state,after,outcome,pre", [
    (_apply, "planned", True, "not_copied", False),  # no copied line: nothing to put back or drop
    (_apply, "copied", True, "undone", False),  # the copy never committed
    (_apply, "copied", True, "undone", True),  # same, onto content the destination held: it stays
    (_apply, "verified", True, "undone", False),  # the row in both files
    (_apply, "source_removed", False, "undone", False),  # the source deleted, its journal line never written
    (_apply, "source_removed", True, "undone", False),
    (_undo, "restored", False, "undone", False),  # the source put back, never committed, nothing journaled
    (_undo, "restored", True, "undone", False),  # journaled, the source COMMIT never ran
    (_undo, "undone", False, "undone", False),  # both files committed, the undone line never written
])
def test_an_undo_after_a_crash_in_each_journal_state_restores_every_row(world, run, state, after, outcome, pre):
    if pre:
        _preexisting(world)
    before = _snapshot(world)
    if run is _undo:
        assert _apply(world).returncode == 0
    _crash(world, state, after, run)
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == [outcome], proc.stdout + proc.stderr
    assert _snapshot(world) == before and _content_back(world)["id"] == world["row"]


def test_undo_puts_the_source_back_under_a_new_id_when_the_old_one_is_taken(world):
    assert _apply(world).returncode == 0
    _sql(world["src"], "INSERT INTO success_patterns (id, pattern_type, category, title, description, pattern_data, "
         "project_id) VALUES (?, 'x', 'x', 'other', 'x', '{}', ?)", world["row"], B)
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"], proc.stdout + proc.stderr
    back = _content_back(world)
    assert back["id"] != world["row"] and [x["restored_id"] for x in _journal(world) if x["state"] == "restored"] == [
        back["id"]]
    for t in qtp.JUNCTION_TABLES:
        assert [j["pattern_id"] for j in _rows(world["src"], t)] == [f"intel_sp_{back['id']}"]
    assert all(_rows(world["dest"], t) == [] for t in FIVE)


def test_undo_leaves_content_the_destination_already_held(world):
    _preexisting(world)
    before = _snapshot(world)
    assert _outcomes(_apply(world)) == ["already_present"]
    proc = _undo(world)  # only the junction rows this move created go, the row it found stays
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _snapshot(world) == before


@pytest.mark.parametrize("sql", ["UPDATE success_patterns SET title = 'edited'",
                                 "UPDATE pattern_usage SET pattern_title = 'edited'",
                                 "DELETE FROM success_patterns"])  # its junction rows stay behind
def test_a_copy_changed_since_stays_and_the_source_still_gets_its_row_back(world, sql):
    assert _apply(world).returncode == 0
    _sql(world["dest"], sql)
    dest = _snapshot(world)[1]
    for _ in range(2):  # the second undo answers from the journal and writes nothing
        proc = _undo(world)
        assert proc.returncode == 1 and _outcomes(proc) == ["undone_kept_changed_copy"], proc.stdout + proc.stderr
        assert _snapshot(world)[1] == dest and _content_back(world)["id"] == world["row"]
    assert [x["state"] for x in _journal(world)][-2:] == ["restored", "undone_kept_changed_copy"]


_NAMING = {"pattern_usage": {"project_id": "gamma"},  # its key is (pattern_id, project_id)
           "dispatch_pattern_offered": {"dispatch_id": "d-new"}, "pattern_injection_outcome": {"dispatch_id": "d-new"}}


@pytest.mark.parametrize("found", [False, True], ids=["created", "already_present"])
@pytest.mark.parametrize("table", qtp.JUNCTION_TABLES)
def test_undo_never_deletes_a_row_a_junction_row_this_move_did_not_create_still_names(world, table, found):
    if found:
        _preexisting(world)
    was = _snapshot(world)
    assert _apply(world).returncode == 0  # codex b8ffc37a: then a new dispatch is offered the copied pattern
    [mine] = _rows(world["dest"], table)
    row = {**{k: v for k, v in mine.items() if k != "id"}, **_NAMING[table]}
    _sql(world["dest"], f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", *row.values())
    dest, outcome = _snapshot(world)[1], "undone" if found else "undone_kept_changed_copy"
    proc = _undo(world)  # created: the copy stays whole; found: the row stays, only this move's junction row goes
    assert proc.returncode == int(not found) and _outcomes(proc) == [outcome], proc.stdout + proc.stderr
    assert _snapshot(world) == [was[0], {**was[1], table: [r for r in dest[table] if r != mine]} if found else dest]
    assert _content_back(world)["id"] == world["row"]


def test_undo_drops_a_copy_from_a_destination_without_a_junction_table_the_move_had_no_rows_for(world):
    _sql(world["src"], "DELETE FROM pattern_usage")  # the move journals no pattern_usage row: none to copy, none named
    _sql(world["dest"], "DROP TABLE pattern_usage")
    assert _apply(world).returncode == 0 and _rows(world["dest"], "success_patterns")
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"], proc.stdout + proc.stderr
    assert _rows(world["dest"], "success_patterns") == [] and _content_back(world)["id"] == world["row"]


def test_a_source_that_changed_the_row_since_keeps_it_and_so_does_the_copy(world):
    _crash(world, "verified")
    _sql(world["src"], "UPDATE success_patterns SET description = 'edited'")
    assert _outcomes(_resume(world)) == ["drift"]  # step 3 ran: that it deleted nothing, no journal line proves
    before = _snapshot(world)
    proc = _undo(world)  # the source row is not the pre-image: the copy is the only row of the journaled content
    assert proc.returncode == 1 and _outcomes(proc) == ["source_not_restored"] and _snapshot(world) == before
    states = [x["state"] for x in _journal(world)]
    assert states[-1] == "source_not_restored" and "restored" not in states
    proc = _apply(world)  # an undo outcome like the others: this plan never moves the move again
    assert proc.returncode == 1 and _outcomes(proc) == ["undo_started"] and _snapshot(world) == before


_VANISH = """import sqlite3, sys
sys.path.insert(0, {lib!r})
import qi_transfer_apply as qa
real = qa._restore
def restore(mv, lines, pre):  # another writer takes the row out of the source between the two undo steps
    rid = real(mv, lines, pre)
    sqlite3.connect({src!r}, isolation_level=None).execute("DELETE FROM success_patterns")
    return rid
qa._restore = restore
sys.exit(qa.main(sys.argv[1:]))
"""


def test_a_row_that_leaves_the_source_after_undo_step_1_keeps_the_copy_until_the_retry(world):
    before = _snapshot(world)
    assert _apply(world).returncode == 0
    dest = _snapshot(world)[1]
    proc = _undo(world, code=_VANISH.format(lib=str(LIB), src=str(world["src"])))
    assert proc.returncode == 1 and _outcomes(proc) == ["source_missing"] and _snapshot(world)[1] == dest
    assert _rows(world["src"], "success_patterns") == [] and _journal(world)[-1]["state"] == "source_missing"
    proc = _undo(world)  # the restore line names a row that is gone: put back again, then the copy goes
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _snapshot(world) == before


def _on_sid(world, project, **edit) -> None:
    """A source row on the move's old id: the pre-image row under ``project`` with ``edit`` applied."""
    row = {**world["plan"]["planned_moves"][0]["pre_image"]["row"], "project_id": project, **edit}
    _sql(world["src"], f"INSERT INTO success_patterns ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
         *row.values())


def _held(world) -> list:
    """Per file (source, destination): the ids of the rows that hold the plan's content."""
    key = world["plan"]["planned_moves"][0]["content_key"]
    return [[r["id"] for r in _rows(world[db], "success_patterns") if qtp.content_key(r) == key]
            for db in ("src", "dest")]


@pytest.mark.parametrize("run,state,after,project,outcome", [
    (_undo, "restored", True, B, "undone"),  # codex: the restore journaled, its source COMMIT never ran
    (_undo, "restored", True, "gamma", "undone"),
    (_apply, "source_removed", False, B, "source_not_restored"),  # step 3 committed, its journal line never written
    (_undo, "undo_started", True, B, "undone"),  # the last line: restore still reads done, so the row is not its own
], ids=["restore_uncommitted", "restore_uncommitted_other_project", "step_3_committed_unjournaled",
        "undo_started_last"])
def test_an_unrelated_row_on_the_old_id_after_a_crash_is_never_taken_for_the_restored_row(
        world, run, state, after, project, outcome):
    if run is _undo:
        assert _apply(world).returncode == 0 and _held(world)[0] == [] and _held(world)[1]
    _crash(world, state, after, run)
    assert _held(world)[0] == [] and len(_held(world)[1]) == 1  # the copy is the only row of the content
    _on_sid(world, project, title="other")
    before = _snapshot(world)
    proc = _undo(world)
    assert proc.returncode == int(outcome != "undone") and _outcomes(proc) == [outcome], proc.stdout + proc.stderr
    assert any(_held(world)), "the journaled content is in neither file"
    assert [r["title"] for r in _rows(world["src"], "success_patterns") if r["id"] == world["row"]] == ["other"]
    if outcome != "undone":  # the source row on that id may be what step 3 left: undo keeps the copy, changes nothing
        assert _snapshot(world) == before and _journal(world)[-1]["state"] == outcome
        return
    back = _content_back(world)  # the pre-image under a new id, then the copy goes
    assert back["id"] != world["row"] and _held(world) == [[back["id"]], []]
    restored = [x["restored_id"] for x in _journal(world) if x["state"] == "restored"]
    assert restored == [world["row"]] * (state == "restored") + [back["id"]]
    for t in qtp.JUNCTION_TABLES:
        assert [j["pattern_id"] for j in _rows(world["src"], t)] == [f"intel_sp_{back['id']}"]
    assert all(_rows(world["dest"], t) == [] for t in FIVE)


_FORCED = """import sys
sys.path.insert(0, {lib!r})
import qi_transfer_apply as qa
qa._restore = lambda mv, lines, pre: mv.m["source_id"]  # undo step 1 answers the old id, whatever it holds
sys.exit(qa.main(sys.argv[1:]))
"""


@pytest.mark.parametrize("project,edit", [(B, {"title": "other"}), (A, {})], ids=["other_content", "other_project"])
def test_undo_step_2_deletes_nothing_while_the_source_row_on_its_id_is_not_the_pre_image(world, project, edit):
    assert _apply(world).returncode == 0
    _on_sid(world, project, **edit)
    before = _snapshot(world)  # every row of both files, column by column: count and content_key included
    proc = _undo(world, code=_FORCED.format(lib=str(LIB)))
    assert proc.returncode == 1 and _outcomes(proc) == ["source_not_restored"], proc.stdout + proc.stderr
    assert _snapshot(world) == before and _journal(world)[-1]["state"] == "source_not_restored"


_KILL_AFTER_RESTORE = """import os, signal, sys
sys.path.insert(0, {lib!r})
import qi_transfer_apply as qa
real = qa._restore
def restore(mv, lines, pre):  # the restore committed, then a crash before undo step 2
    real(mv, lines, pre)
    os.kill(os.getpid(), signal.SIGKILL)
qa._restore = restore
sys.exit(qa.main(sys.argv[1:]))
"""


@pytest.mark.parametrize("taken", [False, True], ids=["old_id", "new_id"])
def test_a_restore_that_committed_before_a_crash_is_taken_and_nothing_goes_back_twice(world, taken):
    assert _apply(world).returncode == 0
    if taken:
        _on_sid(world, B, title="other")
    assert _undo(world, code=_KILL_AFTER_RESTORE.format(lib=str(LIB))).returncode == -signal.SIGKILL
    [rid] = [x["restored_id"] for x in _journal(world) if x["state"] == "restored"]
    assert (rid == world["row"]) is not taken and _held(world)[0] == [rid] and _held(world)[1]
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"], proc.stdout + proc.stderr
    assert [x["restored_id"] for x in _journal(world) if x["state"] == "restored"] == [rid]
    assert _held(world) == [[rid], []] and all(_rows(world["dest"], t) == [] for t in FIVE)
    for t in qtp.JUNCTION_TABLES:
        assert [j["pattern_id"] for j in _rows(world["src"], t)] == [f"intel_sp_{rid}"]


_JUNCTION_CREATED = [lambda x, v=v: {**x, "junction": [j[:2] + ([] if v == "missing" else [v]) for j in x["junction"]]}
                     for v in ("missing", None, "true", 1)]
_UNDECODABLE = [_pre(junction=lambda j, kv=kv: {**j, "pattern_usage": [{**j["pattern_usage"][0], **kv}]})
                for kv in ({"no_such_column": "x"}, {"pattern_title": ["x"]}, {"pattern_hash": {"__bytes_hex__": "zz"}})]


@pytest.mark.parametrize("line,field,edit", [("copied", f"valid {f}", {f: v}) for f, v in _MALFORMED]
                         + [("copied", "valid junction", e) for e in _JUNCTION_CREATED]
                         + [("planned", f"valid {f}", e) for f, e in _PLANNED]
                         + [("planned", "no value of a column the source has", e) for e in _UNDECODABLE]
                         + [("restored", "valid restored_id", {"restored_id": v}) for v in ("1", True, None, "missing")])
def test_undo_reads_no_journal_field_before_it_passed_and_the_retry_refuses_too(world, line, field, edit):
    assert _apply(world).returncode == 0
    if line == "restored":  # the undo committed both files, its undone line is gone: the retry reads restored
        assert _undo(world).returncode == 0
    _rewrite(world, line, edit, 0 if line == "restored" else 9)
    before, lines = _snapshot(world), _journal(world)
    started = ["undo_started"] * ("column the source has" in field)  # the lines passed, then restore refused a value
    for _ in range(2):  # the retry writes no second undo_started
        proc = _undo(world)
        assert proc.returncode == 1 and field in _outcomes(proc)[0], proc.stdout + proc.stderr
        now = _journal(world)
        assert _snapshot(world) == before and now[:len(lines)] == lines
        assert [x["state"] for x in now[len(lines):]] == started


def _missing(world) -> set:
    """The journaled rows (the row, each junction row by content) neither file holds under a row of its content."""
    strip, pre = lambda r: qtp.content_key({**r, "pattern_id": None}), world["plan"]["planned_moves"][0]["pre_image"]
    out = {"row"} | {(t, strip(j)) for t in qtp.JUNCTION_TABLES for j in pre["junction"][t]}
    for db, ids in zip(("src", "dest"), _held(world)):
        pids = {qtp.pattern_id_for("success_patterns", i) for i in ids}
        out -= {"row"} if pids else set()
        out -= {(t, strip(j)) for t in qtp.JUNCTION_TABLES for j in _rows(world[db], t) if j["pattern_id"] in pids}
    return out


def test_undo_after_verified_puts_back_the_junction_rows_the_source_lost_before_the_copy_goes(world):
    before = _snapshot(world)
    _crash(world, "verified")  # codex 8cf8f73d: then the source loses its pattern_usage row, the copy holds the last
    _sql(world["src"], "DELETE FROM pattern_usage")
    assert _missing(world) == set()  # the copy holds the last pattern_usage row
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _snapshot(world) == before, proc.stdout
    assert [x["state"] for x in _journal(world)][-2:] == ["restored", "undone"]


_CHANGE = {"none": lambda w: None, "parent_deleted": lambda w: _sql(w["src"], "DELETE FROM success_patterns"),
           "junction_deleted": lambda w: _sql(w["src"], "DELETE FROM pattern_usage"),
           "parent_foreign": lambda w: (_sql(w["src"], "DELETE FROM success_patterns"), _on_sid(w, B, title="other"))}


# planned: no copied line, undo returns not_copied and runs no step 1, so neither crash can occur.
# copied, verified: the source holds the whole pre-image, step 1 writes nothing and has no COMMIT to crash before.
_UNDO_CRASHES = [(s, c) for s in ("planned", "copied", "verified", "source_removed", "done", "restored")
                 for c in (False, True) if not (s == "planned" or (s in ("copied", "verified") and not c))]


@pytest.mark.parametrize("change", list(_CHANGE))
@pytest.mark.parametrize(("state", "committed"), _UNDO_CRASHES,
                         ids=[f"{s}-{'after' if c else 'before'}_commit" for s, c in _UNDO_CRASHES])
def test_an_undo_crash_and_a_change_before_the_retry_never_take_the_last_copy_of_a_journaled_row(
        world, state, committed, change):
    before = _snapshot(world)
    _crash(world, "done" if state == "restored" else state)
    if state == "restored":  # an earlier undo journaled restored, its source COMMIT never ran
        _crash(world, "restored", run=_undo)
    proc = _undo(world, code=_KILL_AFTER_RESTORE.format(lib=str(LIB)) if committed else  # killed after step 1, or
                 _CRASH.format(lib=str(LIB), state="restored", after=True))  # with restored journaled, before COMMIT
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    _CHANGE[change](world)
    lost, proc = _missing(world), _undo(world)  # lost: what the change overwrote in the one file that held it
    assert proc.returncode in (0, 1) and _missing(world) <= lost, proc.stdout + proc.stderr  # the undo takes nothing
    if proc.returncode == 0:  # undone: the source holds the complete pre-image, the destination nothing of this move
        assert _missing(world) == set() and _content_back(world) and _snapshot(world)[1] == before[1]
    assert change == "parent_foreign" or (proc.returncode == 0 and _snapshot(world) == before)  # all back, all gone


# the apply crash that leaves ``state`` the move's last journal state with its copy committed; done: an apply that ran
_UNDO_FROM = {"copied": ("verified", False), "verified": ("verified", True), "source_removed": ("source_removed", True)}


def _undo_killed_before_its_outcome(world, state: str) -> None:
    """Apply up to ``state``, then an undo killed after undo step 2's destination COMMIT, before it journals undone."""
    if state in _UNDO_FROM:
        _crash(world, *_UNDO_FROM[state])
    else:
        assert _apply(world).returncode == 0
    assert _journal(world)[-1]["state"] == state
    _crash(world, "undone", after=False, run=_undo)


@pytest.mark.parametrize("state", ["copied", "verified", "source_removed", "done"])  # copied: codex on 85accf48
def test_an_undo_killed_after_its_destination_commit_is_never_moved_back_by_resume(world, state):
    before = _snapshot(world)
    _undo_killed_before_its_outcome(world, state)
    lines = _journal(world)
    proc = _resume(world)  # the old code copied again and removed the restored source row: done
    assert proc.returncode == 1 and _outcomes(proc) == ["undo_started"], proc.stdout + proc.stderr
    assert _snapshot(world) == before and _missing(world) == set()  # the source holds the pre-image, no copy is left
    assert _journal(world) == lines and [x["state"] for x in lines].count("undo_started") == 1


@pytest.mark.parametrize("state", ["copied", "verified", "source_removed", "done"])
def test_a_second_undo_after_that_crash_finishes_and_a_third_answers_from_the_journal(world, state):
    before = _snapshot(world)
    _undo_killed_before_its_outcome(world, state)
    proc = _undo(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _snapshot(world) == before, proc.stdout
    lines = _journal(world)
    assert [x["state"] for x in lines].count("undo_started") == 1 and lines[-1]["state"] == "undone"
    proc = _undo(world)  # a finished undo writes nothing
    assert proc.returncode == 0 and _outcomes(proc) == ["undone"] and _journal(world) == lines
