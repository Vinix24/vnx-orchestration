"""Order between deliverables (ADR-096 §1-2, B1): migration 0039 and `vnx deliverable depend|undepend|deps`."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import planning_cli
import schema_manifest
import schema_migration
from coordination_db import db_path_from_state_dir, init_schema
from fixtures.live_store_fixture import build_live_store
from migrations import auto_apply as auto_apply_mod

LIVE_DB = Path.home() / ".vnx-data" / "vnx-dev" / "state" / "runtime_coordination.db"
MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
PID = "test-proj"


def _counts(conn: sqlite3.Connection) -> dict:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _store_at_38(tmp_path: Path) -> tuple[Path, str]:
    """A read-only backup of the live store, or (CI: no live file) a store built from the migrations."""
    state_dir = tmp_path / ".vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    db = db_path_from_state_dir(state_dir)
    if LIVE_DB.is_file():
        src = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(str(db))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        return db, "live-copy"
    upto_38 = tmp_path / "upto-38"
    upto_38.mkdir()
    for number, sql_path in auto_apply_mod._discover_migrations(MIGRATIONS):
        if number <= 38:
            shutil.copyfile(sql_path, upto_38 / sql_path.name)
    init_schema(state_dir)
    auto_apply_mod.auto_apply(db, migrations_dir=upto_38)
    return db, "built-from-migrations"


def test_migration_0039_on_a_copy_of_the_live_schema(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(schema_migration, "_PREFLIGHT_HOOKS", {})
    db, source = _store_at_38(tmp_path)
    conn = sqlite3.connect(str(db))
    before, version = _counts(conn), schema_migration.get_user_version(conn)
    conn.close()
    print(f"migration 0039 source store: {source} at user_version {version}")
    assert auto_apply_mod.auto_apply(db) == ([39] if version < 39 else [])
    conn = sqlite3.connect(str(db))
    try:
        assert schema_migration.get_user_version(conn) >= 39
        table = schema_manifest.table_at(39, "deliverable_dependencies")
        assert schema_manifest.validate_table(conn, table) == []
        assert [r[1] for r in sorted(conn.execute("PRAGMA table_info(deliverable_dependencies)"),
                                     key=lambda r: r[5]) if r[5]] == ["project_id", "deliverable_id", "depends_on_id"]
        after = _counts(conn)
    finally:
        conn.close()
    assert {k: v for k, v in after.items() if k in before} == before
    assert set(after) - set(before) <= {"deliverable_dependencies"}


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    db = build_live_store(state_dir)
    conn = sqlite3.connect(str(db))
    for pid in (PID, "other-proj"):
        for dlv in ("dlv-a", "dlv-b", "dlv-c"):
            conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, metadata_json) VALUES (?, ?, "
                         "'proposed', ?)", (dlv, pid, json.dumps({"title": dlv, "deliverable": True})))
    conn.commit()
    conn.close()
    return state_dir


def _cli(store: Path, *argv: str, pid: str = PID) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main(["deliverable", *argv, "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _deps(store: Path, dlv: str, pid: str = PID) -> dict:
    rc, out, err = _cli(store, "deps", dlv, "--json", pid=pid)
    assert rc == 0, err
    return json.loads(out)


def test_depend_records_a_hard_edge_visible_in_both_directions(store):
    assert _cli(store, "depend", "dlv-b", "--on", "dlv-a")[0] == 0
    assert _cli(store, "depend", "dlv-c", "--on", "dlv-b", "--kind", "soft")[0] == 0
    b = _deps(store, "dlv-b")
    assert set(b) == {"deliverable", "project_id", "depends_on", "depended_on_by"}
    [up], [down] = b["depends_on"], b["depended_on_by"]
    assert (up["depends_on_id"], up["kind"], up["active"], up["removed_reason"]) == ("dlv-a", "hard", True, None)
    assert (down["deliverable_id"], down["kind"]) == ("dlv-c", "soft")
    assert up["created_at"] and up["created_by"]


@pytest.mark.parametrize("edges, new, cycle", [
    ([], ("dlv-a", "dlv-a"), "dlv-a -> dlv-a"),
    ([("dlv-b", "dlv-a")], ("dlv-a", "dlv-b"), "dlv-a -> dlv-b -> dlv-a"),
    ([("dlv-b", "dlv-a"), ("dlv-c", "dlv-b")], ("dlv-a", "dlv-c"), "dlv-a -> dlv-c -> dlv-b -> dlv-a"),
])
def test_a_cycle_is_refused_and_named(store, edges, new, cycle):
    for dlv, on in edges:
        assert _cli(store, "depend", dlv, "--on", on, "--kind", "soft")[0] == 0
    rc, _, err = _cli(store, "depend", new[0], "--on", new[1])
    assert rc == 2 and cycle in err
    assert _deps(store, new[0])["depends_on"] == []


def test_a_removed_edge_closes_no_cycle(store):
    _cli(store, "depend", "dlv-b", "--on", "dlv-a")
    assert _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "merged together")[0] == 0
    assert _cli(store, "depend", "dlv-a", "--on", "dlv-b")[0] == 0


def test_undepend_keeps_the_row_and_refuses_an_empty_reason(store):
    _cli(store, "depend", "dlv-b", "--on", "dlv-a")
    rc, _, err = _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "  ")
    assert rc == 2 and "--reason" in err and _deps(store, "dlv-b")["depends_on"][0]["active"] is True
    assert _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "B2 no longer needs it")[0] == 0
    [row] = _deps(store, "dlv-b")["depends_on"]
    assert (row["active"], row["removed_reason"]) == (False, "B2 no longer needs it") and row["removed_at"]
    assert _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "again")[0] == 2
    assert _cli(store, "depend", "dlv-b", "--on", "dlv-a")[1].startswith("restored:")


@pytest.mark.parametrize("argv", [("depend", "dlv-a", "--on", "dlv-x"), ("depend", "dlv-x", "--on", "dlv-a"),
                                  ("deps", "dlv-x")])
def test_an_unknown_deliverable_is_refused(store, argv):
    rc, _, err = _cli(store, *argv)
    assert rc == 2 and "unknown deliverable 'dlv-x'" in err


def test_two_projects_with_the_same_ids_do_not_see_each_other(store):
    _cli(store, "depend", "dlv-b", "--on", "dlv-a")
    assert _deps(store, "dlv-b", pid="other-proj")["depends_on"] == []
    assert _cli(store, "depend", "dlv-a", "--on", "dlv-b", pid="other-proj")[0] == 0  # no cycle across tenants


def test_add_with_depends_on_records_the_edges_or_creates_nothing(store):
    planning_cli.tracks_lib.create_track(store, "trk", PID, "trk", "ship trk", phase="queued")
    base = ("add", "--objective", "trk", "--output-kind", "pr", "--title", "t")
    rc, out, _ = _cli(store, *base, "--depends-on", "dlv-a,dlv-b")
    assert rc == 0
    new_id = out.split("Deliverable created: ")[1].split()[0]
    assert {r["depends_on_id"] for r in _deps(store, new_id)["depends_on"]} == {"dlv-a", "dlv-b"}
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    count = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    conn.close()
    rc, _, err = _cli(store, *base, "--depends-on", "dlv-a,dlv-x")
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    assert rc == 2 and "dlv-x" in err and conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == count
    conn.close()
