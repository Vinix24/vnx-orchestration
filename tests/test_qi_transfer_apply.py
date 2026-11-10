#!/usr/bin/env python3
"""Tests for qi_transfer_apply.py (reattr L3b): apply and resume of a transfer plan.

Every database, receipt store, plan and journal lives under pytest's tmp_path.
The script only runs as a subprocess, so on a tree without it a test fails on
an assertion, never on an ImportError.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(REPO / "tests"))

import pattern_reattribution as pr  # noqa: E402
import qi_transfer_plan as qtp  # noqa: E402
from test_pattern_reattribution import _db_in, _insert_success_pattern, _offered_to, _receipt  # noqa: E402
from test_qi_transfer_plan import _junction, _sql  # noqa: E402

FIVE = ("success_patterns", "antipatterns") + qtp.JUNCTION_TABLES
A, B = "alpha", "beta"  # the row belongs to A and sits in B's own file


def _rows(db: Path, table: str):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


def _snapshot(world):
    return [{t: _rows(db, t) for t in FIVE + ("pattern_activation",)} for db in (world["src"], world["dest"])]


@pytest.fixture
def world(tmp_path: Path):
    """d-1: receipt in B's store, body and report path name A, so A wins; the row cites d-1."""
    root = tmp_path / "vnx-data"
    src, dest = _db_in(tmp_path, B), _db_in(tmp_path, A)
    _receipt(root, B, "d-1", project_id=A, report_path=f"/u/.vnx-data/{A}/unified_reports/d-1.md")
    row = _insert_success_pattern(src, project_id=B, source_dispatch_ids=["d-1"], title="misfiled")
    _junction(src, qtp.pattern_id_for("success_patterns", row), B)
    for db in (src, dest):
        _sql(db, "CREATE TABLE pattern_activation (pattern_id TEXT, state TEXT, blob BLOB)")
        _sql(db, "INSERT INTO pattern_activation VALUES (?, 'active', ?)", f"intel_sp_{row}", b"\x00\xff")
    world = {"root": root, "src": src, "dest": dest, "row": row, "plan_path": tmp_path / "plans" / "plan.json"}
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS")}
    proc = subprocess.run([sys.executable, str(LIB / "qi_transfer_plan.py"), "--db", str(src), "--vnx-data-root",
                           str(root), "--json", "--out", str(world["plan_path"])], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    world["plan"] = json.loads(world["plan_path"].read_text())
    assert world["plan"]["moves"] == 1
    return world


def _cli(world, *args: str, code: str = ""):
    head = ["-c", code] if code else [str(LIB / "qi_transfer_apply.py")]
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS")}
    return subprocess.run([sys.executable, *head, *args, "--vnx-data-root", str(world["root"])],
                          cwd=REPO, capture_output=True, text=True, env=env)


def _apply(world, code: str = ""):
    return _cli(world, "apply", "--plan", str(world["plan_path"]), "--reason", "test transfer", code=code)


def _outcomes(proc) -> list:
    assert proc.stdout, proc.stderr
    return sorted(json.loads(proc.stdout)["outcomes"].values())


def _preexisting(world) -> int:
    """The destination already holds the row's content under A (another id, no junction rows)."""
    keep = _insert_success_pattern(world["dest"], project_id=A, source_dispatch_ids=["d-1"], title="misfiled")
    first_seen = world["plan"]["planned_moves"][0]["pre_image"]["row"]["first_seen"]
    _sql(world["dest"], "UPDATE success_patterns SET first_seen = ? WHERE id = ?", first_seen, keep)
    return keep


def _journal(world) -> list:
    path = qtp.journal_path(world["src"], world["plan"]["plan_id"])
    return [json.loads(line) for line in path.read_text().splitlines() if json.loads(line)["move"] != "*"]


def test_apply_moves_the_row_and_its_junction_rows(world):
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["done"], proc.stderr
    assert all(_rows(world["src"], t) == [] for t in FIVE)
    [copy] = _rows(world["dest"], "success_patterns")
    assert copy["project_id"] == A and copy["id"] and qtp.content_key(copy) == world["plan"]["planned_moves"][0]["content_key"]
    for t in qtp.JUNCTION_TABLES:
        assert [(j["pattern_id"], j["project_id"]) for j in _rows(world["dest"], t)] == [(f"intel_sp_{copy['id']}", A)]
    lines = _journal(world)
    assert [x["state"] for x in lines] == ["planned", "copied", "verified", "source_removed", "done"]
    assert lines[0]["pre_image"]["row"]["id"] == world["row"] and all(x["ts"] for x in lines)
    assert lines[1]["created"] is True and [j[2] for j in lines[1]["junction"]] == [True, True, True]


_CRASH = """
import os, signal, sys
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


@pytest.mark.parametrize("state,after,in_dest,in_src,pre", [
    ("copied", True, 0, 1, False),  # journaled, the destination transaction never committed
    ("copied", True, 1, 1, True),  # same, onto content the destination held: its junction rows rolled back
    ("verified", True, 1, 1, False),  # between the two transactions: the row is in both files
    ("source_removed", False, 1, 0, False),  # source deleted, its journal line never written
])
def test_crash_loses_no_row_and_resume_finishes(world, state, after, in_dest, in_src, pre):
    if pre:
        _preexisting(world)
    proc = _apply(world, code=_CRASH.format(lib=str(LIB), state=state, after=after))
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert (len(_rows(world["dest"], "success_patterns")), len(_rows(world["src"], "success_patterns"))) == (in_dest, in_src)
    proc = _cli(world, "resume", "--plan-id", world["plan"]["plan_id"])
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present" if pre else "done"], proc.stderr
    assert all(_rows(world["src"], t) == [] for t in FIVE)
    assert all(len(_rows(world["dest"], t)) == 1 for t in FIVE if t != "antipatterns")


def test_source_changed_after_the_plan_is_drift_and_changes_nothing(world):
    _sql(world["src"], "UPDATE success_patterns SET description = 'edited' WHERE id = ?", world["row"])
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 1 and _outcomes(proc) == ["drift"]
    assert _snapshot(world) == before


def test_changed_evidence_is_skipped_and_changes_nothing(world):
    _receipt(world["root"], B, "d-1", project_id=B)  # body B in B's store: the vote is now a tie
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 1 and _outcomes(proc) == ["evidence_changed"]
    assert _snapshot(world) == before


def test_second_apply_is_already_present_and_writes_nothing(world):
    assert _apply(world).returncode == 0
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"]
    assert _snapshot(world) == before


def test_content_the_destination_holds_is_not_inserted_again(world):
    keep = _preexisting(world)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"]
    assert [r["id"] for r in _rows(world["dest"], "success_patterns")] == [keep]
    copied = [x for x in _journal(world) if x["state"] == "copied"]
    assert [(x["dest_id"], x["created"]) for x in copied] == [(keep, False)]


def test_a_junction_key_held_with_other_content_is_refused_and_rolled_back(world):
    _sql(world["dest"], "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, project_id) "
         "VALUES ('intel_sp_1', 'other', 'other', ?)", A)  # the id the copy would get
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 1 and _outcomes(proc)[0].startswith("refused: UNIQUE constraint failed")
    assert _snapshot(world) == before


def test_misroute_replay(world):
    """Body and report path name A, the store is B, the row sits in B's file."""
    index = pr.build_attribution_index(world["root"])
    with pytest.raises(pr.CrossDatabaseMove):  # the relabel tool reaches no consumer of A
        pr.apply_decisions(world["src"], pr.scan_database(world["src"], index))
    assert _offered_to(world["dest"], A) == []
    proc = _apply(world)
    assert [i.title for i in _offered_to(world["dest"], A)] == ["misfiled"], proc.stderr
    assert _offered_to(world["src"], B) == []
    assert proc.returncode == 0


def test_a_table_outside_the_five_is_unchanged(world):
    before = [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])]
    assert _apply(world).returncode == 0
    assert [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])] == before


