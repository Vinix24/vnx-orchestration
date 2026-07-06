"""tests/test_planning_cli.py — D3 escape-hatch CLIs: link-pr + close --attest.

Self-contained synthetic-DB tests for:
- `vnx objective link-pr <track> <pr>[,<pr>...]`
- `vnx objective close <track> --attest "<reason>" --apply --approval-id <id>`
- guarded D5 blocker-hint surface in the normal close path.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

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
    """Create a minimal modern tracks DB (migrations 22/24/27/28)."""
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


def _pr_ref(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    conn.close()
    return (row[0] or "") if row else ""


def _phase(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _build_db_plan_gate(tmp_path: Path) -> Path:
    """Same as `_build_db` but with migration 0030 applied (track_open_items
    .resolved_at / .resolution_reason) — required for the plan-gate blocker
    seed/resolve lifecycle (`_plan_gate_supported`)."""
    state_dir = _build_db(tmp_path)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    schema_migration.apply_script_if_below(
        conn, 30, (_MIGRATIONS / "0030_track_oi_resolved_at.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()
    return state_dir


def _plan_oi_resolved_at(state_dir: Path, track_id: str, project_id: str = PROJECT_ID):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT resolved_at FROM track_open_items "
        "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
        (track_id, project_id, f"OI-PLAN-{track_id}"),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _derived_status(state_dir: Path, track_id: str, project_id: str = PROJECT_ID):
    t = tracks_lib.get_track(state_dir, track_id, project_id)
    return t.get("derived_status") if t else None


def _plan_attest_args(
    state_dir: Path,
    track_id: str,
    *,
    reason: str = "",
    approval_id: str = "",
    project_id: str = PROJECT_ID,
    json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        reason=reason,
        approval_id=approval_id,
        json=json,
    )


def _history_count(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> int:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    n = conn.execute(
        "SELECT count(*) FROM track_phase_history WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()[0]
    conn.close()
    return n


def _track_events(state_dir: Path, track_id: str, event_type: str) -> list[dict]:
    """Read ADR-005 track audit events for a specific track + type."""
    path = state_dir.parent / "events" / "track_events.ndjson"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("track_id") == track_id and rec.get("event_type") == event_type:
            out.append(rec)
    return out


def _link_pr_args(
    state_dir: Path,
    track_id: str,
    *prs: str,
    project_id: str = PROJECT_ID,
    json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        pr=list(prs),
        json=json,
    )


def _close_args(
    state_dir: Path,
    track_id: str,
    *,
    apply: bool = False,
    approval_id: str = "",
    attest: str | None = None,
    include_parked: bool = False,
    project_id: str = PROJECT_ID,
    json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        apply=apply,
        approval_id=approval_id,
        attest=attest,
        include_parked=include_parked,
        json=json,
        repo_root="",
    )


# ---------------------------------------------------------------------------
# link-pr
# ---------------------------------------------------------------------------

def test_link_pr_adds_and_dedupes_preserves_existing(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100"
    )
    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "T", "#397,#398", "#100"))
    assert rc == 0
    assert _pr_ref(sd, "T") == "#100,#397,#398"


def test_link_pr_on_missing_track_is_clean_error(tmp_path, capsys):
    sd = _build_db(tmp_path)
    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "missing", "#1"))
    assert rc == 1
    captured = capsys.readouterr()
    assert "not found" in (captured.out + captured.err)


def test_link_pr_writes_audit_event(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "T", "#397,#398"))
    assert rc == 0
    events = _track_events(sd, "T", "track_pr_linked")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["added"] == ["#397", "#398"]
    assert details["pr_ref"] == "#397,#398"


def test_link_pr_wrong_project_id_does_not_write(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_link_pr(
        _link_pr_args(sd, "T", "#1", project_id="other-proj")
    )
    assert rc == 1
    assert _pr_ref(sd, "T", PROJECT_ID) == ""


# ---------------------------------------------------------------------------
# close --attest
# ---------------------------------------------------------------------------

def test_close_attest_advances_ops_track_and_writes_audit(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "ops", PROJECT_ID, title="fleet sync", goal_state="done", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "ops", apply=True, approval_id="APR-OPS", attest="fleet-sync")
    )
    assert rc == 0
    assert _phase(sd, "ops") == "done"
    assert _pr_ref(sd, "ops").startswith("ops-attest:")
    assert _history_count(sd, "ops") == 2  # queued -> active -> done

    events = _track_events(sd, "ops", "track_ops_attest")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["reason"] == "fleet-sync"
    assert details["approval_id"] == "APR-OPS"
    assert details["pr_ref"].startswith("ops-attest:")


def test_close_attest_without_apply_or_approval_is_rejected(tmp_path, capsys):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "ops", PROJECT_ID, title="x", goal_state="y", phase="queued")

    rc_no_apply = planning_cli.cmd_objective_close(
        _close_args(sd, "ops", apply=False, approval_id="X", attest="reason")
    )
    assert rc_no_apply == 2
    assert _phase(sd, "ops") == "queued"

    rc_no_approval = planning_cli.cmd_objective_close(
        _close_args(sd, "ops", apply=True, approval_id="", attest="reason")
    )
    assert rc_no_approval == 2
    assert _phase(sd, "ops") == "queued"
    assert _pr_ref(sd, "ops") == ""


def test_close_without_attest_still_refuses_non_terminal(tmp_path, capsys):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="X")
    )
    assert rc == 0
    assert "not terminal" in capsys.readouterr().out
    assert _phase(sd, "T") == "queued"


# ---------------------------------------------------------------------------
# D5 blocker hint (guarded)
# ---------------------------------------------------------------------------

def test_close_blocked_renders_blocking_dependency_hint(tmp_path, capsys):
    """When derived_status is blocked, the guarded hint call renders the
    blocker hint now that format_blocking_hint exists (D5)."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "blocked", PROJECT_ID, title="x", goal_state="y", phase="queued")
    tracks_lib.create_track(sd, "dep", PROJECT_ID, title="dep", goal_state="y", phase="queued")
    tracks_lib.add_dependency(
        sd, "blocked", PROJECT_ID, "dep", PROJECT_ID, "hard", "manual"
    )
    # Reconcile so derived_status reflects the blocker.
    import track_reconciler  # noqa: E402
    track_reconciler.reconcile_track(sd, "blocked", PROJECT_ID)

    rc = planning_cli.cmd_objective_close(_close_args(sd, "blocked"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "blocked" in out
    assert "blocked by dependency dep" in out
    assert "not done (phase=queued)" in out


# ---------------------------------------------------------------------------
# plan-gate attest
# ---------------------------------------------------------------------------

def test_plan_gate_attest_resolves_blocker_and_writes_audit(tmp_path):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID) is True
    assert _derived_status(sd, "T") == "blocked"

    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="already shipped+merged pre-gate", approval_id="APR-1")
    )
    assert rc == 0
    assert _plan_oi_resolved_at(sd, "T") is not None
    assert _derived_status(sd, "T") != "blocked"

    events = _track_events(sd, "T", "plan_gate_attest")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["reason"] == "already shipped+merged pre-gate"
    assert details["approval_id"] == "APR-1"
    assert details["track_id"] == "T"


