"""Unit tests for the extracted close-time revalidation: one test per check
(a)-(e) in the module's order, plus both pass paths. The DB is a real migrated
SQLite store built by the same helpers tests/test_close_track_if_done.py uses.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tracks as tracks_lib  # noqa: E402
import track_reconciler_revalidation as revalidation  # noqa: E402
from test_close_track_if_done import PROJECT_ID, _build_db, _set_delivery  # noqa: E402

VERIFIED_AT = "2026-10-25T07:00:00Z"


def _revalidate(state_dir: Path, track_id: str, evidence: dict):
    conn = tracks_lib._get_conn(state_dir)
    try:
        return revalidation.revalidate_close_evidence(conn, track_id, PROJECT_ID, evidence)
    finally:
        conn.close()


def _seed(state_dir: Path, track_id: str, *, phase: str = "active", pr_ref=None) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID, title=track_id, goal_state="ship",
        phase=phase, pr_ref=pr_ref,
    )


def _merged(number: int) -> dict:
    return {"number": number, "state": "MERGED", "mergedAt": VERIFIED_AT}


def _assert_rejected(state_dir: Path, track_id: str, evidence: dict, action: str) -> dict:
    rejection, skip = _revalidate(state_dir, track_id, evidence)
    assert rejection is not None
    assert rejection["action"] == action
    assert rejection["derived_status"] is None
    assert rejection["applied"] is False
    assert skip is False
    return rejection


def test_a_pr_ref_mismatch_rejects_stale(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-pr", pr_ref="#10")
    rejection = _assert_rejected(sd, "R-pr", {"pr_ref": "#11"}, "stale_candidate")
    assert rejection["declared_phase"] == "active"


def test_b_blocker_oi_rejects_stale(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-blocked")
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_open_items (track_id, project_id, oi_id, link_type, link_source) "
        "VALUES (?,?,?,'blocks','manual')",
        ("R-blocked", PROJECT_ID, "OI-R-1"),
    )
    conn.commit()
    conn.close()
    _assert_rejected(sd, "R-blocked", {"pr_ref": None}, "stale_candidate")


def test_c_parked_without_include_flag_rejects_stale(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-parked", phase="parked")
    rejection = _assert_rejected(sd, "R-parked", {"pr_ref": None}, "stale_candidate")
    assert rejection["declared_phase"] == "parked"


def test_d_blocking_dependency_rejects_stale(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-dep-open")
    _seed(sd, "R-dep", pr_ref="#20")
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_dependencies "
        "(from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source) "
        "VALUES (?,?,?,?,?,?)",
        ("R-dep", PROJECT_ID, "R-dep-open", PROJECT_ID, "hard", "manual"),
    )
    conn.commit()
    conn.close()
    evidence = {"pr_ref": "#20", "pr_results": [_merged(20)], "verified_at": VERIFIED_AT}
    _assert_rejected(sd, "R-dep", evidence, "stale_candidate")


def test_e_missing_complete_delivery_rejects_incomplete(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-delivery", pr_ref="#30")
    _set_delivery(sd, "R-delivery", 30, "partial")
    evidence = {"pr_ref": "#30", "pr_results": [_merged(30)], "verified_at": VERIFIED_AT}
    _assert_rejected(sd, "R-delivery", evidence, "noop_incomplete_delivery")


def test_pass_paths_toggle_skip_derived_gate(tmp_path):
    sd = _build_db(tmp_path)
    _seed(sd, "R-pass", pr_ref="#40")
    _set_delivery(sd, "R-pass", 40, "complete")
    evidence = {"pr_ref": "#40", "pr_results": [_merged(40)], "verified_at": VERIFIED_AT}
    assert _revalidate(sd, "R-pass", evidence) == (None, True)
    _seed(sd, "R-plain")
    assert _revalidate(sd, "R-plain", {"pr_ref": None}) == (None, False)
