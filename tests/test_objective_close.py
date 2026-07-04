"""tests/test_objective_close.py — close-the-loop `vnx objective close`.

Self-contained. Verifies the human-gated drift-resolver that advances a track's
declared phase to a terminal derived_status:
- dry-run (default) never writes
- non-terminal derived_status is a no-op
- --apply without --approval-id is rejected fail-closed (no write)
- --apply --approval-id walks the LEGAL path to done (active->done, queued->active->done)
  via the single-writer, leaving a phase_history audit trail
- a track already at done is a no-op (idempotent)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import planning_cli  # noqa: E402
import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402

PROJECT_ID = "test-proj"


def _build_db(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL, project_id TEXT
        )
        """
    )
    conn.commit()
    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for ver, fname in ((27, "0027_planning_horizon_and_deliverable_view.sql"),
                       (28, "0028_tracks_derived_status.sql")):
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    conn.close()
    return state_dir


def _seed_done_track(state_dir: Path, track_id: str, *, phase: str) -> None:
    """Track whose work is terminal (completed dispatch, no pr_ref) => derived 'done'."""
    tracks_lib.create_track(state_dir, track_id, PROJECT_ID,
                            title=track_id, goal_state="ship", phase=phase)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (f"D-{track_id}", PROJECT_ID, "completed", track_id),
    )
    conn.commit()
    conn.close()


def _phase(state_dir: Path, track_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute("SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
                       (track_id, PROJECT_ID)).fetchone()
    conn.close()
    return row[0] if row else ""


def _history_count(state_dir: Path, track_id: str) -> int:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    n = conn.execute("SELECT count(*) FROM track_phase_history WHERE track_id=? AND project_id=?",
                     (track_id, PROJECT_ID)).fetchone()[0]
    conn.close()
    return n


def _args(state_dir: Path, track_id: str, *, apply=False, approval_id="",
          include_parked=False) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir), project_id=PROJECT_ID, track_id=track_id,
        apply=apply, approval_id=approval_id, json=False, include_parked=include_parked,
    )


def test_dry_run_does_not_change_phase(tmp_path, capsys):
    # Dry-run reconciles derived_status (advisory, exactly like `objective drift`)
    # but NEVER touches the declared phase — that is the meaningful guarantee.
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-active", phase="active")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-active"))
    assert rc == 0
    assert "dry-run" in capsys.readouterr().out
    assert _phase(sd, "T-active") == "active"  # phase unchanged


def test_non_terminal_derived_is_noop(tmp_path, capsys):
    sd = _build_db(tmp_path)
    # No dispatches => derived 'queued' (not terminal) => nothing to close.
    tracks_lib.create_track(sd, "T-queued", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-queued", apply=True, approval_id="OK-1"))
    assert rc == 0
    assert "not terminal" in capsys.readouterr().out
    assert _phase(sd, "T-queued") == "queued"


def test_apply_without_approval_is_rejected(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-active", phase="active")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-active", apply=True, approval_id=""))
    assert rc == 2
    assert "approval-id" in capsys.readouterr().out
    assert _phase(sd, "T-active") == "active"  # no write


def test_close_active_to_done(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-active", phase="active")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-active", apply=True, approval_id="APR-1"))
    assert rc == 0
    assert _phase(sd, "T-active") == "done"
    assert _history_count(sd, "T-active") == 1


def test_close_queued_walks_legal_path(tmp_path):
    sd = _build_db(tmp_path)
    # queued -> done is illegal directly; close must walk queued->active->done.
    _seed_done_track(sd, "T-queued", phase="queued")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-queued", apply=True, approval_id="APR-2"))
    assert rc == 0
    assert _phase(sd, "T-queued") == "done"
    assert _history_count(sd, "T-queued") == 2  # queued->active, active->done


def test_parked_is_guarded_without_include_flag(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-parked", phase="parked")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-parked", apply=True, approval_id="P"))
    assert rc == 2
    assert "parked" in capsys.readouterr().out.lower()
    assert _phase(sd, "T-parked") == "parked"  # not un-parked


def test_close_parked_walks_three_steps_with_include_flag(tmp_path):
    sd = _build_db(tmp_path)
    # parked -> done is parked->queued->active->done (3 legal steps); needs the flag.
    _seed_done_track(sd, "T-parked", phase="parked")
    rc = planning_cli.cmd_objective_close(
        _args(sd, "T-parked", apply=True, approval_id="APR-P", include_parked=True))
    assert rc == 0
    assert _phase(sd, "T-parked") == "done"
    assert _history_count(sd, "T-parked") == 3


