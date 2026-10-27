"""tests/test_objective_reconcile_summary_golden.py — run_reconcile summary golden.

Freezes the shape and the values of the summary ``run_reconcile`` writes while
its steps 4a/4b move into ``objective_reconcile_steps`` (S5): gh absent (exit 3,
all unverified), one CONFIRMED + one deferred candidate, and no candidates. The
literals were captured from the code BEFORE the extraction (origin/main
41472157); fixtures come from tests/test_objective_reconcile.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
for _p in (_TESTS, _ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import objective_reconcile  # noqa: E402
from test_objective_reconcile import (  # noqa: E402
    PROJECT_ID, _absent_gh, _build_db, _make_gh_mock, _merged_pr, _seed_track,
)

_SUMMARY_KEYS = ["counts", "dispatch_outcomes", "evidence_source_health", "finished_at",
                 "mode", "per_track", "project_id", "provenance", "run_id", "started_at"]
_COUNTS_KEYS = ["closed", "closed_sibling", "confirmed", "deferred", "delivery_hold",
                "nominated", "open_pr", "reopened_guard", "stale", "tracks", "unverified"]


def _counts(**overrides: int) -> dict:
    """The frozen all-zero counter block; each case overrides only what it set."""
    base = {
        "tracks": 0, "nominated": 0, "confirmed": 0, "closed": 0,
        "closed_sibling": 0, "open_pr": 0, "unverified": 0, "deferred": 0,
        "stale": 0, "reopened_guard": 0, "delivery_hold": 0,
    }
    base.update(overrides)
    return base


_CASES = {
    "gh_absent": {
        "tracks": [("T-gold-nogh", "active", "#700")],
        "gh_available": False, "merged": [], "kwargs": {},
        "gh_health": "absent", "exit_code": 3,
        "counts": _counts(tracks=1, nominated=1, unverified=1),
        "per_track": [
            ("T-gold-nogh", "unverified", ["pr_ref", "reason", "track_id", "verdict"]),
        ],
    },
    "one_confirmed_one_deferred": {
        # track_id ASC: T-gold-a takes the one allowed gh call, T-gold-b defers.
        "tracks": [("T-gold-a", "active", "#810"), ("T-gold-b", "active", "#811")],
        "gh_available": True, "merged": [810, 811], "kwargs": {"max_gh_calls": 1},
        "gh_health": "ok", "exit_code": 0,
        "counts": _counts(tracks=2, nominated=2, confirmed=1, deferred=1),
        "per_track": [
            ("T-gold-a", "CONFIRMED", ["pr_ref", "pr_results", "track_id", "verdict"]),
            ("T-gold-b", "deferred", ["pr_ref", "track_id", "verdict"]),
        ],
    },
    "no_candidates": {
        "tracks": [("T-gold-done", "done", "#900"), ("T-gold-none", "active", "")],
        "gh_available": True, "merged": [900], "kwargs": {},
        "gh_health": "ok", "exit_code": 0,
        "counts": _counts(tracks=2),
        "per_track": [],
    },
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_run_reconcile_summary_golden(tmp_path, monkeypatch, name):
    case = _CASES[name]
    sd = _build_db(tmp_path)
    for track_id, phase, pr_ref in case["tracks"]:
        _seed_track(sd, track_id, phase=phase, pr_ref=pr_ref)
    if case["gh_available"]:
        gh_run = _make_gh_mock({pn: _merged_pr(pn) for pn in case["merged"]})
    else:
        gh_run = _absent_gh
    monkeypatch.setattr(objective_reconcile.subprocess, "run", gh_run)

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False, **case["kwargs"],
    )

    assert code == case["exit_code"]
    assert summary["evidence_source_health"]["gh"] == case["gh_health"]
    assert sorted(summary) == _SUMMARY_KEYS
    assert sorted(summary["counts"]) == _COUNTS_KEYS
    assert summary["counts"] == case["counts"]
    assert [
        (e["track_id"], e["verdict"], sorted(e)) for e in summary["per_track"]
    ] == case["per_track"]
