"""OI-2904: reconcile must not confirm a track that still has open deliverables."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import objective_reconcile  # noqa: E402
import track_reconciler  # noqa: E402
from test_objective_reconcile import (  # noqa: E402
    PROJECT_ID, _build_db, _make_gh_mock, _merged_pr, _seed_track,
)

TRACK = "T-open-dlv"


def _add_dispatch(sd: Path, dispatch_id: str, state: str, *, deliverable: bool) -> None:
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, output_kind) "
        "VALUES (?,?,?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, TRACK,
         f"ref-{dispatch_id}" if deliverable else None, "pr" if deliverable else None),
    )
    conn.commit()
    conn.close()


def _run(tmp_path, monkeypatch, rows):
    sd = _build_db(tmp_path)
    _seed_track(sd, TRACK, pr_ref="#900")
    for i, (state, deliverable) in enumerate(rows):
        _add_dispatch(sd, f"d{i}", state, deliverable=deliverable)
    monkeypatch.setattr(objective_reconcile.subprocess, "run", _make_gh_mock({900: _merged_pr(900)}))
    summary, code = objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path)
    entry = next(e for e in summary["per_track"] if e["track_id"] == TRACK)
    return summary, code, entry


@pytest.mark.parametrize("state", ["proposed", "ready", "failed", "failed_delivery", "timed_out"])
def test_open_deliverable_state_is_not_confirmed(tmp_path, monkeypatch, state):
    summary, code, entry = _run(tmp_path, monkeypatch, [(state, True)])
    assert entry["verdict"] == "open_deliverable"
    assert entry["reason"] == f"open_deliverable: 1 {state}"
    assert summary["counts"]["open_deliverable"] == 1
    assert summary["counts"]["confirmed"] == 0
    assert code == 0


@pytest.mark.parametrize("state", ["claimed", "delivering", "accepted", "running"])
def test_in_flight_dispatch_is_not_confirmed(tmp_path, monkeypatch, state):
    summary, _, entry = _run(tmp_path, monkeypatch, [(state, False)])
    assert entry["verdict"] == "open_deliverable"
    assert summary["counts"]["confirmed"] == 0


def test_reason_counts_per_state(tmp_path, monkeypatch):
    _, _, entry = _run(
        tmp_path, monkeypatch,
        [("ready", True), ("ready", True), ("proposed", True), ("proposed", True), ("proposed", True)],
    )
    assert entry["reason"] == "open_deliverable: 3 proposed, 2 ready"


def test_all_finished_is_confirmed(tmp_path, monkeypatch):
    summary, code, entry = _run(
        tmp_path, monkeypatch,
        [("completed", True), ("expired", True), ("failed", False)],
    )
    assert entry["verdict"] == "CONFIRMED"
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["open_deliverable"] == 0
    assert code == 0


def test_apply_never_closes_open_deliverable_track(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _seed_track(sd, TRACK, pr_ref="#900")
    _add_dispatch(sd, "d0", "ready", deliverable=True)
    monkeypatch.setattr(objective_reconcile.subprocess, "run", _make_gh_mock({900: _merged_pr(900)}))
    summary, _ = objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path, apply=True)
    assert summary["counts"]["closed"] == 0
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    phase = conn.execute("SELECT phase FROM tracks WHERE track_id=?", (TRACK,)).fetchone()[0]
    conn.close()
    assert phase != "done"


def test_unreadable_state_is_unverified_exit_3(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(track_reconciler, "open_deliverable_counts", boom)
    sd = _build_db(tmp_path)
    _seed_track(sd, TRACK, pr_ref="#900")
    monkeypatch.setattr(objective_reconcile.subprocess, "run", _make_gh_mock({900: _merged_pr(900)}))
    summary, code = objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path)
    entry = next(e for e in summary["per_track"] if e["track_id"] == TRACK)
    assert entry["verdict"] == "unverified"
    assert summary["counts"]["unverified"] == 1
    assert summary["counts"]["confirmed"] == 0
    assert code == 3
