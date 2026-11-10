#!/usr/bin/env python3
"""Tests for qi_transfer_plan.py (reattr L3a): the plan of a cross-database move.

Every database and receipt store lives under pytest's tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import qi_transfer_plan as qtp  # noqa: E402
from test_pattern_reattribution import (  # noqa: E402
    _db_in, _insert_success_pattern, _make_db, _write_receipt_store,
)

SCRIPT = REPO / "scripts" / "lib" / "qi_transfer_plan.py"


def _sql(db: Path, sql: str, *args) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _junction(db: Path, pid: str, project_id: str) -> None:
    for sql in (
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, project_id) VALUES (?, 't', 'h', ?)",
        "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
        "VALUES ('d-x', ?, 't', '2026-11-01T00:00:00Z', ?)",
        "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id) VALUES ('d-x', ?, 1, ?)",
    ):
        _sql(db, sql, pid, project_id)


@pytest.fixture
def world(tmp_path: Path):
    """alpha's own DB holding: row 1 (beta's, movable), row 2 (beta's, tags the beta DB lacks), row 3 (ambiguous)."""
    root = tmp_path / "vnx-data"
    _write_receipt_store(root, "beta", ["d-beta-1", "d-beta-2"])
    _write_receipt_store(root, "gamma", ["d-gamma"])  # a winner with no own DB file
    src = _db_in(tmp_path, "alpha")
    dest = _db_in(tmp_path, "beta")
    conn = sqlite3.connect(str(dest))
    conn.execute("ALTER TABLE success_patterns DROP COLUMN tags")
    conn.commit()
    conn.close()
    r1 = _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=["d-beta-1"], title="one")
    r2 = _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=["d-beta-2"], title="two")
    r3 = _insert_success_pattern(src, project_id="alpha", source_dispatch_ids=["d-gamma"], title="three")
    _sql(src, "UPDATE success_patterns SET tags = ? WHERE id = ?", "carries-a-tag", r2)
    _junction(src, qtp.pattern_id_for("success_patterns", r1), "alpha")
    _sql(src, "PRAGMA user_version = 34")
    _sql(dest, "PRAGMA user_version = 28")
    return {"root": root, "src": src, "dest": dest, "r1": r1, "r2": r2, "r3": r3}


def _run(world, *extra: str, script: Path = SCRIPT):
    env = {k: v for k, v in os.environ.items() if k != "VNX_PROJECT_ID"}
    return subprocess.run(
        [sys.executable, str(script.relative_to(REPO)), "--db", str(world["src"]),
         "--vnx-data-root", str(world["root"]), "--json", *extra],
        cwd=REPO, capture_output=True, text=True, env=env,
    )


def _plan(world) -> dict:
    proc = _run(world)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_script_runs_and_counts_moves_and_gaps(world):
    plan = _plan(world)
    assert plan["moves"] == 1
    assert plan["schema_gap"] == 1
    assert plan["planned_moves"][0]["source_id"] == world["r1"]
    assert plan["schema_gap_rows"][0]["columns"] == ["success_patterns.tags"]
    move = plan["planned_moves"][0]
    assert (move["source_user_version"], move["destination_user_version"]) == (34, 28)
    assert plan["user_versions"] == {str(world["src"]): 34}


def test_pre_image_holds_every_column_and_junction_rows(world):
    move = _plan(world)["planned_moves"][0]
    conn = sqlite3.connect(str(world["src"]))
    conn.row_factory = sqlite3.Row
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(success_patterns)")]
    conn.close()
    assert sorted(move["pre_image"]["row"]) == sorted(cols)
    assert set(move["pre_image"]["junction"]) == set(qtp.JUNCTION_TABLES)
    assert all(len(rows) == 1 for rows in move["pre_image"]["junction"].values())
    assert move["pattern_id"] == f"intel_sp_{world['r1']}"


