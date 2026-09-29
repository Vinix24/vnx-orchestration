"""tests/test_reconciler_dependency_kind.py — D9 fabric-state-herstel.

The reconciler used to ignore ``track_dependencies.kind``: a ``soft`` or
``overlap`` edge to an unfinished track blocked exactly like a ``hard`` one,
also for auto-close. This file pins the decided contract:

- Only ``hard`` blocks. ``soft`` and ``overlap`` ride the result as advice
  (``advisory_deps``), never as a block.
- A kind that is not hard/soft/overlap (NULL or a legacy value in a store with
  an older schema) counts as ``hard``: fail-closed, logged as a WARNING.
- The same rule holds on all three read sites (derived status, blocking
  detail, the auto-close revalidation) plus the drift reason in planning_cli.
- An edge can be removed by its FULL key
  ``(from_track_id, from_project_id, to_track_id, to_project_id)``, with a
  ``track_dep_removed`` event; the removal never touches another project's edge.
- track_freshness counts plan-gated tracks (only OI-PLAN-* blockers) apart
  from drift.

ADR-007: every test seeds a second project with colliding track ids whose
edges must not leak into the first project's answer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_t0_state as bts
import planning_cli
import schema_migration
import track_reconciler
import tracks as tracks_lib

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns

P1 = "proj-one"
P2 = "proj-two"
_MERGED_AT = "2026-09-01T12:00:00Z"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """State dir with migrations 0022+0024+0027+0028+0029+0030+0032."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
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
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL,
            project_id TEXT
        )
    """)
    conn.commit()

    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    for ver, fname in (
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (32, "0032_track_pr_delivery.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return state_dir


def _db(state_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _track(state_dir: Path, track_id: str, project_id: str, *, phase: str = "queued",
           pr_ref: Optional[str] = None) -> None:
    tracks_lib.create_track(
        state_dir, track_id, project_id,
        title=f"Track {track_id}", goal_state=f"ship {track_id}",
        phase=phase, pr_ref=pr_ref,
    )


def _dep(state_dir: Path, a: str, pa: str, b: str, pb: str, kind: str) -> None:
    if kind in ("hard", "soft", "overlap"):
        tracks_lib.add_dependency(state_dir, a, pa, b, pb, kind, "manual")
        return
    # A kind outside the CHECK can only exist in a store with an older/looser
    # schema; bypass the CHECK to reproduce that row.
    conn = _db(state_dir)
    conn.execute("PRAGMA ignore_check_constraints = 1")
    conn.execute(
        "INSERT INTO track_dependencies (from_track_id, from_project_id, to_track_id, "
        "to_project_id, kind, derivation_source) VALUES (?,?,?,?,?,'manual')",
        (a, pa, b, pb, kind),
    )
    conn.commit()
    conn.close()


def _dispatch(state_dir: Path, dispatch_id: str, track_id: str, project_id: str,
              *, state: str = "completed") -> None:
    conn = _db(state_dir)
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (dispatch_id, project_id, state, track_id),
    )
    conn.commit()
    conn.close()


def _merged_event(state_dir: Path, dispatch_id: str, project_id: str) -> None:
    conn = _db(state_dir)
    conn.execute(
        "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, "
        "occurred_at, project_id) VALUES (?, 'pr_merged', 'dispatch', ?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
        (f"ev-{dispatch_id}", dispatch_id, project_id),
    )
    conn.commit()
    conn.close()


def _delivery_complete(state_dir: Path, track_id: str, project_id: str, pr: int) -> None:
    conn = _db(state_dir)
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,'complete','operator')",
        (project_id, track_id, pr),
    )
    conn.commit()
    conn.close()


def _edges(state_dir: Path) -> set:
    conn = _db(state_dir)
    try:
        return {
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT from_track_id, from_project_id, to_track_id, to_project_id "
                "FROM track_dependencies"
            )
        }
    finally:
        conn.close()


def _phase(state_dir: Path, track_id: str, project_id: str) -> str:
    conn = _db(state_dir)
    try:
        return conn.execute(
            "SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
            (track_id, project_id),
        ).fetchone()[0]
    finally:
        conn.close()


def _track_events(state_dir: Path) -> list:
    path = state_dir.parent / "events" / "track_events.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Klaar-punt 1: overlap/soft to an unfinished track no longer blocks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["overlap", "soft"])
def test_advisory_edge_to_unfinished_track_does_not_block(tmp_path, kind):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid)
        _track(sd, "B", pid)
    _dep(sd, "A", P1, "B", P1, kind)
    # Colliding ids in P2 carry a HARD edge: must not leak into P1's answer.
    _dep(sd, "A", P2, "B", P2, "hard")

    r1 = track_reconciler.reconcile_track(sd, "A", P1)
    r2 = track_reconciler.reconcile_track(sd, "A", P2)

    assert r1["derived_status"] == "queued"
    assert "blocking_detail" not in r1
    assert r1["advisory_deps"] == [
        {"track_id": "B", "project_id": P1, "phase": "queued", "kind": kind}
    ]
    assert r2["derived_status"] == "blocked"

    peeked = track_reconciler.peek_derived_status(sd, "A", P1)
    assert peeked["derived_status"] == "queued"
    assert peeked["advisory_deps"][0]["kind"] == kind


# ---------------------------------------------------------------------------
# Klaar-punt 2: a hard edge keeps blocking
# ---------------------------------------------------------------------------

def test_hard_edge_still_blocks_and_advisory_is_split_out(tmp_path):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        for t in ("A", "B", "C"):
            _track(sd, t, pid)
    _dep(sd, "A", P1, "B", P1, "hard")
    _dep(sd, "A", P1, "C", P1, "overlap")
    # P2's A has no edges at all.

    r1 = track_reconciler.reconcile_track(sd, "A", P1)
    r2 = track_reconciler.reconcile_track(sd, "A", P2)

    assert r1["derived_status"] == "blocked"
    detail = r1["blocking_detail"]
    assert [d["track_id"] for d in detail["blocking_deps"]] == ["B"]
    assert [d["track_id"] for d in detail["advisory_deps"]] == ["C"]
    hint = track_reconciler.format_blocking_hint(detail)
    assert "blocked by dependency B" in hint
    assert "blocked by dependency C" not in hint
    assert r2["derived_status"] == "queued"


# ---------------------------------------------------------------------------
# Klaar-punt 3: auto-close closes a track with only a soft edge
# ---------------------------------------------------------------------------

def _closable(sd: Path, pid: str, pr: int) -> None:
    _track(sd, "A", pid, phase="active", pr_ref=f"#{pr}")
    _track(sd, "B", pid)
    _dispatch(sd, f"D-{pid}", "A", pid)
    _merged_event(sd, f"D-{pid}", pid)
    _delivery_complete(sd, "A", pid, pr)


def test_autoclose_closes_track_with_only_soft_edge(tmp_path):
    sd = _build_db(tmp_path)
    _closable(sd, P1, 901)
    _closable(sd, P2, 902)
    _dep(sd, "A", P1, "B", P1, "soft")
    _dep(sd, "A", P2, "B", P2, "hard")

    def _ev(pr):
        return {
            "pr_ref": f"#{pr}",
            "pr_results": [{"number": pr, "state": "MERGED", "mergedAt": _MERGED_AT}],
            "verified_at": _MERGED_AT,
        }

    r1 = track_reconciler.close_track_if_done(sd, "A", P1, actor="system", evidence=_ev(901))
    r2 = track_reconciler.close_track_if_done(sd, "A", P2, actor="system", evidence=_ev(902))

    assert r1["action"] == "closed"
    assert _phase(sd, "A", P1) == "done"
    assert r2["action"] == "stale_candidate"
    assert _phase(sd, "A", P2) == "active"


def test_human_close_path_closes_track_with_only_overlap_edge(tmp_path):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid, phase="active")
        _track(sd, "B", pid)
        _dispatch(sd, f"D-{pid}", "A", pid)
    _dep(sd, "A", P1, "B", P1, "overlap")
    _dep(sd, "A", P2, "B", P2, "hard")

    r1 = track_reconciler.close_track_if_done(sd, "A", P1, actor="operator", approval_id="APR-1")
    r2 = track_reconciler.close_track_if_done(sd, "A", P2, actor="operator", approval_id="APR-2")

    assert r1["action"] == "closed"
    assert r2["action"] == "noop_not_terminal"
    assert _phase(sd, "A", P2) == "active"


# ---------------------------------------------------------------------------
# Klaar-punt 4: removing (A,p1)->(B,p1) leaves (A,p2)->(B,p2) standing
# ---------------------------------------------------------------------------

def test_remove_dependency_touches_only_the_full_key(tmp_path):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid)
        _track(sd, "B", pid)
    _dep(sd, "A", P1, "B", P1, "hard")
    _dep(sd, "A", P2, "B", P2, "hard")
    # A cross-project edge from the same from-track to the same to_track_id.
    _dep(sd, "A", P1, "B", P2, "hard")

    removed = tracks_lib.remove_dependency(
        sd, "A", P1, "B", P1, reason="overlap resolved by D9",
    )

    assert removed == {
        "from_track_id": "A", "from_project_id": P1,
        "to_track_id": "B", "to_project_id": P1, "kind": "hard",
    }
    assert _edges(sd) == {("A", P2, "B", P2), ("A", P1, "B", P2)}
    ev = [e for e in _track_events(sd) if e["event_type"] == "track_dep_removed"]
    assert len(ev) == 1
    assert ev[0]["track_id"] == "A" and ev[0]["project_id"] == P1
    assert ev[0]["details"] == {
        "to_track": "B", "to_project": P1, "kind": "hard",
        "reason": "overlap resolved by D9",
    }


def test_remove_dependency_absent_edge_is_noop_without_event(tmp_path):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid)
        _track(sd, "B", pid)
    _dep(sd, "A", P2, "B", P2, "hard")

    assert tracks_lib.remove_dependency(sd, "A", P1, "B", P1, reason="x") is None
    assert _edges(sd) == {("A", P2, "B", P2)}
    assert not [e for e in _track_events(sd) if e["event_type"] == "track_dep_removed"]


def test_remove_dependency_refuses_empty_reason(tmp_path):
    sd = _build_db(tmp_path)
    _track(sd, "A", P1)
    _track(sd, "B", P1)
    _dep(sd, "A", P1, "B", P1, "hard")
    with pytest.raises(ValueError, match="reason"):
        tracks_lib.remove_dependency(sd, "A", P1, "B", P1, reason="  ")
    assert _edges(sd) == {("A", P1, "B", P1)}


def test_remove_dependency_event_failure_leaves_edge(tmp_path, monkeypatch):
    """ADR-005 emit-first: when the audit append fails, the edge stays."""
    sd = _build_db(tmp_path)
    _track(sd, "A", P1)
    _track(sd, "B", P1)
    _dep(sd, "A", P1, "B", P1, "hard")

    def _boom(*_a, **_k):
        raise OSError("audit sink down")

    monkeypatch.setattr(tracks_lib, "_emit_track_event", _boom)
    with pytest.raises(OSError):
        tracks_lib.remove_dependency(sd, "A", P1, "B", P1, reason="x")
    assert _edges(sd) == {("A", P1, "B", P1)}


def test_cli_remove_dependency_verb(tmp_path, capsys):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid)
        _track(sd, "B", pid)
        _dep(sd, "A", pid, "B", pid, "overlap")

    base = ["objective", "remove-dependency", "A", "B",
            "--project-id", P1, "--state-dir", str(sd), "--json"]

    assert planning_cli.main(base + ["--reason", ""]) == 2
    assert len(_edges(sd)) == 2

    capsys.readouterr()
    assert planning_cli.main(base + ["--reason", "not a real dependency"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "removed" and payload["applied"] is True
    assert payload["kind"] == "overlap"
    assert _edges(sd) == {("A", P2, "B", P2)}

    assert planning_cli.main(base + ["--reason", "again"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop_not_present" and payload["applied"] is False
    assert _edges(sd) == {("A", P2, "B", P2)}


def test_cli_remove_dependency_unknown_track_is_refused(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _track(sd, "A", P2)
    _track(sd, "B", P2)
    rc = planning_cli.main([
        "objective", "remove-dependency", "A", "B", "--project-id", P1,
        "--state-dir", str(sd), "--reason", "x",
    ])
    assert rc == 1


# ---------------------------------------------------------------------------
# Klaar-punt 5: an unknown kind blocks, with a warning
# ---------------------------------------------------------------------------

def test_unknown_kind_blocks_fail_closed_with_warning(tmp_path, caplog):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        _track(sd, "A", pid)
        _track(sd, "B", pid)
    _dep(sd, "A", P1, "B", P1, "legacy-kind")
    _dep(sd, "A", P2, "B", P2, "overlap")

    with caplog.at_level(logging.WARNING, logger="track_reconciler"):
        r1 = track_reconciler.reconcile_track(sd, "A", P1)
        r2 = track_reconciler.reconcile_track(sd, "A", P2)

    assert r1["derived_status"] == "blocked"
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING and "legacy-kind" in rec.getMessage()
    ]
    assert warnings, "unknown dependency kind must be logged as a warning"
    assert all(P1 in w and "'A'" in w for w in warnings)
    assert r2["derived_status"] == "queued"
    assert r1["blocking_detail"]["blocking_deps"][0]["kind"] == "legacy-kind"


def test_unknown_kind_blocks_autoclose(tmp_path):
    sd = _build_db(tmp_path)
    _closable(sd, P1, 901)
    _closable(sd, P2, 902)
    _dep(sd, "A", P1, "B", P1, "legacy-kind")
    _dep(sd, "A", P2, "B", P2, "soft")
    ev = {
        "pr_ref": "#901",
        "pr_results": [{"number": 901, "state": "MERGED", "mergedAt": _MERGED_AT}],
    }
    r1 = track_reconciler.close_track_if_done(sd, "A", P1, actor="system", evidence=ev)
    assert r1["action"] == "stale_candidate"
    assert _phase(sd, "A", P1) == "active"


# ---------------------------------------------------------------------------
# Second read site in planning_cli: the drift reason names a HARD edge
# ---------------------------------------------------------------------------

def test_drift_reason_names_the_hard_edge_not_the_overlap(tmp_path):
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        for t in ("A", "B", "C"):
            _track(sd, t, pid)
    # Overlap inserted first and sorts first: the old unordered LIMIT 1 hit it.
    _dep(sd, "A", P1, "B", P1, "overlap")
    _dep(sd, "A", P1, "C", P1, "hard")
    _dep(sd, "A", P2, "B", P2, "hard")

    reason = planning_cli._drift_reason(sd, "A", P1, "queued", "blocked")
    assert reason == "blocked by dependency: C"
    assert planning_cli._drift_reason(sd, "A", P2, "queued", "blocked") == (
        "blocked by dependency: B"
    )


# ---------------------------------------------------------------------------
# track_freshness: plan-gated counts apart from drift
# ---------------------------------------------------------------------------

def test_track_freshness_counts_plan_gated_apart_from_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(bts, "resolve_central_data_dir", None)
    sd = _build_db(tmp_path)
    for pid in (P1, P2):
        for t in ("T-plan", "T-dep", "T-target"):
            _track(sd, t, pid)
    tracks_lib.link_open_item(sd, "T-plan", P1, "OI-PLAN-T-plan", "blocks", "manual")
    _dep(sd, "T-dep", P1, "T-target", P1, "hard")
    # P2: the same ids are plan-gated/blocked the other way round; must not leak.
    tracks_lib.link_open_item(sd, "T-dep", P2, "OI-PLAN-T-dep", "blocks", "manual")
    tracks_lib.link_open_item(sd, "T-plan", P2, "OI-PLAN-T-plan", "blocks", "manual")
    tracks_lib.link_open_item(sd, "T-target", P2, "OI-PLAN-T-target", "blocks", "manual")

    marker = bts._reconcile_tracks_fresh(sd, P1)

    assert marker["derived_refreshed"] is True
    assert [t["track_id"] for t in marker["drifted_tracks"]] == ["T-dep"]
    assert marker["drifted"] == 1
    assert marker["plan_gated"] == 1
    assert [t["track_id"] for t in marker["plan_gated_tracks"]] == ["T-plan"]

    summary = bts._track_freshness_summary(marker)
    assert summary["plan_gated"] == 1
    assert summary["drifted"] == 1
