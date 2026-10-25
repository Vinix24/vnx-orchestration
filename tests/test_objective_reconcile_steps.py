"""tests/test_objective_reconcile_steps.py — unit tests for the S4 extraction.

``objective_reconcile_steps`` holds steps 2, 2b and 3 that used to live inline
in ``run_reconcile``. These tests exercise each function directly (the moved
bodies are otherwise covered end-to-end by ``test_objective_reconcile.py``):

- ``refresh_derived``: success returns the reconciler results and ``None``; a
  ``RuntimeError`` returns the persisted error summary plus exit 2.
- ``recompute_dispatch_outcomes``: a failing classifier is captured in
  ``error``, never raised, and the counters stay at zero.
- ``nominate_candidates``: a delivery hold, an unparseable reopen stamp and an
  unchanged pr_ref are the three ways a track fails to become a candidate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import objective_reconcile_steps  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from test_objective_reconcile import (  # noqa: E402
    PROJECT_ID,
    _build_db,
    _seed_track,
    _set_delivery,
)


# ---------------------------------------------------------------------------
# refresh_derived (step 2)
# ---------------------------------------------------------------------------

def test_refresh_derived_returns_results_and_no_failure(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    fake_results = [{"track_id": "T-a", "derived_status": "done"}]
    monkeypatch.setattr(
        track_reconciler, "reconcile_all_tracks",
        lambda state_dir, project_id, repo_root=None: fake_results,
    )

    results, failure = objective_reconcile_steps.refresh_derived(
        sd, PROJECT_ID, tmp_path,
        run_id="run-ok", mode="check", started_at="2026-10-25T07:00:00Z",
        provenance={"scanned": 1, "linked": 1},
    )

    assert results == fake_results
    assert failure is None


def test_refresh_derived_runtime_error_persists_summary_and_exit_2(tmp_path, monkeypatch):
    """The error path is exactly today's: same keys, persisted, exit 2."""
    sd = _build_db(tmp_path)

    def _boom(state_dir, project_id, repo_root=None):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(track_reconciler, "reconcile_all_tracks", _boom)

    results, failure = objective_reconcile_steps.refresh_derived(
        sd, PROJECT_ID, tmp_path,
        run_id="run-err", mode="apply", started_at="2026-10-25T07:00:00Z",
        provenance={"scanned": 3, "linked": 2},
    )

    assert results == []
    assert failure is not None
    summary, exit_code = failure
    assert exit_code == 2
    assert summary["run_id"] == "run-err"
    assert summary["project_id"] == PROJECT_ID
    assert summary["mode"] == "apply"
    assert summary["started_at"] == "2026-10-25T07:00:00Z"
    assert summary["error"] == "state store unavailable"
    assert summary["evidence_source_health"] == {"gh": "unknown"}
    assert summary["provenance"] == {"scanned": 3, "linked": 2}
    assert summary["per_track"] == []
    assert summary["counts"] == {
        "tracks": 0, "nominated": 0, "confirmed": 0, "closed": 0,
        "closed_sibling": 0, "open_pr": 0, "unverified": 0, "deferred": 0,
        "stale": 0, "delivery_hold": 0,
    }

    # Same _persist_summary call as today: summary file + one history record.
    persisted = json.loads((sd / "reconcile_summary.json").read_text(encoding="utf-8"))
    assert persisted["error"] == "state store unavailable"
    history = [
        json.loads(line)
        for line in (sd / "reconcile_history.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(history) == 1
    assert history[0]["run_id"] == "run-err"
    assert history[0]["error"] == "state store unavailable"


# ---------------------------------------------------------------------------
# recompute_dispatch_outcomes (step 2b)
# ---------------------------------------------------------------------------

def test_recompute_dispatch_outcomes_counts_outcomes(tmp_path, monkeypatch):
    import dispatch_outcome_classifier

    sd = _build_db(tmp_path)
    monkeypatch.setattr(
        dispatch_outcome_classifier, "reconcile_all_dispatch_outcomes",
        lambda state_dir, data_dir, project_id, repo_root=None: [
            {"dispatch_id": "d1", "outcome": "merged-PR"},
            {"dispatch_id": "d2", "outcome": None},
        ],
    )

    summary = objective_reconcile_steps.recompute_dispatch_outcomes(
        sd, PROJECT_ID, tmp_path
    )

    assert summary == {"closed": 1, "total": 2, "error": None}


def test_recompute_dispatch_outcomes_captures_classifier_failure(tmp_path, monkeypatch):
    """A failing classifier is advisory only: captured in error, never raised."""
    import dispatch_outcome_classifier

    sd = _build_db(tmp_path)

    def _boom(state_dir, data_dir, project_id, repo_root=None):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(
        dispatch_outcome_classifier, "reconcile_all_dispatch_outcomes", _boom
    )

    summary = objective_reconcile_steps.recompute_dispatch_outcomes(
        sd, PROJECT_ID, tmp_path
    )

    assert summary == {"closed": 0, "total": 0, "error": "classifier exploded"}


# ---------------------------------------------------------------------------
# nominate_candidates (step 3)
# ---------------------------------------------------------------------------

def test_nominate_candidates_holds_track_with_partial_delivery(tmp_path):
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-free", phase="active", pr_ref="#200")
    _set_delivery(sd, "T-free", 200, "complete")
    _seed_track(sd, "T-held", phase="active", pr_ref="#201")
    _set_delivery(sd, "T-held", 201, "partial")

    candidates, guarded_tracks, held_tracks = objective_reconcile_steps.nominate_candidates(
        sd, PROJECT_ID
    )

    assert [c["track_id"] for c in candidates] == ["T-free"]
    assert candidates[0]["pr_numbers"] == frozenset({200})
    assert guarded_tracks == []
    assert [h["track_id"] for h in held_tracks] == ["T-held"]
    assert held_tracks[0]["verdict"] == "delivery_hold"
    assert held_tracks[0]["pr_ref"] == "#201"
    assert "delivery hold" in held_tracks[0]["reason"]


def test_nominate_candidates_guards_unparseable_reopen_stamp(tmp_path):
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-badstamp", phase="active", pr_ref="#300")
    tracks_lib.transition_phase(sd, "T-badstamp", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-badstamp", PROJECT_ID, "active",
        actor="operator",
        reason="manually reopened without proper stamp format",
        approval_id="appr-badstamp-001",
    )

    candidates, guarded_tracks, held_tracks = objective_reconcile_steps.nominate_candidates(
        sd, PROJECT_ID
    )

    assert candidates == []
    assert held_tracks == []
    assert [g["track_id"] for g in guarded_tracks] == ["T-badstamp"]
    assert guarded_tracks[0]["verdict"] == "reopened_guard"
    assert guarded_tracks[0]["reason"] == "unparseable_reopen_stamp"


def test_nominate_candidates_guards_unchanged_pr_ref_since_reopen(tmp_path):
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-unchanged", phase="active", pr_ref="#400")
    tracks_lib.transition_phase(sd, "T-unchanged", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-unchanged", PROJECT_ID, "active",
        actor="operator",
        reason='reopen pr_ref="#400" | reopened unchanged',
        approval_id="appr-unchanged-001",
    )

    candidates, guarded_tracks, held_tracks = objective_reconcile_steps.nominate_candidates(
        sd, PROJECT_ID
    )

    assert candidates == []
    assert held_tracks == []
    assert [g["track_id"] for g in guarded_tracks] == ["T-unchanged"]
    assert guarded_tracks[0]["reason"] == "pr_ref_unchanged_since_reopen"


def test_nominate_candidates_rearmed_when_pr_ref_changed_and_skips_done(tmp_path):
    """A changed pr_ref re-arms the track; done/parked tracks are never nominated."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rearmed", phase="active", pr_ref="#500")
    tracks_lib.transition_phase(sd, "T-rearmed", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-rearmed", PROJECT_ID, "active",
        actor="operator",
        reason='reopen pr_ref="#500" | follow-up work',
        approval_id="appr-rearmed-001",
    )
    tracks_lib.update_authored_fields(
        sd, "T-rearmed", PROJECT_ID, pr_ref="#501", actor="operator",
    )
    _seed_track(sd, "T-done", phase="done", pr_ref="#502")
    _seed_track(sd, "T-parked", phase="parked", pr_ref="#503")

    candidates, guarded_tracks, held_tracks = objective_reconcile_steps.nominate_candidates(
        sd, PROJECT_ID
    )

    assert [c["track_id"] for c in candidates] == ["T-rearmed"]
    assert candidates[0]["pr_ref"] == "#501"
    assert guarded_tracks == []
    assert held_tracks == []
