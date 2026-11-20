"""`vnx deliverable add --depends-on`, `depend`, `undepend`, `deps` (ADR-096 §1-2, B1c): real CLI, real store."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planning_cli
import tracks as tracks_lib

from fixtures.closure_track_fixture import build_tracks_db
from fixtures.live_store_fixture import build_live_store

PID, OTHER, TRACK = "test-proj", "other-proj", "feat-deps"


def _seed(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    try:
        for pid in (PID, OTHER):
            for dlv in ("dlv-a", "dlv-b", "dlv-c"):
                conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json) "
                             "VALUES (?, ?, 'proposed', ?, ?)",
                             (dlv, pid, TRACK, json.dumps({"title": dlv, "deliverable": True})))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    build_live_store(state_dir)
    for pid in (PID, OTHER):
        tracks_lib.create_track(state_dir, TRACK, pid, TRACK, f"ship {TRACK}", phase="queued",
                                metadata_json=json.dumps({"plan_scope": "architecture"}))
    _seed(state_dir)
    return state_dir


def _cli(store: Path, *argv: str, pid: str = PID) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main(["deliverable", *argv, "--project-id", pid, "--state-dir", str(store)])
    return rc, out.getvalue(), err.getvalue()


def _sql(store: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(store / tracks_lib.DB_FILENAME))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _add(store: Path, title: str, *extra: str) -> tuple[int, str, str]:
    return _cli(store, "add", "--objective", TRACK, "--output-kind", "pr", "--title", title, *extra)


def _id_of(store: Path, title: str) -> list[str]:
    return [r[0] for r in _sql(store, "SELECT dispatch_id FROM dispatches WHERE project_id = ? AND "
                                      "json_extract(metadata_json, '$.title') = ?", (PID, title))]


def _deps(store: Path, dlv: str, pid: str = PID) -> dict:
    rc, out, err = _cli(store, "deps", dlv, "--json", pid=pid)
    assert rc == 0, err
    return json.loads(out)


def test_add_depends_on_writes_hard_rows_in_the_same_project(store):
    rc, _, err = _add(store, "new one", "--depends-on", "dlv-a, dlv-b")
    assert rc == 0, err
    [new] = _id_of(store, "new one")
    rows = _sql(store, "SELECT project_id, depends_on_id, kind, removed_at FROM deliverable_dependencies "
                       "WHERE deliverable_id = ? ORDER BY depends_on_id", (new,))
    assert rows == [(PID, "dlv-a", "hard", None), (PID, "dlv-b", "hard", None)]
    assert [r["deliverable_id"] for r in _deps(store, "dlv-a")["depended_on_by"]] == [new]
    assert _deps(store, "dlv-a", pid=OTHER)["depended_on_by"] == []


@pytest.mark.parametrize("depends_on, named", [
    ("dlv-missing", "unknown deliverable 'dlv-missing'"),
    ("dlv-a,dlv-missing", "unknown deliverable 'dlv-missing'"),  # the first edge rolls back too
    ("dlv-a,dlv-a", "already depends on dlv-a"),
])
def test_refused_dependency_leaves_no_deliverable_and_no_edge(store, depends_on, named):
    before = _sql(store, "SELECT COUNT(*) FROM dispatches")[0][0]
    rc, out, err = _add(store, "refused one", "--depends-on", depends_on)
    assert rc == 2
    assert named in err and "No change made." in err and "Traceback" not in err
    assert out == ""
    assert _id_of(store, "refused one") == []
    assert _sql(store, "SELECT COUNT(*) FROM dispatches")[0][0] == before
    assert _sql(store, "SELECT COUNT(*) FROM deliverable_dependencies") == [(0,)]
    assert _sql(store, "SELECT COUNT(*) FROM coordination_events WHERE event_type IN "
                       "('deliverable_created', 'deliverable_dependency_added')") == [(0,)]


def test_add_with_depends_on_in_another_project_is_refused(store):
    rc, _, err = _add(store, "cross", "--depends-on", "dlv-a")
    assert rc == 0, err
    rc, _, err = _cli(store, "add", "--objective", TRACK, "--output-kind", "pr", "--title", "x",
                      "--depends-on", _id_of(store, "cross")[0], pid=OTHER)
    assert rc == 2 and "unknown deliverable" in err and f"in project {OTHER!r}" in err


def test_depend_undepend_and_deps_json_shapes(store):
    rc, out, err = _cli(store, "depend", "dlv-b", "--on", "dlv-a")
    assert (rc, out.strip(), err) == (0, "added: dlv-b depends on dlv-a (hard)", "")
    rc, out, _ = _cli(store, "depend", "dlv-c", "--on", "dlv-b", "--kind", "soft")
    assert (rc, out.strip()) == (0, "added: dlv-c depends on dlv-b (soft)")
    rc, out, err = _cli(store, "undepend", "dlv-c", "--on", "dlv-b", "--reason", "  merged elsewhere ")
    assert (rc, out.strip(), err) == (0, "removed: dlv-c no longer depends on dlv-b (reason: merged elsewhere)", "")

    deps = _deps(store, "dlv-b")
    assert set(deps) == {"deliverable", "project_id", "depends_on", "depended_on_by"}
    assert (deps["deliverable"], deps["project_id"]) == ("dlv-b", PID)
    [up], [down] = deps["depends_on"], deps["depended_on_by"]
    assert set(up) == {"deliverable_id", "depends_on_id", "kind", "created_at", "created_by",
                       "removed_at", "removed_reason", "active"}
    assert (up["depends_on_id"], up["kind"], up["active"]) == ("dlv-a", "hard", True)
    assert (down["deliverable_id"], down["kind"], down["active"]) == ("dlv-c", "soft", False)
    assert down["removed_reason"] == "merged elsewhere" and down["removed_at"]

    rc, out, _ = _cli(store, "deps", "dlv-b")
    assert rc == 0
    assert "depends on:" in out and "dlv-a" in out and "active" in out
    assert "removed" in out and "(merged elsewhere)" in out

    rc, out, _ = _cli(store, "depend", "dlv-c", "--on", "dlv-b")
    assert (rc, out.strip()) == (0, "restored: dlv-c depends on dlv-b (hard)")


@pytest.mark.parametrize("edges, new, cycle", [
    ([], ("dlv-a", "dlv-a"), "dlv-a -> dlv-a"),
    ([("dlv-b", "dlv-a")], ("dlv-a", "dlv-b"), "dlv-a -> dlv-b -> dlv-a"),
    ([("dlv-b", "dlv-a"), ("dlv-c", "dlv-b")], ("dlv-a", "dlv-c"), "dlv-a -> dlv-c -> dlv-b -> dlv-a"),
])
def test_depend_refuses_a_cycle_and_names_it(store, edges, new, cycle):
    for dlv, on in edges:
        assert _cli(store, "depend", dlv, "--on", on)[0] == 0
    rc, out, err = _cli(store, "depend", new[0], "--on", new[1])
    assert rc == 2 and out == "" and "Traceback" not in err
    assert err.strip() == f"deliverable depend: refused, this would close a cycle: {cycle}. No change made."
    assert len(_sql(store, "SELECT 1 FROM deliverable_dependencies")) == len(edges)


def test_depend_refuses_an_unknown_id_on_either_side(store):
    for dlv, on in (("dlv-zz", "dlv-a"), ("dlv-a", "dlv-zz")):
        rc, _, err = _cli(store, "depend", dlv, "--on", on)
        assert rc == 2 and f"unknown deliverable 'dlv-zz' in project {PID!r}" in err
    assert _sql(store, "SELECT COUNT(*) FROM deliverable_dependencies") == [(0,)]


def test_undepend_refuses_an_empty_reason_and_a_missing_edge(store):
    assert _cli(store, "depend", "dlv-b", "--on", "dlv-a")[0] == 0
    rc, _, err = _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "   ")
    assert rc == 2 and "--reason is required and may not be empty" in err
    assert _sql(store, "SELECT removed_at FROM deliverable_dependencies") == [(None,)]
    with pytest.raises(SystemExit) as exc, contextlib.redirect_stderr(io.StringIO()):
        planning_cli.main(["deliverable", "undepend", "dlv-b", "--on", "dlv-a", "--state-dir", str(store)])
    assert exc.value.code == 2
    rc, _, err = _cli(store, "undepend", "dlv-c", "--on", "dlv-a", "--reason", "gone")
    assert rc == 2 and "dlv-c has no active dependency on dlv-a" in err


def test_two_projects_with_the_same_ids_do_not_see_each_other(store):
    assert _cli(store, "depend", "dlv-b", "--on", "dlv-a")[0] == 0
    other = _deps(store, "dlv-b", pid=OTHER)
    assert (other["project_id"], other["depends_on"], other["depended_on_by"]) == (OTHER, [], [])
    rc, _, err = _cli(store, "undepend", "dlv-b", "--on", "dlv-a", "--reason", "x", pid=OTHER)
    assert rc == 2 and f"in project {OTHER!r}" in err
    assert _cli(store, "depend", "dlv-a", "--on", "dlv-b", pid=OTHER)[0] == 0  # no cycle across projects
    assert _sql(store, "SELECT project_id, deliverable_id, depends_on_id FROM deliverable_dependencies "
                       "ORDER BY project_id") == [(OTHER, "dlv-a", "dlv-b"), (PID, "dlv-b", "dlv-a")]


def test_deps_of_an_unknown_deliverable_and_a_store_without_the_table(store, tmp_path):
    rc, out, err = _cli(store, "deps", "dlv-zz", "--json")
    assert (rc, out) == (2, "") and "unknown deliverable 'dlv-zz'" in err
    bare = tmp_path / "bare"
    build_tracks_db(bare)
    for argv in (("deps", "dlv-a"), ("depend", "dlv-b", "--on", "dlv-a")):
        rc, _, err = _cli(bare, *argv)
        assert rc == 2 and "run `vnx migrate` (migration 0039) first" in err and "Traceback" not in err
