"""The one-time plan backfill (ADR-096, B8) on a live-shaped store and a tmp plans tree."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import backfill_plan_dependencies as backfill  # noqa: E402
import tracks as tracks_lib  # noqa: E402
from fixtures.live_store_fixture import build_live_store  # noqa: E402

PID, TRACK = "test-proj", "feat-plan"
A, B, C, D = "dlv-aaaaaaaaaaaa", "dlv-bbbbbbbbbbbb", "dlv-cccccccccccc", "dlv-dddddddddddd"
DEP_HEADER = "| # | Levering | hangt af van |"

def _insert(conn, dlv, title, *, state="proposed", track=TRACK, paths=None):
    metadata = {"title": title, "deliverable": True}
    if paths is not None:
        metadata["dispatch_paths"] = paths
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json) "
                 "VALUES (?, ?, ?, ?, ?)", (dlv, PID, state, track, json.dumps(metadata)))

@pytest.fixture()
def store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    build_live_store(state_dir)
    tracks_lib.create_track(state_dir, TRACK, PID, TRACK, "ship", phase="queued",
                            metadata_json=json.dumps({"plan_scope": "architecture"}))
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        for dlv, title in ((A, "B0: base"), (B, "B1: next"), (C, "B2: last"), (D, "B3: extra")):
            _insert(conn, dlv, title)
        conn.commit()
    finally:
        conn.close()
    return state_dir

def _write_plan(plans: Path, rows, *, track=TRACK, header=DEP_HEADER, name="plan.md"):
    path = plans / "specs" / track / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([f"# Plan for {track}", "", f"Track `{track}`", "",
                               header, "|---|---|---|", *rows, ""]), encoding="utf-8")
    return path

def _run(capsys, store: Path, plans: Path, *argv):
    rc = backfill.main(["--project-id", PID, "--state-dir", str(store),
                        "--plans-root", str(plans), "--json", *argv])
    captured = capsys.readouterr()
    return rc, (json.loads(captured.out) if captured.out.strip() else None), captured.err

def _sql(store: Path, sql: str, params=()):
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()

def _deps(store: Path):
    return [(r[0], r[1], r[2], r[3]) for r in _sql(
        store, "SELECT deliverable_id, depends_on_id, kind, removed_at FROM deliverable_dependencies "
               "ORDER BY deliverable_id, depends_on_id")]

def _metadata(store: Path, dlv: str):
    return json.loads(_sql(store, "SELECT metadata_json FROM dispatches WHERE project_id = ? "
                                  "AND dispatch_id = ?", (PID, dlv))[0][0])

def _events(store: Path, event_type: str):
    return [(r[0], r[1]) for r in _sql(store, "SELECT actor, reason FROM coordination_events "
                                               "WHERE event_type = ?", (event_type,))]

def test_dry_run_writes_nothing_then_apply_is_idempotent(store, tmp_path, capsys):
    _write_plan(tmp_path, ["| B0 | base | geen |",
                           "| B1 | next | B0 (soft) | `scripts/lib/a.py`, `tests/test_a.py` |"],
                header="| # | Levering | hangt af van | paden |")
    rc, payload, err = _run(capsys, store, tmp_path)
    assert rc == 0, err
    report = payload["tracks"][0]
    assert (report["plan"], report["rows"], report["rows_mapped"], report["dependencies"]["new"]) == (
        "one", 2, 2, 1)
    [proposal] = [p for p in report["proposals"] if p["type"] == "dependency"]
    assert (proposal["deliverable"], proposal["on_id"], proposal["status"]) == (B, A, "new")
    assert _deps(store) == [] and "dispatch_paths" not in _metadata(store, B)
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    assert rc == 0, err and payload["applied"] == {"dependencies": 1, "paths": 1}
    assert _deps(store) == [(B, A, "soft", None)]
    assert _metadata(store, B)["dispatch_paths"] == ["scripts/lib/a.py", "tests/test_a.py"]
    [(actor, reason)] = _events(store, "deliverable_metadata_set")
    assert actor == "backfill_plan_dependencies" and "dispatch_paths" in reason
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    report = payload["tracks"][0]
    assert rc == 0, err
    assert (report["dependencies"]["new"], report["dependencies"]["exists"]) == (0, 1)
    assert (report["paths"]["proposed"], report["paths"]["has_paths"]) == (0, 1)
    assert payload["applied"] == {"dependencies": 0, "paths": 0}
    assert _deps(store) == [(B, A, "soft", None)] and len(_events(store, "deliverable_metadata_set")) == 1

def test_a_removed_dependency_and_existing_paths_are_never_touched(store, tmp_path, capsys):
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    try:
        conn.execute("INSERT INTO deliverable_dependencies (project_id, deliverable_id, depends_on_id, "
                     "kind, created_at, created_by, removed_at, removed_reason) VALUES "
                     "(?, ?, ?, 'hard', '2026-11-01T00:00:00Z', 't0', '2026-11-02T00:00:00Z', 'superseded')",
                     (PID, B, A))
        conn.execute("UPDATE dispatches SET metadata_json = ? WHERE project_id = ? AND dispatch_id = ?",
                     (json.dumps({"title": "B1: next", "deliverable": True,
                                  "dispatch_paths": ["old.py"]}), PID, B))
        conn.commit()
    finally:
        conn.close()
    _write_plan(tmp_path, ["| B1 | next | B0 | `new.py` |"],
                header="| # | Levering | hangt af van | paden |")
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    report = payload["tracks"][0]
    assert rc == 0, err
    assert (report["dependencies"]["removed"], report["paths"]["has_paths"]) == (1, 1)
    [proposal] = [p for p in report["proposals"] if p["type"] == "dependency"]
    assert "removed on 2026-11-02T00:00:00Z: superseded, not restored" in proposal["detail"]
    assert payload["applied"] == {"dependencies": 0, "paths": 0}
    assert _deps(store) == [(B, A, "hard", "2026-11-02T00:00:00Z")]
    assert _metadata(store, B)["dispatch_paths"] == ["old.py"]
    assert _events(store, "deliverable_metadata_set") == []

def test_stored_and_proposed_cycles_are_refused(store, tmp_path, capsys):
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    try:
        conn.execute("INSERT INTO deliverable_dependencies (project_id, deliverable_id, depends_on_id, "
                     "kind, created_at, created_by) VALUES (?, ?, ?, 'hard', '2026-11-01T00:00:00Z', 't0')",
                     (PID, A, B))
        conn.execute("DELETE FROM dispatches WHERE dispatch_id = ?", (D,))  # keep the plan rows to B0..B2
        conn.commit()
    finally:
        conn.close()
    _write_plan(tmp_path, ["| B1 | next | B0 |"])
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    report = payload["tracks"][0]
    [proposal] = [p for p in report["proposals"] if p["type"] == "dependency"]
    assert rc == 0 and report["dependencies"]["cycle"] == 1 and "would close a cycle" in proposal["detail"]
    assert _deps(store) == [(A, B, "hard", None)]
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    try:
        conn.execute("DELETE FROM deliverable_dependencies")
        conn.commit()
    finally:
        conn.close()
    _write_plan(tmp_path, ["| B1 | next | B2 |", "| B2 | last | B1 |"])
    rc, payload, err = _run(capsys, store, tmp_path)
    assert rc == 0, err
    assert (payload["tracks"][0]["dependencies"]["new"],
            payload["tracks"][0]["dependencies"]["cycle"]) == (1, 1)

def test_unknown_unmapped_and_a_label_mismatch_are_named(store, tmp_path, capsys):
    _write_plan(tmp_path, ["| BX | `dlv-bbbbbbbbbbbb` | B1: next | B0 |",
                           "| ZZ |  | missing | B0 |",
                           "| B1 |  | next | dlv-999999999999 |"],
                header="| # | id | Levering | hangt af van |")
    rc, payload, err = _run(capsys, store, tmp_path)
    report = payload["tracks"][0]
    assert rc == 0, err
    assert (report["dependencies"]["new"], report["dependencies"]["unmapped"],
            report["dependencies"]["unknown_predecessor"], report["rows_mapped"]) == (1, 1, 1, 2)
    by_status = {p["status"]: p for p in report["proposals"] if p["type"] == "dependency"}
    assert by_status["new"]["deliverable"] == B and "label_mismatch" in by_status["new"]["label_mismatch"]
    assert by_status["unmapped"]["detail"] == "no deliverable titled ZZ"
    assert "dlv-999999999999" in by_status["unknown_predecessor"]["detail"]

def test_unreadable_order_and_a_bad_path_propose_nothing(store, tmp_path, capsys):
    _write_plan(tmp_path, ["| B1 | next | B4b plus zeven dagen | `with space.py` |"],
                header="| # | Levering | hangt af van | paden |")
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    report = payload["tracks"][0]
    assert rc == 0, err
    assert report["order_errors"][0]["line"] == 7 and "plus zeven dagen" in report["order_errors"][0]["error"]
    assert report["paths"]["error"] == 1 and report["dependencies"]["new"] == 0
    assert [p for p in report["proposals"] if p["type"] == "dependency"] == []
    assert payload["applied"] == {"dependencies": 0, "paths": 0}
    assert "dispatch_paths" not in _metadata(store, B)

def test_coverage_uncovered_none_and_several(store, tmp_path, capsys):
    rc, payload, err = _run(capsys, store, tmp_path)
    assert rc == 0 and payload["tracks"][0]["plan"] == "none"
    assert payload["tracks"][0]["uncovered_open"] == [A, B, C, D]
    _write_plan(tmp_path, ["| B0 | base | geen |", "| B1 | next | B0 |"])
    rc, payload, err = _run(capsys, store, tmp_path)
    total = payload["total"]
    assert rc == 0, err
    assert (total["tracks"], total["rows"], total["rows_mapped"], total["open_deliverables"]) == (1, 2, 2, 4)
    assert total["uncovered_open"] == [C, D]
    _write_plan(tmp_path, ["| B1 | next | geen |"], name="plan-b.md")
    rc, payload, err = _run(capsys, store, tmp_path)
    assert payload["tracks"][0]["plan"] == "several"
    assert payload["tracks"][0]["plan_note"].startswith("several plans: ")
    assert payload["tracks"][0]["rows"] == 0 and payload["total"]["plan"]["several"] == 1

def test_duplicate_edges_and_default_track_selection(store, tmp_path, capsys):
    tracks_lib.create_track(store, "done-track", PID, "done", "done", phase="done",
                            metadata_json=json.dumps({"plan_scope": "architecture"}))
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    try:
        _insert(conn, "dlv-eeeeeeeeeeee", "B9: old", track="done-track")
        conn.commit()
    finally:
        conn.close()
    _write_plan(tmp_path, ["| B1 | `dlv-bbbbbbbbbbbb` | B1: next | B0 |",
                           "| B1 | `dlv-bbbbbbbbbbbb` | B1: next | B0 |"],
                header="| # | id | Levering | hangt af van |")
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    assert rc == 0, err
    assert [report["track"] for report in payload["tracks"]] == [TRACK]
    assert payload["tracks"][0]["dependencies"]["exists"] == 1
    assert payload["applied"]["dependencies"] == 1 and _deps(store) == [(B, A, "hard", None)]
    assert payload["total"]["open_deliverables"] == 4

def test_refusals_write_nothing(store, tmp_path, capsys):
    _write_plan(tmp_path, ["| B1 | next | B0 |"])
    rc, payload, err = _run(capsys, store, tmp_path, "--track", "no-such-track", "--apply")
    assert rc == 2 and "unknown track" in err and payload is None and _deps(store) == []
    rc, payload, err = _run(capsys, store, tmp_path / "missing")
    assert rc == 2 and "not a directory" in err and payload is None
    conn = sqlite3.connect(str(store / "runtime_coordination.db"))
    try:
        conn.execute("DROP TABLE deliverable_dependencies")
        conn.commit()
    finally:
        conn.close()
    rc, payload, err = _run(capsys, store, tmp_path, "--apply")
    assert rc == 2 and "deliverable_dependencies" in err and payload is None
