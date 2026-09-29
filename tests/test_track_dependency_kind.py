"""tests/test_track_dependency_kind.py — D9 fabric-state-herstel.

The reconciler respects the dependency kind: only ``hard`` blocks. ``soft`` and
``overlap`` are advice. An unrecognized kind (NULL / old value in a store with
an older schema) counts as ``hard`` (fail-closed) and logs a warning.

Also covers the edge-removal API + ``objective unlink-dep`` CLI verb.

ADR-007: every scenario has a second project with colliding track ids whose
edges must not leak into the first project's result.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planning_cli  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402
from test_close_track_if_done import (  # noqa: E402
    PROJECT_ID,
    _build_db,
    _phase,
    _seed_done_track,
    _set_delivery,
)

OTHER = "other-proj"


def _mk(state_dir: Path, track_id: str, project_id: str, phase: str = "queued") -> None:
    tracks_lib.create_track(
        state_dir, track_id, project_id, title=track_id, goal_state="ship", phase=phase
    )


def _edge(state_dir: Path, frm: str, to: str, kind: str, project_id: str = PROJECT_ID) -> None:
    tracks_lib.add_dependency(
        state_dir, frm, project_id, to, project_id, kind=kind, derivation_source="manual"
    )


def _derived(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> str:
    return track_reconciler.reconcile_track(state_dir, track_id, project_id)["derived_status"]


def _edges(state_dir: Path) -> set:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return {
            tuple(r)
            for r in conn.execute(
                "SELECT from_track_id, from_project_id, to_track_id, to_project_id "
                "FROM track_dependencies"
            )
        }
    finally:
        conn.close()


def _relax_kind_constraint(state_dir: Path) -> None:
    """Rebuild track_dependencies the way an older-schema store might carry it:
    no CHECK on kind, kind nullable."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE track_dependencies RENAME TO _td_old")
    conn.execute(
        """
        CREATE TABLE track_dependencies (
            from_track_id TEXT NOT NULL, from_project_id TEXT NOT NULL,
            to_track_id TEXT NOT NULL, to_project_id TEXT NOT NULL,
            kind TEXT, derivation_source TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0, evidence_json TEXT,
            derived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (from_track_id, from_project_id, to_track_id, to_project_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO track_dependencies SELECT from_track_id, from_project_id, "
        "to_track_id, to_project_id, kind, derivation_source, confidence, "
        "evidence_json, derived_at FROM _td_old"
    )
    conn.execute("DROP TABLE _td_old")
    conn.commit()
    conn.close()


@pytest.fixture
def sd(tmp_path):
    return _build_db(tmp_path)


# ---------------------------------------------------------------------------
# derived status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["overlap", "soft"])
def test_advisory_edge_to_unfinished_track_does_not_block(sd, kind):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", kind)
    assert _derived(sd, "A") == "queued"


def test_hard_edge_to_unfinished_track_still_blocks(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "hard")
    assert _derived(sd, "A") == "blocked"


def test_hard_edge_blocks_even_next_to_advisory_edge(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _mk(sd, "C", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "overlap")
    _edge(sd, "A", "C", "hard")
    assert _derived(sd, "A") == "blocked"


def test_unknown_kind_blocks_and_warns(sd, caplog):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _mk(sd, "C", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "hard")
    _edge(sd, "A", "C", "hard")
    _relax_kind_constraint(sd)
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("UPDATE track_dependencies SET kind = 'strict' WHERE to_track_id = 'B'")
    conn.execute("UPDATE track_dependencies SET kind = NULL WHERE to_track_id = 'C'")
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        assert _derived(sd, "A") == "blocked"
    assert any("unrecognized kind" in r.getMessage() for r in caplog.records)


def test_unknown_kind_on_single_edge_blocks(sd, caplog):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "hard")
    _relax_kind_constraint(sd)
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("UPDATE track_dependencies SET kind = NULL")
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        assert _derived(sd, "A") == "blocked"
    assert any("unrecognized kind" in r.getMessage() for r in caplog.records)


def test_other_project_hard_edge_does_not_leak(sd):
    """Same ids in a second project: its hard edge must not block project one."""
    for pid in (PROJECT_ID, OTHER):
        _mk(sd, "A", pid)
        _mk(sd, "B", pid, phase="active")
    _edge(sd, "A", "B", "overlap", PROJECT_ID)
    _edge(sd, "A", "B", "hard", OTHER)
    assert _derived(sd, "A", PROJECT_ID) == "queued"
    assert _derived(sd, "A", OTHER) == "blocked"


def test_blocking_detail_splits_hard_and_advisory(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _mk(sd, "C", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "overlap")
    _edge(sd, "A", "C", "hard")
    conn = track_reconciler._get_conn(sd)
    try:
        detail = track_reconciler._blocking_detail(conn, "A", PROJECT_ID)
    finally:
        conn.close()
    assert [d["track_id"] for d in detail["blocking_deps"]] == ["C"]
    assert [d["track_id"] for d in detail["advisory_deps"]] == ["B"]
    assert detail["advisory_deps"][0]["kind"] == "overlap"


# ---------------------------------------------------------------------------
# auto-close
# ---------------------------------------------------------------------------

def test_soft_edge_does_not_stop_close(sd):
    _seed_done_track(sd, "A", phase="active")
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "soft")
    result = track_reconciler.close_track_if_done(sd, "A", PROJECT_ID, actor="system")
    assert result["action"] == "closed"
    assert _phase(sd, "A") == "done"


def test_hard_edge_still_stops_close(sd):
    _seed_done_track(sd, "A", phase="active")
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "hard")
    result = track_reconciler.close_track_if_done(sd, "A", PROJECT_ID, actor="system")
    assert result["action"] == "noop_not_terminal"
    assert _phase(sd, "A") == "active"


def _gh_evidence(pr: int) -> dict:
    return {
        "pr_ref": f"#{pr}",
        "pr_results": [{"number": pr, "state": "MERGED", "mergedAt": "2026-09-29T10:00:00Z"}],
        "verified_at": "2026-09-29T10:00:00Z",
    }


def _gh_track(sd: Path, track_id: str, pr: int) -> None:
    tracks_lib.create_track(
        sd, track_id, PROJECT_ID, title=track_id, goal_state="ship", phase="active",
        pr_ref=f"#{pr}",
    )
    _set_delivery(sd, track_id, pr, "complete")


@pytest.mark.parametrize("kind", ["soft", "overlap"])
def test_gh_evidence_close_ignores_advisory_edge(sd, kind):
    _gh_track(sd, "A", 901)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", kind)
    result = track_reconciler.close_track_if_done(
        sd, "A", PROJECT_ID, actor="system", evidence=_gh_evidence(901)
    )
    assert result["action"] == "closed"


def test_gh_evidence_close_stale_on_hard_edge(sd):
    _gh_track(sd, "A", 902)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "hard")
    result = track_reconciler.close_track_if_done(
        sd, "A", PROJECT_ID, actor="system", evidence=_gh_evidence(902)
    )
    assert result["action"] == "stale_candidate"
    assert _phase(sd, "A") == "active"


# ---------------------------------------------------------------------------
# drift reason (planning_cli)
# ---------------------------------------------------------------------------

def test_drift_reason_names_only_hard_dependency(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _mk(sd, "C", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "overlap")
    _edge(sd, "A", "C", "hard")
    reason = planning_cli._drift_reason(sd, "A", PROJECT_ID, "queued", "blocked")
    assert reason == "blocked by dependency: C"


# ---------------------------------------------------------------------------
# edge removal API + CLI
# ---------------------------------------------------------------------------

def _events(sd: Path) -> list:
    path = sd.parent / "events" / "track_events.ndjson"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_remove_dependency_only_touches_the_named_project(sd):
    for pid in (PROJECT_ID, OTHER):
        _mk(sd, "A", pid)
        _mk(sd, "B", pid, phase="active")
        _edge(sd, "A", "B", "hard", pid)
    tracks_lib.remove_dependency(
        sd, "A", PROJECT_ID, "B", PROJECT_ID, reason="advisory only"
    )
    assert _edges(sd) == {("A", OTHER, "B", OTHER)}
    assert _derived(sd, "A", PROJECT_ID) == "queued"
    assert _derived(sd, "A", OTHER) == "blocked"


def test_remove_dependency_emits_track_event(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID, phase="active")
    _edge(sd, "A", "B", "overlap")
    tracks_lib.remove_dependency(
        sd, "A", PROJECT_ID, "B", PROJECT_ID, reason="wrong edge", actor="T0"
    )
    ev = [e for e in _events(sd) if e["event_type"] == "track_dep_removed"]
    assert len(ev) == 1
    assert ev[0]["track_id"] == "A"
    assert ev[0]["project_id"] == PROJECT_ID
    assert ev[0]["actor"] == "T0"
    assert ev[0]["details"] == {
        "to_track": "B", "to_project": PROJECT_ID, "kind": "overlap", "reason": "wrong edge",
    }


def test_remove_missing_dependency_raises_and_writes_nothing(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID)
    _mk(sd, "A", OTHER)
    _mk(sd, "B", OTHER)
    _edge(sd, "A", "B", "hard", OTHER)
    with pytest.raises(tracks_lib.DependencyNotFoundError):
        tracks_lib.remove_dependency(sd, "A", PROJECT_ID, "B", PROJECT_ID, reason="x")
    assert _edges(sd) == {("A", OTHER, "B", OTHER)}
    events_path = sd.parent / "events" / "track_events.ndjson"
    if events_path.exists():
        assert not [e for e in _events(sd) if e["event_type"] == "track_dep_removed"]


def test_remove_dependency_requires_reason(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID)
    _edge(sd, "A", "B", "hard")
    with pytest.raises(ValueError):
        tracks_lib.remove_dependency(sd, "A", PROJECT_ID, "B", PROJECT_ID, reason="  ")
    assert _edges(sd) == {("A", PROJECT_ID, "B", PROJECT_ID)}


def test_cli_unlink_dep_removes_edge(sd, capsys):
    for pid in (PROJECT_ID, OTHER):
        _mk(sd, "A", pid)
        _mk(sd, "B", pid, phase="active")
        _edge(sd, "A", "B", "overlap", pid)
    rc = planning_cli.main([
        "objective", "unlink-dep", "A", "B",
        "--project-id", PROJECT_ID, "--state-dir", str(sd),
        "--reason", "overlap is advice",
    ])
    assert rc == 0
    assert _edges(sd) == {("A", OTHER, "B", OTHER)}
    assert "unlink-dep" in capsys.readouterr().out


def test_cli_unlink_dep_cross_project_target(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", OTHER, phase="active")
    tracks_lib.add_dependency(
        sd, "A", PROJECT_ID, "B", OTHER, kind="hard", derivation_source="manual"
    )
    rc = planning_cli.main([
        "objective", "unlink-dep", "A", "B",
        "--project-id", PROJECT_ID, "--to-project-id", OTHER, "--state-dir", str(sd),
        "--reason", "cross edge",
    ])
    assert rc == 0
    assert _edges(sd) == set()


def test_cli_unlink_dep_refuses_empty_reason(sd, capsys):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID)
    _edge(sd, "A", "B", "hard")
    rc = planning_cli.main([
        "objective", "unlink-dep", "A", "B",
        "--project-id", PROJECT_ID, "--state-dir", str(sd), "--reason", "",
    ])
    assert rc == 2
    assert _edges(sd) == {("A", PROJECT_ID, "B", PROJECT_ID)}


def test_cli_unlink_dep_missing_edge_exits_1(sd):
    _mk(sd, "A", PROJECT_ID)
    _mk(sd, "B", PROJECT_ID)
    rc = planning_cli.main([
        "objective", "unlink-dep", "A", "B",
        "--project-id", PROJECT_ID, "--state-dir", str(sd), "--reason", "gone",
    ])
    assert rc == 1
