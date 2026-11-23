"""The planning gaps of a plan (ADR-096, B9): real migrations, readable store, no writes."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import plan_gaps  # noqa: E402

from fixtures.live_store_fixture import build_live_store  # noqa: E402

PID, OTHER, TRACK = "gaps-proj", "other-proj", "feat-gaps"
B1 = "dlv-111111111111"
B2 = "dlv-222222222222"
B3 = "dlv-333333333333"
B4 = "dlv-444444444444"
B5 = "dlv-555555555555"
B6 = "dlv-666666666666"
B7 = "dlv-777777777777"


def _store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    build_live_store(state_dir)
    return state_dir


def _insert_deliverable(state_dir: Path, dlv: str, state: str, title: str,
                        paths=None, project_id: str = PID) -> None:
    metadata = {"deliverable": True, "title": title}
    if paths is not None:
        metadata["dispatch_paths"] = paths
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dlv, project_id, state, TRACK, json.dumps(metadata)),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_dependency(state_dir: Path, deliverable: str, depends_on: str, *,
                       removed: bool = False, project_id: str = PID) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(
            "INSERT INTO deliverable_dependencies "
            "(project_id, deliverable_id, depends_on_id, kind, created_at, created_by, "
            " removed_at, removed_reason) VALUES (?, ?, ?, 'hard', '2026-11-01', 'test', ?, ?)",
            (project_id, deliverable, depends_on,
             "2026-11-02" if removed else None, "undepend" if removed else None),
        )
        conn.commit()
    finally:
        conn.close()


def _plan(tmp_path: Path, body: str) -> Path:
    doc = tmp_path / "plan.md"
    doc.write_text(body, encoding="utf-8")
    return doc


def _full_store(tmp_path: Path) -> Path:
    """One deliverable per row kind: open paths, open without paths, done without paths."""
    state_dir = _store(tmp_path)
    _insert_deliverable(state_dir, B1, "proposed", "B1: first", ["scripts/a.py"])
    _insert_deliverable(state_dir, B2, "proposed", "B2: second")            # no paths
    _insert_deliverable(state_dir, B3, "proposed", "B3: third", ["scripts/c.py"])
    _insert_deliverable(state_dir, B4, "proposed", "B4: fourth", ["scripts/d.py"])
    _insert_deliverable(state_dir, B5, "proposed", "B5: fifth", ["scripts/e.py"])
    _insert_deliverable(state_dir, B6, "proposed", "B6: sixth", ["scripts/f.py"])
    _insert_deliverable(state_dir, B7, "done", "B7: seventh")               # done: not a gap
    return state_dir


_PLAN_BODY = """## Plan