def test_already_done_is_noop(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-done", phase="done")
    rc = planning_cli.cmd_objective_close(_args(sd, "T-done", apply=True, approval_id="APR-3"))
    assert rc == 0
    assert "already closed" in capsys.readouterr().out
    assert _history_count(sd, "T-done") == 0  # no transition


def test_partial_walk_failure_leaves_intermediate_and_is_resumable(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-q", phase="queued")  # queued->active->done
    real = planning_cli.tracks_lib.transition_phase

    def flaky(state_dir, tid, pid, to_phase, **kw):
        if to_phase == "done":  # fail the 2nd step
            raise planning_cli.tracks_lib.InvalidTransitionError("injected mid-walk failure")
        return real(state_dir, tid, pid, to_phase, **kw)

    monkeypatch.setattr(planning_cli.tracks_lib, "transition_phase", flaky)
    rc = planning_cli.cmd_objective_close(_args(sd, "T-q", apply=True, approval_id="A"))
    assert rc == 2
    assert _phase(sd, "T-q") == "active"  # non-atomic: stuck at the intermediate phase

    # Recovery: the walk re-computes from the CURRENT phase, so a re-run resumes.
    monkeypatch.setattr(planning_cli.tracks_lib, "transition_phase", real)
    rc2 = planning_cli.cmd_objective_close(_args(sd, "T-q", apply=True, approval_id="A"))
    assert rc2 == 0
    assert _phase(sd, "T-q") == "done"


def test_evidence_warns_when_done_has_no_success_signal(tmp_path, capsys):
    # A track whose only dispatch EXPIRED (failure) + no pr_ref still derives
    # 'done' (all-terminal). The operator must see that there is no success
    # signal before closing.
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-failed", PROJECT_ID, title="x", goal_state="y", phase="active")
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
                 ("D-exp", PROJECT_ID, "expired", "T-failed"))
    conn.commit()
    conn.close()

    ev = planning_cli._close_evidence(sd, "T-failed", PROJECT_ID)
    assert ev["failed_terminal"] == 1 and ev["completed"] == 0
    assert ev["has_success_signal"] is False

    rc = planning_cli.cmd_objective_close(_args(sd, "T-failed"))  # dry-run
    out = capsys.readouterr().out.lower()
    assert rc == 0
    assert "warning" in out and "no success signal" in out


def test_evidence_has_success_signal_for_completed(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-ok", phase="active")  # a completed dispatch
    ev = planning_cli._close_evidence(sd, "T-ok", PROJECT_ID)
    assert ev["completed"] == 1 and ev["has_success_signal"] is True


def test_evidence_success_signal_from_merged_pr_without_completed_dispatch(tmp_path):
    # A merged PR is a success signal even when no dispatch shows 'completed'
    # (the dispatch expired but the PR landed). Must NOT false-warn.
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-pr", PROJECT_ID, title="x", goal_state="y",
                            phase="active", pr_ref="PR-500")
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) VALUES (?,?,?,?,?)",
                 ("D-exp", PROJECT_ID, "expired", "T-pr", "PR-500"))
    conn.execute(
        "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES (?, 'pr_merged', 'dispatch', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
        ("ev-1", "D-exp", PROJECT_ID),
    )
    conn.commit()
    conn.close()
    ev = planning_cli._close_evidence(sd, "T-pr", PROJECT_ID)
    assert ev["completed"] == 0 and ev["pr_merged"] is True
    assert ev["has_success_signal"] is True


def test_phase_path_unreachable_returns_none():
    # 'done' is terminal (no outgoing transitions) so it can reach nothing.
    assert planning_cli._phase_path_to("done", "active") is None
    assert planning_cli._phase_path_to("active", "active") == []
    assert planning_cli._phase_path_to("queued", "done") == ["active", "done"]


def _write_pr_merged_ndjson(state_dir: Path, pr_numbers: list) -> None:
    """Seed pr_merged.ndjson so _load_merged_pr_numbers sees the given PR numbers."""
    import json as _json
    events_dir = state_dir.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    lines = [_json.dumps({"event_type": "pr_merged", "pr_number": n}) for n in pr_numbers]
    (events_dir / "pr_merged.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_evidence_multi_pr_both_merged_signals_success(tmp_path):
    # Multi-PR pr_ref '#908,#909' with BOTH in the merged set → pr_merged=True,
    # has_success_signal=True (mirrors _compute_derived_status subset semantics).
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-multi", PROJECT_ID, title="x", goal_state="y",
                            phase="active", pr_ref="#908,#909")
    _write_pr_merged_ndjson(sd, [908, 909])

    ev = planning_cli._close_evidence(sd, "T-multi", PROJECT_ID)
    assert ev["pr_merged"] is True
    assert ev["has_success_signal"] is True


def test_evidence_multi_pr_partial_merge_no_success_signal(tmp_path):
    # Multi-PR pr_ref '#908,#909' with only ONE merged → pr_merged stays False;
    # the subset check requires ALL PRs to be merged (not just any).
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-partial", PROJECT_ID, title="x", goal_state="y",
                            phase="active", pr_ref="#908,#909")
    _write_pr_merged_ndjson(sd, [908])  # 909 not merged

    ev = planning_cli._close_evidence(sd, "T-partial", PROJECT_ID)
    assert ev["pr_merged"] is False
    assert ev["has_success_signal"] is False