@pytest.mark.parametrize("edit", ["plan_id", "pre_image", "table", "plan_version"])
def test_a_tampered_plan_is_refused_before_any_write(world, edit):
    plan, move, plan_id = world["plan"], world["plan"]["planned_moves"][0], world["plan"]["plan_id"]
    if edit == "pre_image":
        move["pre_image"]["row"]["title"] = "other"
    elif edit == "table":
        move["table"] = "sqlite_master"
    else:
        plan[edit] = None
    world["plan_path"].write_text(json.dumps(plan))
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == 2 and proc.stdout == ""
    assert _snapshot(world) == before
    assert not qtp.journal_path(world["src"], plan_id).parent.exists()


@pytest.mark.parametrize("args", [["resume", "--plan-id", "0123456789abcdef"], ["resume", "--plan-id", "../x"],
                                  ["apply", "--plan", "missing.json", "--reason", "r"],
                                  ["apply", "--plan", "plan.json", "--reason", " "]])
def test_missing_plan_or_reason_exits_2(world, args):
    proc = _cli(world, *args)
    assert proc.returncode == 2 and proc.stdout == ""


@pytest.mark.parametrize("damage", ["no_apply_run", "other_plan_in_the_copy"])
def test_resume_refuses_a_journal_it_cannot_trust(world, damage):
    assert _apply(world).returncode == 0
    path = qtp.journal_path(world["src"], world["plan"]["plan_id"])
    if damage == "no_apply_run":
        path.write_text("".join(line + "\n" for line in path.read_text().splitlines() if '"move":"*"' not in line))
    else:  # a valid plan filed under another plan id
        (path.parent.parent / "0123456789abcdef").mkdir()
        (path.parent / "plan.json").rename(path.parent.parent / "0123456789abcdef" / "plan.json")
    plan_id = "0123456789abcdef" if damage == "other_plan_in_the_copy" else world["plan"]["plan_id"]
    proc = _cli(world, "resume", "--plan-id", plan_id)
    assert proc.returncode == 2 and proc.stdout == ""
