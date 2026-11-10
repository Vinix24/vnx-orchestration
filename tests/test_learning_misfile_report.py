#!/usr/bin/env python3
"""Tests for learning_misfile_report.py (reattr L6). Every store lives under tmp_path."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import qi_transfer_plan as qtp  # noqa: E402
from pattern_reattribution import build_attribution_index  # noqa: E402
from test_pattern_reattribution import (  # noqa: E402
    _db_in, _insert_success_pattern, _write_receipt_store,
)

SCRIPT = "scripts/learning_misfile_report.py"


def _cli(*args: str):
    env = {k: v for k, v in os.environ.items() if k != "VNX_PROJECT_ID"}
    return subprocess.run([sys.executable, SCRIPT, *args], cwd=REPO, capture_output=True, text=True, env=env)


def _sql(db: Path, sql: str, *args) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*.db"))}


@pytest.fixture
def world(tmp_path: Path):
    """alpha's DB: row 1 is beta's (misrouted, moves), row 2 has no source ids (defaulted)."""
    root = tmp_path / "vnx-data"
    _write_receipt_store(root, "beta", ["d-beta-1"])
    src = _db_in(tmp_path, "alpha")
    dest = _db_in(tmp_path, "beta")
    moved = _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=["d-beta-1"], title="misrouted")
    _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=None, title="defaulted")
    return {"root": root, "src": src, "dest": dest, "moved": moved, "tmp": tmp_path}


def _plan(world) -> dict:
    index = build_attribution_index(world["root"])
    return qtp.build_plan([world["src"]], index)


def _insert_row(move: dict, db_key: str, project_id: str) -> None:
    """The planned row, as a fresh row of ``project_id`` in the database ``move[db_key]``."""
    row = dict(move["pre_image"]["row"])
    row.pop("id")
    row["project_id"] = project_id
    _sql(Path(move[db_key]), f"INSERT INTO {move['table']} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
         *row.values())


def _transfer(world, plan: dict) -> None:
    """What L3b does, done by hand: copy the planned row under the destination project, delete the source."""
    for move in plan["planned_moves"]:
        _insert_row(move, "destination_db", move["destination_project_id"])
        _sql(Path(move["source_db"]), f"DELETE FROM {move['table']} WHERE id = ?", move["source_id"])


def _write_plan(world, plan: dict) -> Path:
    path = world["tmp"] / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _verify(plan_path: Path):
    return _cli("verify", "--plan", str(plan_path), "--json")


def _reasons(proc) -> list:
    return [f["reason"] for f in json.loads(proc.stdout)["failures"]]


# Red run: on origin/main the script does not exist, so this returns 2 and `== 0` fails.
def test_report_all_counts_the_misrouted_and_the_defaulted_row(world):
    proc = _cli("report", "--all", "--json", "--vnx-data-root", str(world["root"]))
    assert proc.returncode == 0, proc.stderr
    counts = json.loads(proc.stdout)["databases"][str(world["src"].resolve())]
    assert counts["moves"] == 1
    assert counts["unattributed"] >= 1
    assert counts["schema_gap"] == 0 and "ambiguous" in counts and "store_only" in counts and "unchanged" in counts




def test_report_changes_no_database_file(world):
    before = _snapshot(world["tmp"])
    assert _cli("report", "--all", "--json", "--vnx-data-root", str(world["root"])).returncode == 0
    assert _snapshot(world["tmp"]) == before


def test_report_names_an_unreadable_database_and_fails(world):
    world["dest"].write_bytes(b"this is not a database" * 50)
    proc = _cli("report", "--all", "--json", "--vnx-data-root", str(world["root"]))
    assert proc.returncode == 1
    assert str(world["dest"].resolve()) in json.loads(proc.stdout)["errors"]




def test_unknown_flag_is_argparse_exit_2(world):
    assert _cli("report", "--all", "--bogus").returncode == 2
    assert _cli("verify", "--plan", "x", "--bogus").returncode == 2


def test_verify_passes_after_the_transfer(world):
    plan = _plan(world)
    _transfer(world, plan)
    proc = _verify(_write_plan(world, plan))
    out = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stdout
    assert (out["present"], out["planned"], out["moved_total"]) == (1, 1, 1)


def test_verify_before_the_transfer_fails_as_missing(world):
    proc = _verify(_write_plan(world, _plan(world)))
    assert proc.returncode == 1
    assert _reasons(proc) == ["missing_in_destination"]


def test_verify_fails_when_the_row_is_still_in_the_source(world):
    plan = _plan(world)
    _transfer(world, plan)
    _insert_row(plan["planned_moves"][0], "source_db", "alpha")
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode == 1
    assert _reasons(proc) == ["still_in_source"]


def test_verify_fails_on_a_duplicate_in_the_destination(world):
    plan = _plan(world)
    _transfer(world, plan)
    _insert_row(plan["planned_moves"][0], "destination_db", "beta")
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode == 1
    assert _reasons(proc) == ["duplicate_in_destination"]


def test_verify_fails_when_the_destination_database_is_unreadable(world):
    plan = _plan(world)
    _transfer(world, plan)
    world["dest"].write_bytes(b"garbage" * 100)
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode == 1
    assert _reasons(proc) == ["destination_unreadable"]




def test_verify_fails_when_the_source_database_is_unreadable(world):
    plan = _plan(world)
    _transfer(world, plan)
    world["src"].write_bytes(b"garbage" * 100)
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode == 1
    assert _reasons(proc) == ["source_unreadable"]


def test_verify_fails_when_the_destination_row_is_expired(world):
    plan = _plan(world)
    _transfer(world, plan)
    # valid_until is part of content_key: expire the destination row and move the plan's image with it.
    _sql(world["dest"], "UPDATE success_patterns SET valid_until = '2020-01-01 00:00:00' WHERE project_id = 'beta'")
    move = plan["planned_moves"][0]
    move["pre_image"]["row"]["valid_until"] = "2020-01-01 00:00:00"
    move["content_key"] = qtp.content_key(move["pre_image"]["row"])
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode == 1
    assert _reasons(proc) == ["not_selectable_by_consumer"]




def test_verify_fails_when_the_destination_lacks_valid_until(world):
    plan = _plan(world)
    _transfer(world, plan)
    _sql(world["dest"], "ALTER TABLE success_patterns DROP COLUMN valid_until")
    assert _reasons(_verify(_write_plan(world, plan))) == ["destination_unreadable"]


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("planned_moves"),
    lambda p: p.update(planned_moves=None),
    lambda p: p.update(planned_moves=[]),
    lambda p: p.update(moves=5),
    lambda p: p["planned_moves"].append("not-an-object"),
    lambda p: p["planned_moves"][0].pop("content_key"),
    lambda p: p["planned_moves"][0].update(content_key=None),
    lambda p: p["planned_moves"][0].update(content_key="0" * 64),
    lambda p: p["planned_moves"][0].update(table="sqlite_master"),
    lambda p: p["planned_moves"][0].update(destination_db=None),
    lambda p: p["planned_moves"][0].update(pre_image=None),
    lambda p: p["planned_moves"][0].update(pre_image={"row": []}),
])
def test_verify_rejects_a_malformed_plan(world, mutate):
    plan = _plan(world)
    _transfer(world, plan)
    mutate(plan)
    proc = _verify(_write_plan(world, plan))
    assert proc.returncode != 0
    assert json.loads(proc.stdout)["ok"] is False


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", "42", '{"planned_moves": 1}'])
def test_verify_rejects_unparseable_or_wrong_shaped_plan_files(world, content):
    path = world["tmp"] / "bad.json"
    path.write_text(content, encoding="utf-8")
    proc = _verify(path)
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["reason"] == "plan_malformed"


def test_verify_rejects_a_missing_plan_file(world):
    assert _verify(world["tmp"] / "no-such-plan.json").returncode == 2


def test_verify_changes_no_database_file(world):
    plan = _plan(world)
    _transfer(world, plan)
    path = _write_plan(world, plan)
    before = _snapshot(world["tmp"])
    assert _verify(path).returncode == 0
    assert _snapshot(world["tmp"]) == before