| # | Levering | hangt af van | paden |
|---|---|---|---|
| B1 | first | geen | scripts/a.py |
| B2 | second | B1 | scripts/b.py |
| B4 | fourth | B3 | scripts/d.py |
| B5 | fifth | B6 | scripts/e.py |
| Z9 | no such deliverable | geen | scripts/z.py |
| B6 | sixth | B1, plus zeven dagen | scripts/f.py |
"""


def test_every_gap_kind_once_with_the_active_and_removed_pairs_left_out(tmp_path):
    state_dir = _full_store(tmp_path)
    _insert_dependency(state_dir, B4, B3)                     # active: B4 depends on B3
    _insert_dependency(state_dir, B5, B6, removed=True)       # undepend was deliberate
    doc = _plan(tmp_path, _PLAN_BODY)

    gaps = plan_gaps.find(doc, TRACK, PID, state_dir)

    assert [gap["kind"] for gap in gaps] == [
        "no_paths", "missing_dependency", "unmapped_row", "unreadable_row"]
    assert {gap["class"] for gap in gaps} == {"planning_gap"}
    no_paths, missing, unmapped, unreadable = gaps
    assert (no_paths["deliverable"], no_paths["label"]) == (B2, None)
    assert B2 in no_paths["detail"] and "dispatch_paths" in no_paths["detail"]
    assert (missing["deliverable"], missing["label"]) == (B2, "B2")
    assert B1 in missing["detail"] and "no active dependency" in missing["detail"]
    assert (unmapped["deliverable"], unmapped["label"]) == (None, "Z9")
    assert unmapped["detail"] == "no deliverable titled Z9"
    assert (unreadable["deliverable"], unreadable["label"]) == (None, "B6")
    assert "line 10" in unreadable["detail"] and "plus zeven dagen" in unreadable["detail"]
    # B7 (done, no paths) and the active/removed pairs produced no gap at all.
    assert B7 not in [gap["deliverable"] for gap in gaps]


def test_an_id_column_id_that_exists_wins_over_the_label(tmp_path):
    state_dir = _store(tmp_path)
    _insert_deliverable(state_dir, B1, "proposed", "B1: first", ["scripts/a.py"])
    _insert_deliverable(state_dir, B3, "proposed", "B3: third", ["scripts/c.py"])
    # The label "wrong label" maps to no deliverable; the id column names B1, so
    # the row maps to B1 and its missing B3 dependency is reported on B1.
    doc = _plan(tmp_path, "\n".join([
        "| # | id | Levering | hangt af van | paden |", "|---|---|---|---|---|",
        f"| B2 | `{B1}` | wrong label | B3 | scripts/b.py |", ""]))

    gaps = plan_gaps.find(doc, TRACK, PID, state_dir)

    assert [gap["kind"] for gap in gaps] == ["missing_dependency"]
    assert (gaps[0]["deliverable"], gaps[0]["label"]) == (B1, "B2")


def test_a_doc_without_a_table_is_one_no_table_gap(tmp_path):
    state_dir = _store(tmp_path)  # no deliverables: nothing else can be a gap
    doc = _plan(tmp_path, "## Plan\nno deliverable table here\n")

    assert plan_gaps.find(doc, TRACK, PID, state_dir) == [{
        "class": "planning_gap", "kind": "no_table", "deliverable": None,
        "label": None, "detail": "the plan document has no deliverable table"}]


def test_a_missing_store_is_one_unknown_gap_naming_the_store(tmp_path):
    doc = _plan(tmp_path, _PLAN_BODY)
    missing = tmp_path / "absent"

    gaps = plan_gaps.find(doc, TRACK, PID, missing)

    assert len(gaps) == 1 and gaps[0]["kind"] == "unknown"
    assert gaps[0]["deliverable"] is None and gaps[0]["label"] is None
    assert "planning store not found" in gaps[0]["detail"] and str(missing) in gaps[0]["detail"]


def test_a_store_without_the_dependency_table_is_one_unknown_gap(tmp_path):
    state_dir = tmp_path / "old"
    state_dir.mkdir()
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, project_id TEXT, "
                     "state TEXT, track TEXT, metadata_json TEXT)")
        conn.commit()
    finally:
        conn.close()
    doc = _plan(tmp_path, _PLAN_BODY)

    gaps = plan_gaps.find(doc, TRACK, PID, state_dir)

    assert len(gaps) == 1 and gaps[0]["kind"] == "unknown"
    assert "deliverable_dependencies" in gaps[0]["detail"]


def test_malformed_metadata_is_one_unknown_gap_naming_the_dispatch(tmp_path):
    state_dir = _store(tmp_path)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, metadata_json) "
                     "VALUES (?, ?, 'proposed', ?, '{not json')", (B1, PID, TRACK))
        conn.commit()
    finally:
        conn.close()

    gaps = plan_gaps.find(_plan(tmp_path, _PLAN_BODY), TRACK, PID, state_dir)

    assert len(gaps) == 1 and gaps[0]["kind"] == "unknown"
    assert B1 in gaps[0]["detail"] and "malformed metadata_json" in gaps[0]["detail"]


def test_no_plan_document_is_one_unknown_gap(tmp_path):
    gaps = plan_gaps.find(None, TRACK, PID, _store(tmp_path))

    assert [gap["kind"] for gap in gaps] == ["unknown"]
    assert "goal_state" in gaps[0]["detail"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a chmod 000 file, so the read cannot fail")
def test_an_unreadable_doc_is_one_unknown_gap(tmp_path):
    state_dir = _store(tmp_path)
    doc = _plan(tmp_path, _PLAN_BODY)
    doc.chmod(0o000)
    try:
        gaps = plan_gaps.find(doc, TRACK, PID, state_dir)
    finally:
        doc.chmod(0o600)

    assert len(gaps) == 1 and gaps[0]["kind"] == "unknown"
    assert "plan doc unreadable" in gaps[0]["detail"] and str(doc) in gaps[0]["detail"]


def test_another_project_is_not_read(tmp_path):
    state_dir = _full_store(tmp_path)
    _insert_deliverable(state_dir, "dlv-aaaaaaaaaaaa", "proposed", "B1: other", [], project_id=OTHER)
    doc = _plan(tmp_path, _PLAN_BODY)

    gaps = plan_gaps.find(doc, TRACK, PID, state_dir)

    assert "dlv-aaaaaaaaaaaa" not in [gap["deliverable"] for gap in gaps]
