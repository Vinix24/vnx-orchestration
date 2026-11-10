#!/usr/bin/env python3
"""Tests for qi_transfer_apply.py (reattr L3b, split 1 of 3): apply copies and verifies, the source stays as it was.

Every database, receipt store, plan and journal lives under tmp_path. The script only runs as a
subprocess, so on a tree without it a test fails on an assertion, never on an ImportError.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import qi_transfer_plan as qtp
from test_pattern_reattribution import _db_in, _insert_success_pattern, _receipt
from test_qi_transfer_plan import _junction, _sql

LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
FIVE = ("success_patterns", "antipatterns") + qtp.JUNCTION_TABLES
A, B = "alpha", "beta"  # the row belongs to A and sits in B's own file
TITLE = "mis\u2028filed"  # U+2028 ends a line for str.splitlines, not for the journal


def _rows(db: Path, table: str):
    with closing(qtp.open_readonly(db)) as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _snapshot(world):
    return [{t: _rows(db, t) for t in FIVE + ("pattern_activation",)} for db in (world["src"], world["dest"])]


def _script(name: str, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS")}
    return subprocess.run([sys.executable, str(LIB / name), *map(str, args)], capture_output=True, text=True, env=env)


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


def _apply(world, plan=None, reason="test transfer"):
    return _script("qi_transfer_apply.py", "apply", "--plan", plan or world["plan_path"], "--reason", reason,
                   "--vnx-data-root", world["root"])


def _outcomes(proc) -> list:
    assert proc.stdout, proc.stderr
    return sorted(json.loads(proc.stdout)["outcomes"].values())


def _journal(world) -> list:
    return [json.loads(x) for x in world["journal"].read_bytes().splitlines() if json.loads(x)["move"] != "*"]


def _rewrite(world, edit, keep_verified=False):
    """The copied line edited in place ("missing" drops a key); the verified line after it stays only if kept."""
    raw = world["journal"].read_bytes().splitlines()
    line = {k: v for k, v in {**json.loads(raw[-2]), **edit}.items() if v != "missing"}
    raw = raw[:-2] + [json.dumps(line).encode()] + raw[-1:] * keep_verified
    world["journal"].write_bytes(b"".join(x + b"\n" for x in raw))


def test_apply_copies_and_verifies_and_leaves_the_source_byte_equal(world):
    before = hashlib.sha256(world["src"].read_bytes()).hexdigest()
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["verified"], proc.stderr
    assert ["still holds the row" in n and "step 3" in n for n in json.loads(proc.stdout)["notes"].values()] == [True]
    assert hashlib.sha256(world["src"].read_bytes()).hexdigest() == before
    [copy] = _rows(world["dest"], "success_patterns")
    assert copy["project_id"] == A and qtp.content_key(copy) == world["plan"]["planned_moves"][0]["content_key"]
    for t in qtp.JUNCTION_TABLES:
        assert [(j["pattern_id"], j["project_id"]) for j in _rows(world["dest"], t)] == [(f"intel_sp_{copy['id']}", A)]
    lines = _journal(world)
    assert [x["state"] for x in lines] == ["planned", "copied", "verified"]
    assert lines[0]["pre_image"]["row"]["id"] == world["row"] and all(x["ts"] for x in lines)
    assert lines[1]["created"] is True and [j[2] for j in lines[1]["junction"]] == [True, True, True]
    assert _rows(world["dest"], "pattern_activation") == _rows(world["src"], "pattern_activation")  # outside the five


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
    keep = _insert_success_pattern(world["dest"], project_id=A, source_dispatch_ids=["d-1"], title=TITLE)
    first_seen = world["plan"]["planned_moves"][0]["pre_image"]["row"]["first_seen"]
    _sql(world["dest"], "UPDATE success_patterns SET first_seen = ? WHERE id = ?", first_seen, keep)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"]
    assert [r["id"] for r in _rows(world["dest"], "success_patterns")] == [keep]
    assert [(x["dest_id"], x["created"]) for x in _journal(world) if x["state"] == "copied"] == [(keep, False)]


_MALFORMED = [("created", "true"), ("created", 1), ("dest_id", True), ("dest_id", "1"), ("row_key", 7),
              ("pattern_id", "intel_sp_9"), ("cols", ["title"]), ("cols", {"success_patterns": "title"}),
              ("junction", [["pattern_usage", "k"]]), ("junction", [["sqlite_master", "k", True]]),
              ("junction", [["pattern_usage", "k", "true"]])]
_MALFORMED += [(f, v) for f in dict(_MALFORMED) for v in (None, "missing")]  # None and missing for every field


@pytest.mark.parametrize("edit,keep,damage,outcome", [
    ({}, 0, "", "verified"), ({}, 0, "UPDATE success_patterns SET title = 'edited'", "verify_failed"),
    ({}, 0, "UPDATE pattern_usage SET pattern_title = 'edited'", "verify_failed"), ({}, 1, "", "already_present"),
    ({"state": "source_removed"}, 0, "", "refused"), ({"state": "done"}, 0, "", "refused"),
    ({"state": "planned"}, 1, "", "refused"),  # verified, and no copied line before it
    ({}, 1, "UPDATE success_patterns SET project_id = 'gamma'", "destination_missing"),
] + [({f: v}, keep, "", f"valid {f}") for f, v in _MALFORMED for keep in (0, 1)])  # copied path, verified fast path
def test_a_second_apply_finishes_from_the_journal_and_refuses_what_it_cannot_prove(world, edit, keep, damage, outcome):
    assert _apply(world).returncode == 0
    _rewrite(world, edit, keep)
    if damage:
        _sql(world["dest"], damage)
    before, lines, logs = _snapshot(world), _journal(world), outcome in ("verified", "verify_failed")
    for _ in range(1 if logs else 2):  # an apply that journals nothing answers the same on its retry
        proc = _apply(world)
        assert proc.returncode == int(outcome not in ("verified", "already_present")), proc.stdout
        assert outcome in _outcomes(proc)[0] and _snapshot(world) == before
    assert [x["state"] for x in _journal(world)[len(lines):]] == [outcome][:logs]


@pytest.mark.parametrize("edit", ["plan_id", "pre_image", "table", "plan_version", "missing_plan", "blank_reason"])
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
    proc = _apply(world, plan=world["plan_path"].with_name("missing.json") if edit == "missing_plan" else None,
                  reason=" " if edit == "blank_reason" else "test transfer")
    assert proc.returncode == 2 and proc.stdout == "" and _snapshot(world) == before
    assert not world["journal"].parent.exists()
