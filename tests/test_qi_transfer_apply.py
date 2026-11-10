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
    row = _insert_success_pattern(src, project_id=B, source_dispatch_ids=["d-1"], title="misfiled")
    _junction(src, qtp.pattern_id_for("success_patterns", row), B)
    for db in (src, dest):
        _sql(db, "CREATE TABLE pattern_activation (pattern_id TEXT, state TEXT, blob BLOB)")
        _sql(db, "INSERT INTO pattern_activation VALUES (?, 'active', ?)", f"intel_sp_{row}", b"\x00\xff")
    proc = _script("qi_transfer_plan.py", "--db", src, "--vnx-data-root", root, "--json", "--out", plan_path)
    assert proc.returncode == 0, proc.stderr
    assert (plan := json.loads(plan_path.read_text()))["moves"] == 1
    return {"root": root, "src": src, "dest": dest, "row": row, "plan_path": plan_path, "plan": plan}


def _apply(world, plan=None, reason="test transfer"):
    return _script("qi_transfer_apply.py", "apply", "--plan", plan or world["plan_path"], "--reason", reason,
                   "--vnx-data-root", world["root"])


def _outcomes(proc) -> list:
    assert proc.stdout, proc.stderr
    return sorted(json.loads(proc.stdout)["outcomes"].values())


def _journal(world) -> list:
    path = qtp.journal_path(world["src"], world["plan"]["plan_id"])
    return [json.loads(line) for line in path.read_text().splitlines() if json.loads(line)["move"] != "*"]


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


def test_a_second_apply_is_already_present_and_writes_nothing(world):
    assert _apply(world).returncode == 0
    before, lines = _snapshot(world), _journal(world)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"] and _snapshot(world) == before
    assert _journal(world) == lines  # a no-op on verified: not one move line


def test_content_the_destination_holds_is_not_inserted_again(world):
    keep = _insert_success_pattern(world["dest"], project_id=A, source_dispatch_ids=["d-1"], title="misfiled")
    first_seen = world["plan"]["planned_moves"][0]["pre_image"]["row"]["first_seen"]
    _sql(world["dest"], "UPDATE success_patterns SET first_seen = ? WHERE id = ?", first_seen, keep)
    proc = _apply(world)
    assert proc.returncode == 0 and _outcomes(proc) == ["already_present"]
    assert [r["id"] for r in _rows(world["dest"], "success_patterns")] == [keep]
    assert [(x["dest_id"], x["created"]) for x in _journal(world) if x["state"] == "copied"] == [(keep, False)]


@pytest.mark.parametrize("edit,damage,outcome", [
    ({}, "", "verified"), ({}, "UPDATE success_patterns SET title = 'edited'", "verify_failed"),
    ({}, "UPDATE pattern_usage SET pattern_title = 'edited'", "verify_failed"),
    ({"state": "source_removed"}, "", "refused"), ({"state": "done"}, "", "refused"), ({"created": 1}, "", "refused"),
    ({"created": None}, "", "refused"), ({"created": "true"}, "", "refused"), ({"created": "missing"}, "", "refused"),
    ({"state": "verified"}, "UPDATE success_patterns SET project_id = 'gamma'", "destination_missing"),
])
def test_a_second_apply_finishes_from_the_journal_and_refuses_what_it_cannot_prove(world, edit, damage, outcome):
    assert _apply(world).returncode == 0
    path = qtp.journal_path(world["src"], world["plan"]["plan_id"])
    raw = path.read_text().splitlines()
    line = {k: v for k, v in {**json.loads(raw[-2]), **edit}.items() if v != "missing"}  # the copied line
    path.write_text("".join(f"{x}\n" for x in raw[:-1] + [json.dumps(line)]))  # in place of verified
    if damage:
        _sql(world["dest"], damage)
    before = _snapshot(world)
    proc = _apply(world)
    assert proc.returncode == int(outcome != "verified") and _outcomes(proc)[0].startswith(outcome), proc.stdout
    assert _snapshot(world) == before and (outcome in ("refused", "destination_missing")
                                           or _journal(world)[-1]["state"] == outcome)


def test_a_table_outside_the_five_is_unchanged(world):
    before = [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])]
    assert _apply(world).returncode == 0
    assert [_rows(db, "pattern_activation") for db in (world["src"], world["dest"])] == before


@pytest.mark.parametrize("edit", ["plan_id", "pre_image", "table", "plan_version", "missing_plan", "blank_reason"])
def test_a_tampered_or_missing_plan_or_a_blank_reason_exits_2_before_any_write(world, edit):
    plan, move, plan_id = world["plan"], world["plan"]["planned_moves"][0], world["plan"]["plan_id"]
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
    assert not qtp.journal_path(world["src"], plan_id).parent.exists()
