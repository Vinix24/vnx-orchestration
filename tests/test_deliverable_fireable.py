"""ADR-096 §18 / B5: `vnx deliverable fireable` — what a T0 can fire together now, and why the rest drops."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blast_radius_gate as brg
import blast_radius_sources as brs
import deliverable_fireable as dlv_fireable
import planning_cli
import tracks as tracks_lib

from fixtures.live_store_fixture import build_live_store

PID, OTHER, T0, T1 = "proj-a", "proj-b", "t-p0", "t-p1"


class FakeGh:
    """The gh callable `open_pr_subjects` takes: pr list + per-PR files, as the sources tests stub it."""

    def __init__(self, prs, files=None):
        self.prs, self.files = prs, files or {}

    def __call__(self, argv, cwd):
        if argv[1] == "list":
            return json.dumps(self.prs)
        number = int(argv[2])
        pr = next(p for p in self.prs if p["number"] == number)
        return json.dumps({"headRefName": pr["headRefName"], "headRefOid": pr["headRefOid"],
                           "files": [{"path": f} for f in self.files[number]]})


@pytest.fixture()
def store(tmp_path):
    state = tmp_path / "state"
    build_live_store(state)
    (state / "t0_receipts.ndjson").write_text("")
    for pid in (PID, OTHER):
        tracks_lib.create_track(state, T0, pid, T0, "ship", phase="queued", priority="P0", horizon="now")
        tracks_lib.create_track(state, T1, pid, T1, "ship", phase="queued", priority="P1", horizon="later")
    return state


@pytest.fixture(autouse=True)
def no_live_work(monkeypatch):
    """Every test starts with no live dispatch and no open PR; a test that needs one swaps the value or gh."""
    live = {"v": brs.Sources()}
    monkeypatch.setattr(brs, "live_subjects", lambda sd, pid: live["v"])
    monkeypatch.setattr(brg, "gh", FakeGh([]))  # the CLI path resolves brg.gh at call time
    return live


def add_deliverable(state, did, track=T0, pid=PID, created="2026-01-01T00:00:00Z", status="ready"):
    with sqlite3.connect(state / tracks_lib.DB_FILENAME) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_kind, output_ref, "
            "created_at, metadata_json) VALUES (?, ?, ?, ?, 'pr', ?, ?, ?)",
            (did, pid, status, track, f"pr:{did}", created, json.dumps({"title": did, "deliverable": True})))


def add_dependency(state, did, on, *, kind="hard", on_status="completed", pid=PID):
    with sqlite3.connect(state / tracks_lib.DB_FILENAME) as conn:
        # the predecessor's own row: read_edges needs its state, it is not itself a ready candidate here
        conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json) "
                     "VALUES (?, ?, ?, ?, '{}')", (on, pid, on_status, T0))
        conn.execute("INSERT INTO deliverable_dependencies (project_id, deliverable_id, depends_on_id, kind, "
                     "created_at, created_by) VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', 'test')",
                     (pid, did, on, kind))


def merge_receipt(state, did, pid=PID):
    with open(state / "t0_receipts.ndjson", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "pr_merged", "conclusion": "merged", "project_id": pid,
                             "dispatch_id": did, "branch": f"dispatch/{did}"}) + "\n")


def stage_spec(state, did, entries, bucket="pending"):
    """``entries`` is a list of paths (read-write) or of ``{"path": ..., "access": ...}`` dicts."""
    directory = state.parent / "dispatches" / bucket / did
    directory.mkdir(parents=True, exist_ok=True)
    paths = [e if isinstance(e, dict) else {"path": e} for e in entries]
    (directory / "dispatch-spec.json").write_text(json.dumps({"dispatch_paths": paths}))


def run(state, *, pid=PID, horizon=None, gh=None):
    root = state.parent
    sources = dlv_fireable.default_sources(state, pid, root, gh or FakeGh([]))
    return dlv_fireable.select(state_dir=state, project_id=pid, repo_root=root, sources=sources, horizon=horizon)


def reasons(selection):
    return {e.deliverable_id: e.reason for e in selection.dropped}


def fireable(selection):
    return [e.deliverable_id for e in selection.fireable]


def cli(state, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = planning_cli.main(["deliverable", "fireable", "--project-id", PID,
                                "--state-dir", str(state), "--repo-root", str(state.parent), *argv])
    return rc, out.getvalue(), err.getvalue()


def test_a_satisfied_predecessor_fires_and_an_open_one_drops_with_its_id(store):
    add_deliverable(store, "dlv-done")
    stage_spec(store, "dlv-done", ["scripts/ok.py"])
    add_deliverable(store, "dlv-blocked")
    stage_spec(store, "dlv-blocked", ["scripts/other.py"])
    add_dependency(store, "dlv-done", "dlv-pred-merged")
    merge_receipt(store, "dlv-pred-merged")
    add_dependency(store, "dlv-blocked", "dlv-pred-open", on_status="ready")
    selection = run(store)
    assert fireable(selection) == ["dlv-done"]
    assert reasons(selection) == {"dlv-blocked": "predecessor_open: dlv-pred-open"}


def test_a_retired_predecessor_drops(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    add_dependency(store, "dlv-x01", "dlv-pred-gone", on_status="cancelled")
    assert reasons(run(store)) == {"dlv-x01": "predecessor_retired"}


def test_a_spec_without_a_write_path_is_scope_incomplete(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", [{"path": "docs/", "access": "read"}])
    assert reasons(run(store)) == {"dlv-x01": "scope_incomplete"}


def test_a_missing_spec_is_unknown_never_no_overlap(store):
    add_deliverable(store, "dlv-x01")
    [reason] = reasons(run(store)).values()
    assert reason.startswith("unknown: ") and "no dispatch-spec.json" in reason


def test_overlap_with_an_open_pr_names_the_path_and_the_pr(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    gh = FakeGh([{"number": 7, "headRefName": "feature/x", "headRefOid": "aaa"}], {7: ["scripts/x01.py"]})
    selection = run(store, gh=gh)
    assert fireable(selection) == [] and reasons(selection) == {"dlv-x01": "overlap: scripts/x01.py with #7"}


def test_overlap_with_a_live_dispatch_names_the_dispatch(store, no_live_work):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    no_live_work["v"] = brs.Sources((brs.Subject("dlv-live", ("scripts/x01.py",), "dispatch/dlv-live"),))
    assert reasons(run(store)) == {"dlv-x01": "overlap: scripts/x01.py with dlv-live"}


def test_an_unreadable_source_is_unknown(store, no_live_work):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    no_live_work["v"] = brs.Sources(unavailable="live_work unavailable: db gone")
    assert reasons(run(store)) == {"dlv-x01": "unknown: live_work unavailable: db gone"}


def test_a_live_dispatch_with_unknown_paths_is_unknown(store, no_live_work):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    no_live_work["v"] = brs.Sources(
        (brs.Subject("dlv-unmeasured", None, "dispatch/dlv-unmeasured"),),
        reasons={"dlv-unmeasured": "lock probe unmeasured: boom"})
    assert reasons(run(store)) == {"dlv-x01": "unknown: lock probe unmeasured: boom"}


def test_an_open_pr_with_unreadable_files_is_unknown(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    gh = FakeGh([{"number": 9, "headRefName": "dispatch/dlv-other", "headRefOid": "bbb"}])  # no files for 9
    [reason] = reasons(run(store, gh=gh)).values()
    assert reason.startswith("unknown: gh pr view 9 failed: ")


def test_two_overlapping_ready_deliverables_select_by_priority_then_age(store):
    add_deliverable(store, "dlv-p1-old", T1, created="2025-01-01T00:00:00Z")
    add_deliverable(store, "dlv-p0-new", T0, created="2026-01-01T00:00:00Z")
    for did in ("dlv-p1-old", "dlv-p0-new"):
        stage_spec(store, did, ["scripts/same.py"])
    selection = run(store)
    assert fireable(selection) == ["dlv-p0-new"]  # P0 beats the older P1
    assert reasons(selection) == {"dlv-p1-old": "overlap: scripts/same.py with dlv-p0-new"}


def test_same_priority_selects_the_older_one(store):
    add_deliverable(store, "dlv-young", created="2026-02-01T00:00:00Z")
    add_deliverable(store, "dlv-old", created="2026-01-01T00:00:00Z")
    for did in ("dlv-young", "dlv-old"):
        stage_spec(store, did, ["scripts/same.py"])
    selection = run(store)
    assert fireable(selection) == ["dlv-old"]
    assert reasons(selection) == {"dlv-young": "overlap: scripts/same.py with dlv-old"}


def test_non_overlapping_ready_deliverables_both_fire(store):
    add_deliverable(store, "dlv-a01")
    add_deliverable(store, "dlv-b01")
    stage_spec(store, "dlv-a01", ["scripts/a01.py"])
    stage_spec(store, "dlv-b01", ["scripts/b01.py"])
    selection = run(store)
    assert fireable(selection) == ["dlv-a01", "dlv-b01"] and reasons(selection) == {}


def test_the_same_id_in_two_projects_never_mixes(store):
    for pid in (PID, OTHER):
        add_deliverable(store, "dlv-shared", pid=pid)
    stage_spec(store, "dlv-shared", ["scripts/shared.py"])
    add_dependency(store, "dlv-shared", "dlv-ghost", on_status="ready", pid=OTHER)
    assert fireable(run(store)) == ["dlv-shared"]  # PID's own row has no predecessor
    assert reasons(run(store, pid=OTHER)) == {"dlv-shared": "predecessor_open: dlv-ghost"}


def test_horizon_filter_narrows_to_one_band(store):
    add_deliverable(store, "dlv-now", T0)
    add_deliverable(store, "dlv-later", T1)
    stage_spec(store, "dlv-now", ["scripts/now.py"])
    stage_spec(store, "dlv-later", ["scripts/later.py"])
    assert fireable(run(store, horizon="now")) == ["dlv-now"]
    assert fireable(run(store)) == ["dlv-now", "dlv-later"]


def test_json_shape_and_exit_zero_on_an_empty_list(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    rc, out, err = cli(store, "--json")
    assert rc == 0, err
    body = json.loads(out)
    assert body == {"fireable": [{"deliverable_id": "dlv-x01", "track": T0, "reason": None}], "dropped": []}
    empty = store.parent / "empty" / "state"
    build_live_store(empty)
    rc, out, err = cli(empty, "--json")
    assert rc == 0 and json.loads(out) == {"fireable": [], "dropped": []} and err == ""


def test_cli_table_and_read_only(store):
    add_deliverable(store, "dlv-x01")
    stage_spec(store, "dlv-x01", ["scripts/x01.py"])
    before = sorted(p.name for p in store.iterdir())
    rc, out, err = cli(store)
    assert rc == 0, err
    assert "fireable:" in out and "dlv-x01" in out and "dropped: none" in out
    assert sorted(p.name for p in store.iterdir()) == before  # no ledger line, no new state file
