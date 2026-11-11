#!/usr/bin/env python3
"""Tests for qi_transfer_apply.py (reattr L3b, split 2 of 3): apply and resume move the row, the source last.

Every database, receipt store, plan and journal lives under tmp_path. The script only runs as a
subprocess, so on a tree without it a test fails on an assertion, never on an ImportError.
"""
from __future__ import annotations

import json
import os
import signal
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


def _resume(world, plan_id=None):
    return _script("qi_transfer_apply.py", "resume", "--plan-id", plan_id or world["plan"]["plan_id"],
                   "--vnx-data-root", world["root"])


def _crash(world, state: str, after: bool = True) -> None:
    """An apply killed with SIGKILL right after (or right before) it journals ``state``."""
    proc = _apply(world, code=_CRASH.format(lib=str(LIB), state=state, after=after))
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
    for retry in (_apply, _resume)[:1 if logged else 2]:  # a run that journals nothing answers the same again
        proc = retry(world)
        assert proc.returncode == int(outcome not in ("done", "already_present")), proc.stdout + proc.stderr
        assert outcome in _outcomes(proc)[0] and _snapshot(world) == before
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
], ids=["source_row_changed", "source_junction_changed", "copy_changed", "planned_line_malformed", "not_verified"])
def test_step_3_deletes_nothing_unless_the_source_is_as_journaled_and_the_copy_verifies(
        world, after, where, damage, outcome):
    _crash(world, "verified", after)  # the row stands in both files
    if where == "planned":
        _rewrite(world, where, damage, 2)
    else:
        _sql(world[where], damage)
    before = _snapshot(world)
    proc = _resume(world)
    assert proc.returncode == 1 and outcome in _outcomes(proc)[0] and _snapshot(world) == before, proc.stdout
    assert "source_removed" not in [x["state"] for x in _journal(world)]
    kept = ["step 3 deleted nothing" in n for n in json.loads(proc.stdout)["notes"].values()]
    assert kept == [True] * (outcome in ("drift", "destination_missing"))


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