def test_content_key_is_hex64_and_ignores_id_and_project_id(world):
    a = {"id": 1, "project_id": "alpha", "title": "t", "tags": None, "data": "é"}
    b = {"id": 99, "project_id": "beta", "title": "t", "tags": None, "data": "é"}
    key = qtp.content_key(a)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == qtp.content_key(b)
    assert key != qtp.content_key({**b, "title": "other"})
    assert qtp.content_key({"id": 1, "blob": b"\x00\x01"}) != qtp.content_key({"id": 1, "blob": b"\x00\x02"})


    move = _plan(world)["planned_moves"][0]
    assert move["content_key"] == qtp.content_key(move["pre_image"]["row"])


def test_sources_are_untouched(world):
    files = [world["src"], world["dest"]] + list(world["root"].glob("*/state/t0_receipts.ndjson"))
    before = {f: (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest()) for f in files}
    siblings = sorted(p.name for p in world["src"].parent.iterdir())
    assert _run(world).returncode == 0
    after = {f: (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest()) for f in files}
    assert before == after
    assert siblings == sorted(p.name for p in world["src"].parent.iterdir())


def test_same_input_gives_same_plan_id_and_changed_input_does_not(world):
    first, second = _plan(world), _plan(world)
    assert first["plan_id"] == second["plan_id"]
    _insert_success_pattern(world["src"], project_id="alpha", source_dispatch_ids=["d-beta-1"], title="four")
    assert _plan(world)["plan_id"] != first["plan_id"]


def test_ambiguous_row_is_listed_and_not_planned(world):
    plan = _plan(world)
    listed = {r["source_id"]: r["status"] for r in plan["listed_rows"]}
    assert listed[world["r3"]] == "ambiguous"
    planned = {m["source_id"] for m in plan["planned_moves"]} | {m["source_id"] for m in plan["schema_gap_rows"]}
    assert world["r3"] not in planned


def test_journal_path_and_states_are_defined_not_written(world):
    plan = _plan(world)
    move = plan["planned_moves"][0]
    expected = world["src"].resolve().parent / "reattribution" / plan["plan_id"] / "journal.ndjson"
    assert Path(move["journal_path"]) == expected
    assert not expected.parent.exists()
    assert plan["journal_states"] == ["planned", "copied", "verified", "source_removed", "done"]


def test_missing_destination_table_with_data_is_a_schema_gap(world):
    conn = sqlite3.connect(str(world["dest"]))
    conn.execute("DROP TABLE pattern_usage")
    conn.commit()
    conn.close()
    plan = _plan(world)
    assert plan["moves"] == 0
    cols = {c for g in plan["schema_gap_rows"] for c in g["columns"]}
    assert "pattern_usage.pattern_hash" in cols


def test_unreadable_destination_is_unplanned_not_planned(world):
    world["dest"].write_bytes(b"not a sqlite file at all, just bytes")
    plan = _plan(world)
    assert plan["moves"] == 0
    assert plan["unplanned"] >= 1
    assert all(u["reason"].startswith("destination_unreadable") for u in plan["unplanned_rows"])


def test_plan_reads_attribution_and_pre_image_from_one_snapshot(world, monkeypatch):
    _sql(world["src"], "PRAGMA journal_mode=WAL")
    real = qtp.read_pre_image

    def racing(conn, table, row_id):
        _sql(world["src"], "UPDATE success_patterns SET source_dispatch_ids = ? WHERE id = ?", '["d-gamma"]', world["r1"])
        return real(conn, table, row_id)

    monkeypatch.setattr(qtp, "read_pre_image", racing)
    plan = qtp.build_plan([world["src"]], qtp.build_attribution_index(world["root"]))
    move = plan["planned_moves"][0]
    assert move["source_user_version"] == 34 and move["destination_project_id"] == "beta"
    assert json.loads(move["pre_image"]["row"]["source_dispatch_ids"]) == ["d-beta-1"]


def test_blob_column_survives_the_cli_output(world):
    _sql(world["src"], "UPDATE success_patterns SET pattern_data = ? WHERE id = ?", b"\x00\xff", world["r1"])
    move = _plan(world)["planned_moves"][0]
    assert move["pre_image"]["row"]["pattern_data"] == {"__bytes_hex__": "00ff"}