def test_plan_gate_attest_requires_reason_and_approval_id(tmp_path):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)

    rc_no_reason = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="", approval_id="APR-1")
    )
    assert rc_no_reason == 2
    assert _plan_oi_resolved_at(sd, "T") is None

    rc_no_approval = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="shipped", approval_id="")
    )
    assert rc_no_approval == 2
    assert _plan_oi_resolved_at(sd, "T") is None
    assert _track_events(sd, "T", "plan_gate_attest") == []


def test_plan_gate_attest_no_blocker_reports_honestly(tmp_path, capsys):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    # No _seed_plan_blocker call: nothing to resolve.

    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="shipped", approval_id="APR-1")
    )
    assert rc == 1
    assert _track_events(sd, "T", "plan_gate_attest") == []
    assert "no unresolved plan blocker" in capsys.readouterr().out


def test_plan_gate_attest_track_not_found(tmp_path):
    sd = _build_db_plan_gate(tmp_path)
    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "missing", reason="x", approval_id="y")
    )
    assert rc == 1


def test_plan_gate_attest_still_blocked_reports_plainly(tmp_path, capsys):
    """Resolving the plan blocker clears IT, but a hard dependency still blocks —
    attest must report that plainly, not claim a full unblock."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    tracks_lib.create_track(sd, "dep", PROJECT_ID, title="dep", goal_state="y", phase="queued")
    tracks_lib.add_dependency(sd, "T", PROJECT_ID, "dep", PROJECT_ID, kind="hard", derivation_source="manual")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)

    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="shipped", approval_id="APR-1")
    )
    assert rc == 2
    assert _plan_oi_resolved_at(sd, "T") is not None  # the plan blocker itself WAS resolved
    assert _derived_status(sd, "T") == "blocked"  # but still blocked by the dependency
    assert len(_track_events(sd, "T", "plan_gate_attest")) == 1
    assert "STILL" in capsys.readouterr().out
