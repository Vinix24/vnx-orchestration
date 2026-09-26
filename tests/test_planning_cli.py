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

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

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
    ensure_dispatches_columns(conn)
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


def _plan_oi_resolution_reason(state_dir: Path, track_id: str, project_id: str = PROJECT_ID):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT resolution_reason FROM track_open_items "
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


def _plan_missing_reasons_args(
    state_dir: Path,
    *,
    project_id: str = PROJECT_ID,
    json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        json=json,
    )


def _plan_backfill_args(
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


def _plan_reblock_args(
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


def _lift_plan_blocker_reasonless(
    state_dir: Path, track_id: str, resolved_at: str, project_id: str = PROJECT_ID
) -> None:
    """Simulate a pre-fix lift: resolved_at set, resolution_reason dropped."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE track_open_items SET resolved_at = ? "
        "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
        (resolved_at, track_id, project_id, f"OI-PLAN-{track_id}"),
    )
    conn.commit()
    conn.close()


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
    delivery: str = "partial",
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        pr=list(prs),
        json=json,
        delivery=delivery,
    )


def _pr_delivery(state_dir: Path, track_id: str, pr_number: int, project_id: str = PROJECT_ID):
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT delivery_kind FROM track_pr_delivery "
        "WHERE project_id = ? AND track_id = ? AND pr_number = ?",
        (project_id, track_id, pr_number),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _apply_migration_0032(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    schema_migration.apply_script_if_below(
        conn, 32, (_MIGRATIONS / "0032_track_pr_delivery.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()


def _apply_migration_0033(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    schema_migration.apply_script_if_below(
        conn, 33, (_MIGRATIONS / "0033_track_decision_ref.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()


def _show_args(
    state_dir: Path,
    track_id: str,
    *,
    project_id: str = PROJECT_ID,
    json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir), project_id=project_id, track_id=track_id, json=json,
    )


def _close_args(
    state_dir: Path,
    track_id: str,
    *,
    apply: bool = False,
    approval_id: str = "",
    attest: str | None = None,
    pr: list[str] | None = None,
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
        pr=pr,
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
# unlink-pr (golf Bx, D5b, OI-1664) — inverse of link-pr, operator-gated
# ---------------------------------------------------------------------------

def _unlink_pr_args(
    state_dir: Path,
    track_id: str,
    *prs: str,
    project_id: str = PROJECT_ID,
    json: bool = False,
    reason: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        pr=list(prs),
        json=json,
        reason=reason,
    )


def test_unlink_pr_removes_present_pr_and_writes_audit_event(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100,#1790,#200"
    )
    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#1790", reason="herkomst onbekend, OI-1664")
    )
    assert rc == 0
    assert _pr_ref(sd, "T") == "#100,#200"

    events = _track_events(sd, "T", "track_pr_unlinked")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["removed"] == ["#1790"]
    assert details["not_present"] == []
    assert details["pr_ref"] == "#100,#200"
    assert details["reason"] == "herkomst onbekend, OI-1664"


def test_unlink_pr_removing_all_refs_leaves_pr_ref_empty(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1790")
    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#1790", reason="only ref"))
    assert rc == 0
    assert _pr_ref(sd, "T") == ""


def test_unlink_pr_not_present_is_clean_noop_not_an_error(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100")
    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#999", reason="not linked here")
    )
    assert rc == 0
    assert _pr_ref(sd, "T") == "#100"  # unchanged
    # a no-op writes no audit event -- nothing to attest to.
    assert _track_events(sd, "T", "track_pr_unlinked") == []


def test_unlink_pr_on_missing_track_is_clean_error(tmp_path, capsys):
    sd = _build_db(tmp_path)
    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "missing", "#1", reason="x"))
    assert rc == 1
    captured = capsys.readouterr()
    assert "not found" in (captured.out + captured.err)


def test_unlink_pr_empty_reason_is_refused(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100")
    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#100", reason=""))
    assert rc == 2
    assert _pr_ref(sd, "T") == "#100"  # unchanged -- no silent bypass
    assert _track_events(sd, "T", "track_pr_unlinked") == []


def test_unlink_pr_whitespace_only_reason_is_refused(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100")
    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#100", reason="   "))
    assert rc == 2
    assert _pr_ref(sd, "T") == "#100"


def test_unlink_pr_wrong_project_id_does_not_write(tmp_path):
    """Builds on the SAME wrong-project guard as link-pr
    (`test_link_pr_wrong_project_id_does_not_write`): `get_track` is scoped to
    (track_id, project_id), so a track that exists only under PROJECT_ID is
    invisible under a different project_id -- the command errors out via the
    "track not found" path before any write is attempted, never via a
    second/duplicate scoping check."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1")
    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#1", project_id="other-proj", reason="x")
    )
    assert rc == 1
    assert _pr_ref(sd, "T", PROJECT_ID) == "#1"  # untouched


def test_unlink_pr_isolation_same_track_id_two_projects(tmp_path):
    """Tenancy requirement (golf Bx D5b, ADR-007): the SAME track_id under two
    different project_ids are two different rows -- unlinking in project A
    must never touch project B's row."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", "proj-a", title="x", goal_state="y", phase="queued", pr_ref="#1790")
    tracks_lib.create_track(sd, "T", "proj-b", title="x", goal_state="y", phase="queued", pr_ref="#1790")

    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#1790", project_id="proj-a", reason="isolation test")
    )
    assert rc == 0
    assert _pr_ref(sd, "T", project_id="proj-a") == ""
    assert _pr_ref(sd, "T", project_id="proj-b") == "#1790"  # untouched

    events_a = _track_events(sd, "T", "track_pr_unlinked")
    assert len(events_a) == 1
    assert events_a[0]["project_id"] == "proj-a"


# ---------------------------------------------------------------------------
# link-pr --delivery — OI-829 fail-closed auto-close gate
# ---------------------------------------------------------------------------

def test_link_pr_defaults_to_partial_delivery(tmp_path):
    """No --delivery flag -> 'partial' is recorded (fail-closed default)."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")

    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "T", "#500"))
    assert rc == 0
    assert _pr_delivery(sd, "T", 500) == "partial"


def test_link_pr_explicit_complete_delivery(tmp_path):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")

    rc = planning_cli.cmd_objective_link_pr(
        _link_pr_args(sd, "T", "#501", delivery="complete")
    )
    assert rc == 0
    assert _pr_delivery(sd, "T", 501) == "complete"


def test_link_pr_upgrades_already_present_pr_to_complete(tmp_path):
    """Re-linking an already-present PR with a different --delivery updates the
    existing row instead of being rejected as a no-op (upgrade workflow)."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#502"
    )
    rc1 = planning_cli.cmd_objective_link_pr(
        _link_pr_args(sd, "T", "#502", delivery="partial")
    )
    assert rc1 == 0
    assert _pr_delivery(sd, "T", 502) == "partial"

    # Same PR, already present -> pr_ref unchanged (noop_no_change) but the
    # delivery marking is still upgraded to 'complete'.
    rc2 = planning_cli.cmd_objective_link_pr(
        _link_pr_args(sd, "T", "#502", delivery="complete")
    )
    assert rc2 == 0
    assert _pr_ref(sd, "T") == "#502"
    assert _pr_delivery(sd, "T", 502) == "complete"


def test_link_pr_delivery_missing_migration_fails_closed_loudly(tmp_path, capsys):
    """OI-1167: DB without migration 0032 applied: link-pr must not crash, and
    must not quietly bury the failure either. pr_ref linking is a genuinely
    separate, successful fact (the reconciler's own OI-1167 hold protects
    auto-close independently of what gets recorded here), so the command
    still exits 0 -- but the delivery marking was NOT recorded, and that must
    be starkly visible: an ERROR-level line on stderr (not a soft "WARNING"
    bullet buried in the success output on stdout). The old shape printed
    only the soft warning and nothing on stderr, so a caller who only checks
    stderr for problems -- a common convention -- would see nothing at all."""
    sd = _build_db(tmp_path)  # deliberately NOT applying 0032
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")

    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "T", "#503", delivery="complete"))
    assert rc == 0
    assert _pr_ref(sd, "T") == "#503"
    captured = capsys.readouterr()
    assert "ERROR" in captured.err and "track_pr_delivery" in captured.err
    assert "WARNING" not in captured.out
    assert "WARNING" not in captured.err


def test_link_pr_delivery_missing_migration_json_reports_error(tmp_path, capsys):
    """Same missing-migration scenario via --json: delivery_written is False
    and an explicit "error" key names the cause -- a machine caller reading
    the JSON payload must not have to scrape human-facing text to detect it."""
    sd = _build_db(tmp_path)  # deliberately NOT applying 0032
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")

    rc = planning_cli.cmd_objective_link_pr(
        _link_pr_args(sd, "T", "#504", delivery="complete", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery_written"] is False
    assert payload["action"] == "linked"
    assert "track_pr_delivery" in payload["error"]


def test_link_pr_delivery_missing_migration_noop_no_change_branch_fails_closed(tmp_path, capsys):
    """Same fail-closed-and-loud contract on the OTHER write branch: re-linking
    an already-present PR (pr_ref unchanged -> action='noop_no_change') on a
    DB without migration 0032 must also surface the loud stderr error, not
    just the 'new pr_ref' branch above."""
    sd = _build_db(tmp_path)  # deliberately NOT applying 0032
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#505"
    )

    rc = planning_cli.cmd_objective_link_pr(_link_pr_args(sd, "T", "#505", delivery="complete"))
    assert rc == 0
    assert _pr_ref(sd, "T") == "#505"
    captured = capsys.readouterr()
    assert "ERROR" in captured.err and "track_pr_delivery" in captured.err


# ---------------------------------------------------------------------------
# unmark-delivery + unlink-pr delivery cleanup (OI-1872)
# ---------------------------------------------------------------------------

def _mark_delivery(
    state_dir: Path, track_id: str, pr_number: int, kind: str, project_id: str = PROJECT_ID
) -> None:
    """Seed a track_pr_delivery row directly, independent of the code under test."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?, ?, ?, ?, 'test')",
        (project_id, track_id, pr_number, kind),
    )
    conn.commit()
    conn.close()


def _delivery_prs(state_dir: Path, track_id: str, project_id: str = PROJECT_ID) -> list[int]:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    rows = conn.execute(
        "SELECT pr_number FROM track_pr_delivery WHERE project_id = ? AND track_id = ? "
        "ORDER BY pr_number",
        (project_id, track_id),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _unmark_args(
    state_dir: Path,
    track_id: str,
    *prs: str,
    project_id: str = PROJECT_ID,
    json: bool = False,
    reason: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=project_id,
        track_id=track_id,
        pr=list(prs),
        json=json,
        reason=reason,
    )


def test_unmark_delivery_removes_the_row_and_leaves_pr_ref_alone(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20"
    )
    _mark_delivery(sd, "T", 10, "partial")
    _mark_delivery(sd, "T", 20, "complete")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", reason="OI-1872: back to unmarked")
    )
    assert rc == 0
    assert _pr_delivery(sd, "T", 10) is None          # unmarked: no row
    assert _pr_delivery(sd, "T", 20) == "complete"    # other PR untouched
    assert _pr_ref(sd, "T") == "#10,#20"              # pr_ref left alone

    out = capsys.readouterr().out
    assert "delivery marker removed: #10" in out
    assert "no marker to remove" not in out


def test_unmark_delivery_reports_removed_and_no_row_separately(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20,#30"
    )
    _mark_delivery(sd, "T", 10, "partial")
    _mark_delivery(sd, "T", 30, "partial")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "10,20", "#30", reason="r", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == ["#10", "#30"]
    assert payload["no_row"] == ["#20"]
    assert payload["action"] == "unmarked"
    assert payload["applied"] is True
    assert payload["pr_ref"] == "#10,#20,#30"
    assert _delivery_prs(sd, "T") == []


def test_unmark_delivery_human_output_lists_removed_and_no_row(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    _mark_delivery(sd, "T", 10, "partial")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", "#20", reason="r")
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "delivery marker removed: #10" in out
    assert "no marker to remove: #20" in out


def test_unmark_delivery_writes_audit_event_with_reason(tmp_path):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    _mark_delivery(sd, "T", 10, "partial")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", "#20", reason="herstel OI-1872")
    )
    assert rc == 0
    events = _track_events(sd, "T", "track_delivery_unmarked")
    assert len(events) == 1
    assert events[0]["actor"] == "operator"
    assert events[0]["project_id"] == PROJECT_ID
    details = events[0]["details"]
    assert details["removed"] == ["#10"]
    assert details["no_row"] == ["#20"]
    assert details["reason"] == "herstel OI-1872"


def test_unmark_delivery_is_scoped_to_project_id(tmp_path):
    """ADR-007: the same track_id and PR under another project_id is a different
    row. Unmarking in proj-a must never touch proj-b's marker."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", "proj-a", title="x", goal_state="y", phase="queued", pr_ref="#10")
    tracks_lib.create_track(sd, "T", "proj-b", title="x", goal_state="y", phase="queued", pr_ref="#10")
    _mark_delivery(sd, "T", 10, "partial", project_id="proj-a")
    _mark_delivery(sd, "T", 10, "complete", project_id="proj-b")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", project_id="proj-a", reason="isolation test")
    )
    assert rc == 0
    assert _pr_delivery(sd, "T", 10, project_id="proj-a") is None
    assert _pr_delivery(sd, "T", 10, project_id="proj-b") == "complete"   # untouched
    assert _pr_ref(sd, "T", project_id="proj-b") == "#10"

    events = _track_events(sd, "T", "track_delivery_unmarked")
    assert len(events) == 1
    assert events[0]["project_id"] == "proj-a"


def test_unmark_delivery_wrong_project_id_removes_nothing(tmp_path, capsys):
    """A track that exists only under PROJECT_ID is invisible under another
    project_id: the command stops at 'track not found' before any DELETE."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")
    _mark_delivery(sd, "T", 10, "partial")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", project_id="other-proj", reason="x")
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err
    assert _pr_delivery(sd, "T", 10) == "partial"


def test_unmark_delivery_only_no_row_is_a_clean_noop_without_event(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", reason="r", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop_no_row"
    assert payload["applied"] is False
    assert payload["removed"] == []
    assert payload["no_row"] == ["#10"]
    # a no-op writes no audit event -- nothing to attest to.
    assert _track_events(sd, "T", "track_delivery_unmarked") == []


def test_unmark_delivery_empty_reason_is_refused(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")
    _mark_delivery(sd, "T", 10, "partial")

    for reason in ("", "   "):
        rc = planning_cli.cmd_objective_unmark_delivery(_unmark_args(sd, "T", "#10", reason=reason))
        assert rc == 2
    assert "--reason is required" in capsys.readouterr().err
    assert _pr_delivery(sd, "T", 10) == "partial"     # no silent bypass
    assert _track_events(sd, "T", "track_delivery_unmarked") == []


def test_unmark_delivery_on_missing_track_is_clean_error(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    rc = planning_cli.cmd_objective_unmark_delivery(_unmark_args(sd, "missing", "#1", reason="x"))
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_unmark_delivery_rejects_input_without_a_valid_pr_ref(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")
    _mark_delivery(sd, "T", 10, "partial")

    rc = planning_cli.cmd_objective_unmark_delivery(_unmark_args(sd, "T", "abc", reason="x"))
    assert rc == 2
    assert "no valid PR refs" in capsys.readouterr().err
    assert _pr_delivery(sd, "T", 10) == "partial"


def test_unmark_delivery_pre_0032_store_is_legible_not_a_crash(tmp_path, capsys):
    """A store without migration 0032 has no track_pr_delivery table: no marker
    can exist, so the command says exactly that (stderr, and a note in --json)
    instead of crashing on the missing table or claiming a removal."""
    sd = _build_db(tmp_path)  # deliberately NOT applying 0032
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")

    rc = planning_cli.cmd_objective_unmark_delivery(_unmark_args(sd, "T", "#10", reason="r"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "track_pr_delivery" in captured.err and "0032" in captured.err
    assert "delivery marker removed" not in captured.out
    assert _pr_ref(sd, "T") == "#10"
    assert _track_events(sd, "T", "track_delivery_unmarked") == []

    rc = planning_cli.cmd_objective_unmark_delivery(
        _unmark_args(sd, "T", "#10", reason="r", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop_table_absent"
    assert payload["delivery_table_present"] is False
    assert payload["applied"] is False
    assert payload["removed"] == []
    assert "track_pr_delivery" in payload["note"]


def test_unlink_pr_deletes_the_delivery_marker_of_the_removed_pr_only(tmp_path):
    """OI-1872: unlink-pr used to drop the ref and leave an orphan marker behind."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#100,#1790,#200"
    )
    _mark_delivery(sd, "T", 100, "complete")
    _mark_delivery(sd, "T", 1790, "partial")
    _mark_delivery(sd, "T", 200, "partial")

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#1790", reason="r"))
    assert rc == 0
    assert _pr_ref(sd, "T") == "#100,#200"
    assert _delivery_prs(sd, "T") == [100, 200]       # #1790's marker is gone, no orphan
    assert _pr_delivery(sd, "T", 100) == "complete"   # the kept refs keep their marks


def test_unlink_pr_removing_several_prs_deletes_each_marker(tmp_path):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1,#2,#3"
    )
    for n in (1, 2):
        _mark_delivery(sd, "T", n, "partial")     # #3 stays unmarked

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#1,#2,#3", reason="r"))
    assert rc == 0
    assert _pr_ref(sd, "T") == ""
    assert _delivery_prs(sd, "T") == []


def test_unlink_pr_reports_the_removed_markers(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1,#2,#3"
    )
    _mark_delivery(sd, "T", 1, "partial")
    _mark_delivery(sd, "T", 2, "complete")

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#1,#2", reason="r"))
    assert rc == 0
    assert "delivery markers removed: #1, #2" in capsys.readouterr().out

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#3", reason="r"))
    assert rc == 0
    assert "none had a marker" in capsys.readouterr().out


def test_unlink_pr_json_and_audit_event_carry_delivery_removed(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1,#2"
    )
    _mark_delivery(sd, "T", 1, "partial")

    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#1", "#2", reason="r", json=True)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == ["#1", "#2"]
    assert payload["delivery_removed"] == ["#1"]
    assert payload["delivery_table_present"] is True
    details = _track_events(sd, "T", "track_pr_unlinked")[0]["details"]
    assert details["delivery_removed"] == ["#1"]


def test_unlink_pr_delivery_delete_is_scoped_to_project_id(tmp_path):
    """ADR-007: same track_id and PR under two project_ids -- unlinking in
    proj-a deletes proj-a's marker and never proj-b's."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", "proj-a", title="x", goal_state="y", phase="queued", pr_ref="#1790")
    tracks_lib.create_track(sd, "T", "proj-b", title="x", goal_state="y", phase="queued", pr_ref="#1790")
    _mark_delivery(sd, "T", 1790, "partial", project_id="proj-a")
    _mark_delivery(sd, "T", 1790, "complete", project_id="proj-b")

    rc = planning_cli.cmd_objective_unlink_pr(
        _unlink_pr_args(sd, "T", "#1790", project_id="proj-a", reason="isolation test")
    )
    assert rc == 0
    assert _delivery_prs(sd, "T", "proj-a") == []
    assert _pr_delivery(sd, "T", 1790, project_id="proj-b") == "complete"   # untouched
    assert _pr_ref(sd, "T", project_id="proj-b") == "#1790"


def test_unlink_pr_pre_0032_store_still_unlinks_and_says_no_marker_table(tmp_path, capsys):
    sd = _build_db(tmp_path)  # deliberately NOT applying 0032
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#1,#2")

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#1", reason="r"))
    assert rc == 0
    assert _pr_ref(sd, "T") == "#2"
    assert "track_pr_delivery table is absent" in capsys.readouterr().out

    rc = planning_cli.cmd_objective_unlink_pr(_unlink_pr_args(sd, "T", "#2", reason="r", json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery_table_present"] is False
    assert payload["delivery_removed"] == []


def test_unmark_delivery_is_reachable_through_the_planning_cli_parser(tmp_path, capsys):
    """The subcommand is wired into planning_cli's own argparse tree (what
    `bin/vnx objective ...` execs), with the flags the handler reads."""
    sd = _build_db(tmp_path)
    _apply_migration_0032(sd)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10")
    _mark_delivery(sd, "T", 10, "partial")

    rc = planning_cli.main([
        "objective", "unmark-delivery", "T", "#10", "--reason", "via parser",
        "--state-dir", str(sd), "--project-id", PROJECT_ID, "--json",
    ])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["removed"] == ["#10"]
    assert _pr_delivery(sd, "T", 10) is None


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


def test_close_attest_with_pr_stamps_real_ref_not_date(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "ops", PROJECT_ID, title="fleet sync", goal_state="done", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "ops", apply=True, approval_id="APR-OPS", attest="fleet-sync", pr=["1234"])
    )
    assert rc == 0
    assert _phase(sd, "ops") == "done"
    assert _pr_ref(sd, "ops") == "#1234"
    assert not _pr_ref(sd, "ops").startswith("ops-attest:")

    events = _track_events(sd, "ops", "track_ops_attest")
    assert len(events) == 1
    details = events[0]["details"]
    assert details["pr_ref"] == "#1234"
    assert details["pr_arg_raw"] == ["1234"]
    assert details["pr_arg_resolved"] == "#1234"


def test_close_attest_with_multi_pr_stamps_ordered_deduped_ref(tmp_path):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "ops", PROJECT_ID, title="fleet sync", goal_state="done", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "ops", apply=True, approval_id="APR-OPS", attest="fleet-sync",
                    pr=["1199,1200"])
    )
    assert rc == 0
    assert _pr_ref(sd, "ops") == "#1199,#1200"

    events = _track_events(sd, "ops", "track_ops_attest")
    assert events[0]["details"]["pr_ref"] == "#1199,#1200"


def test_close_attest_pr_normalization_variants(tmp_path):
    # Bare number, '#'-prefixed, and repeated --pr all resolve via _merge_pr_refs.
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "bare", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "bare", apply=True, approval_id="A", attest="r", pr=["1234"])
    )
    assert rc == 0
    assert _pr_ref(sd, "bare") == "#1234"

    tracks_lib.create_track(sd, "hashed", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "hashed", apply=True, approval_id="A", attest="r", pr=["#1234"])
    )
    assert rc == 0
    assert _pr_ref(sd, "hashed") == "#1234"

    tracks_lib.create_track(sd, "repeated", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "repeated", apply=True, approval_id="A", attest="r", pr=["1234", "1235"])
    )
    assert rc == 0
    assert _pr_ref(sd, "repeated") == "#1234,#1235"


def test_close_pr_without_attest_is_rejected_no_write(tmp_path, capsys):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", pr=["1234"])
    )
    assert rc != 0
    out = (capsys.readouterr().err)
    assert "--attest" in out and "link-pr" in out
    assert _phase(sd, "T") == "queued"
    assert _pr_ref(sd, "T") == ""


def test_close_attest_pr_scoped_to_project_id(tmp_path):
    # ADR-007: the UPDATE stays WHERE track_id=? AND project_id=? — a same-named
    # track in another project must not be touched.
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    tracks_lib.create_track(sd, "T", "other-proj", title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["1234"],
                    project_id=PROJECT_ID)
    )
    assert rc == 0
    assert _pr_ref(sd, "T", PROJECT_ID) == "#1234"
    assert _pr_ref(sd, "T", "other-proj") == ""
    assert _phase(sd, "T", "other-proj") == "queued"


# ---------------------------------------------------------------------------
# close --attest APPENDS to pr_ref (OI-1872: a replace lost 17 refs)
# ---------------------------------------------------------------------------

def test_close_attest_pr_appends_to_existing_refs_not_replaces(tmp_path):
    """The OI-1872 regression: a track with 3 refs closed with `--attest --pr 9`
    ends with 4 refs, the 3 earlier ones first and in their original order."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20,#30"
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["9"])
    )
    assert rc == 0
    assert _phase(sd, "T") == "done"
    assert _pr_ref(sd, "T") == "#10,#20,#30,#9"
    assert len(_pr_ref(sd, "T").split(",")) == 4


def test_close_attest_pr_dedupes_against_existing_refs(tmp_path):
    """`--pr` uses the link-pr merge: a ref already on the track is not added
    twice, however it is spelled, and the new ones land after the old ones."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20"
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["20,#40", "10", "40"])
    )
    assert rc == 0
    assert _pr_ref(sd, "T") == "#10,#20,#40"


def test_close_attest_without_pr_keeps_existing_refs_and_adds_the_stamp(tmp_path):
    """The ops-attest fail-open (no --pr) must not wipe a non-empty pr_ref."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20"
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r")
    )
    assert rc == 0
    refs = _pr_ref(sd, "T").split(",")
    assert refs[:2] == ["#10", "#20"]
    assert len(refs) == 3
    assert refs[2].startswith("ops-attest:")


def test_close_attest_pr_keeps_an_earlier_ops_attest_stamp(tmp_path):
    """An earlier attestation is part of the record: appending a real PR to a
    track that carries `ops-attest:<date>` keeps the stamp, verbatim and in
    place. `_merge_pr_refs` alone would drop it (it only understands PR refs)."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued",
        pr_ref="#10,ops-attest:2026-08-01",
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["9"])
    )
    assert rc == 0
    assert _pr_ref(sd, "T") == "#10,ops-attest:2026-08-01,#9"


def test_close_attest_stamp_is_not_duplicated_on_an_identical_stamp(tmp_path):
    """Attesting twice on the same day must not stack two identical stamps."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    assert planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="first")
    ) == 0
    first = _pr_ref(sd, "T")
    assert first.startswith("ops-attest:")

    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET phase = 'queued' WHERE track_id = 'T' AND project_id = ?",
        (PROJECT_ID,),
    )
    conn.commit()
    conn.close()

    assert planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="second")
    ) == 0
    assert _pr_ref(sd, "T") == first


def test_close_attest_audit_event_records_pr_ref_before(tmp_path):
    """The audit event carries what pr_ref was before the close, so the record
    itself shows nothing was lost."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20,#30"
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["9"])
    )
    assert rc == 0
    details = _track_events(sd, "T", "track_ops_attest")[0]["details"]
    assert details["pr_ref_before"] == "#10,#20,#30"
    assert details["pr_ref"] == "#10,#20,#30,#9"
    assert details["pr_arg_resolved"] == "#9"


def test_close_attest_invalid_pr_leaves_existing_refs_untouched(tmp_path, capsys):
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued", pr_ref="#10,#20"
    )
    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T", apply=True, approval_id="A", attest="r", pr=["not-a-pr"])
    )
    assert rc == 2
    assert "no valid PR refs" in capsys.readouterr().out
    assert _pr_ref(sd, "T") == "#10,#20"
    assert _phase(sd, "T") == "queued"
    assert _track_events(sd, "T", "track_ops_attest") == []


def test_close_attest_help_says_append_not_replace(capsys):
    """The help text is the operator's only documentation of this flag: it must
    say what happens to the refs already on the track."""
    with pytest.raises(SystemExit) as exc:
        planning_cli._build_parser().parse_args(["objective", "close", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "APPENDS the real PR to the track's existing pr_ref" in out
    assert "nothing replaced" in out


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


def test_plan_gate_attest_writes_resolution_reason(tmp_path):
    """Resolving with a reason must persist resolution_reason — measured by reading
    the row back (not by the function's return value)."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="shipped", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)

    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="already shipped+merged pre-gate", approval_id="APR-1")
    )
    assert rc == 0
    assert _plan_oi_resolution_reason(sd, "T") == "[attest:APR-1] already shipped+merged pre-gate"


def test_resolve_plan_blocker_panel_reason_tag(tmp_path):
    """The panel path tags its recorded reason with ``[panel]`` so a later audit can
    tell a panel pass from an operator attestation."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)

    resolved = planning_cli._resolve_plan_blocker(
        sd, "T", PROJECT_ID,
        reason="PASS (2 pass / 0 revise / 0 block, 3 seats)", resolver="panel",
    )
    assert resolved is True
    assert _plan_oi_resolution_reason(sd, "T") == "[panel] PASS (2 pass / 0 revise / 0 block, 3 seats)"


@pytest.mark.parametrize("bad_reason", ["", "   ", "\t\n"])
def test_resolve_plan_blocker_empty_reason_fails_closed(tmp_path, bad_reason):
    """A resolution without a non-empty reason must FAIL and leave resolved_at
    untouched — the fail-closed check lives in the write layer, not the CLI."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)

    with pytest.raises(ValueError):
        planning_cli._resolve_plan_blocker(
            sd, "T", PROJECT_ID, reason=bad_reason, resolver="attest", approval_id="APR-1"
        )

    assert _plan_oi_resolved_at(sd, "T") is None
    assert _derived_status(sd, "T") == "blocked"


def test_plan_gate_missing_reasons_lists_reasonless_row(tmp_path, capsys):
    """The read-only report surfaces a resolved plan-gate blocker that carries no
    resolution_reason (the pre-fix write path dropped it)."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    # Simulate a pre-fix resolution: resolved_at set, resolution_reason dropped.
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE track_open_items SET resolved_at = ? "
        "WHERE track_id = ? AND project_id = ? AND oi_id = ? AND link_type = 'blocks'",
        ("2026-08-14T10:00:00.000000Z", "T", PROJECT_ID, "OI-PLAN-T"),
    )
    conn.commit()
    conn.close()

    rc = planning_cli.cmd_plan_gate_missing_reasons(_plan_missing_reasons_args(sd))
    assert rc == 0
    out = capsys.readouterr().out
    assert "T" in out
    assert "2026-08-14T10:00:00.000000Z" in out


def test_plan_gate_missing_reasons_empty_is_success(tmp_path, capsys):
    """A DB with no reasonless rows is a clean success, not a failure (exit 0)."""
    sd = _build_db_plan_gate(tmp_path)
    rc = planning_cli.cmd_plan_gate_missing_reasons(_plan_missing_reasons_args(sd))
    assert rc == 0
    assert "no resolved plan-gate blockers" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# plan-gate backfill-reason
# ---------------------------------------------------------------------------

def test_plan_gate_backfill_reason_fills_reasonless_resolved_row(tmp_path):
    """backfill records the missing resolution_reason on an already-resolved row,
    leaving resolved_at untouched — measured by reading the row back."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="shipped", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    _lift_plan_blocker_reasonless(sd, "T", "2026-08-14T10:00:00.000000Z")

    rc = planning_cli.cmd_plan_gate_backfill_reason(
        _plan_backfill_args(sd, "T", reason="already shipped+merged pre-gate", approval_id="APR-2")
    )
    assert rc == 0
    assert _plan_oi_resolution_reason(sd, "T") == "[backfill:APR-2] already shipped+merged pre-gate"
    assert _plan_oi_resolved_at(sd, "T") == "2026-08-14T10:00:00.000000Z"  # untouched

    events = _track_events(sd, "T", "plan_gate_backfill")
    assert len(events) == 1
    assert events[0]["details"]["reason"] == "already shipped+merged pre-gate"
    assert events[0]["details"]["approval_id"] == "APR-2"


def test_plan_gate_backfill_reason_unresolved_row_fails_and_leaves_row(tmp_path, capsys):
    """backfill on a still-blocked row is refused with an explicit pointer to attest,
    and the row is left untouched."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)  # unresolved

    rc = planning_cli.cmd_plan_gate_backfill_reason(
        _plan_backfill_args(sd, "T", reason="x", approval_id="APR-1")
    )
    assert rc == 1
    assert "attest" in capsys.readouterr().err
    assert _plan_oi_resolved_at(sd, "T") is None
    assert _plan_oi_resolution_reason(sd, "T") is None
    assert _track_events(sd, "T", "plan_gate_backfill") == []


def test_plan_gate_backfill_reason_existing_reason_fails_and_preserves(tmp_path, capsys):
    """Overwriting an existing resolution_reason is falsification: refused, the
    existing reason is shown, and it stays put."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="shipped", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    planning_cli._resolve_plan_blocker(
        sd, "T", PROJECT_ID, reason="shipped pre-gate", resolver="attest", approval_id="APR-9"
    )
    original = _plan_oi_resolution_reason(sd, "T")
    assert original == "[attest:APR-9] shipped pre-gate"

    rc = planning_cli.cmd_plan_gate_backfill_reason(
        _plan_backfill_args(sd, "T", reason="second", approval_id="APR-2")
    )
    assert rc == 1
    assert original in capsys.readouterr().err  # existing reason shown
    assert _plan_oi_resolution_reason(sd, "T") == original  # unchanged


def test_plan_gate_backfill_reason_no_seeded_blocker_fails(tmp_path, capsys):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_plan_gate_backfill_reason(
        _plan_backfill_args(sd, "T", reason="x", approval_id="APR-1")
    )
    assert rc == 1
    assert "nothing to backfill" in capsys.readouterr().err


@pytest.mark.parametrize("bad_reason", ["", "   ", "\t\n"])
def test_plan_gate_backfill_reason_empty_reason_refused(tmp_path, bad_reason):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    _lift_plan_blocker_reasonless(sd, "T", "2026-08-14T10:00:00.000000Z")

    rc = planning_cli.cmd_plan_gate_backfill_reason(
        _plan_backfill_args(sd, "T", reason=bad_reason, approval_id="APR-1")
    )
    assert rc == 2
    assert _plan_oi_resolution_reason(sd, "T") is None
    assert _track_events(sd, "T", "plan_gate_backfill") == []


# ---------------------------------------------------------------------------
# plan-gate reblock
# ---------------------------------------------------------------------------

def test_plan_gate_reblock_puts_back_lifted_blocker_and_blocks(tmp_path):
    """reblock clears resolved_at again and the track reconciles back to blocked,
    with the prior resolution history preserved in resolution_reason."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    _lift_plan_blocker_reasonless(sd, "T", "2026-08-14T10:00:00.000000Z")

    # Reconcile so the (wrong) lift is reflected before we re-block — proves the
    # reblock is what flips derived_status back to blocked.
    planning_cli.track_reconciler.reconcile_track(sd, "T", PROJECT_ID)
    assert _derived_status(sd, "T") != "blocked"

    rc = planning_cli.cmd_plan_gate_reblock(
        _plan_reblock_args(sd, "T", reason="gate still has work", approval_id="APR-3")
    )
    assert rc == 0
    assert _plan_oi_resolved_at(sd, "T") is None  # blocking again
    assert _derived_status(sd, "T") == "blocked"

    reason = _plan_oi_resolution_reason(sd, "T")
    assert reason.startswith("[reblock:APR-3] gate still has work")
    assert "2026-08-14T10:00:00.000000Z" in reason  # prior resolved_at preserved
    assert "prior reason: (none)" in reason

    events = _track_events(sd, "T", "plan_gate_reblock")
    assert len(events) == 1
    assert events[0]["details"]["approval_id"] == "APR-3"
    assert events[0]["details"]["prior_resolved_at"] == "2026-08-14T10:00:00.000000Z"


def test_plan_gate_reblock_preserves_prior_reason(tmp_path):
    """A wrongly-lifted row that DID carry a reason keeps that reason visible,
    embedded in the reversal note — nothing is silently wiped."""
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    planning_cli._resolve_plan_blocker(
        sd, "T", PROJECT_ID, reason="shipped pre-gate", resolver="attest", approval_id="APR-9"
    )

    rc = planning_cli.cmd_plan_gate_reblock(
        _plan_reblock_args(sd, "T", reason="was wrong", approval_id="APR-4")
    )
    assert rc == 0
    reason = _plan_oi_resolution_reason(sd, "T")
    assert reason.startswith("[reblock:APR-4] was wrong")
    assert "prior reason: [attest:APR-9] shipped pre-gate" in reason
    assert _derived_status(sd, "T") == "blocked"


def test_plan_gate_reblock_already_blocked_fails(tmp_path, capsys):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)  # already unresolved

    rc = planning_cli.cmd_plan_gate_reblock(
        _plan_reblock_args(sd, "T", reason="x", approval_id="APR-1")
    )
    assert rc == 1
    assert "nothing to put back" in capsys.readouterr().err
    assert _plan_oi_resolved_at(sd, "T") is None
    assert _track_events(sd, "T", "plan_gate_reblock") == []


def test_plan_gate_reblock_no_seeded_blocker_fails(tmp_path, capsys):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    rc = planning_cli.cmd_plan_gate_reblock(
        _plan_reblock_args(sd, "T", reason="x", approval_id="APR-1")
    )
    assert rc == 1
    assert "nothing to put back" in capsys.readouterr().err


@pytest.mark.parametrize("bad_reason", ["", "   ", "\t\n"])
def test_plan_gate_reblock_empty_reason_refused(tmp_path, bad_reason):
    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="y", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    _lift_plan_blocker_reasonless(sd, "T", "2026-08-14T10:00:00.000000Z")

    rc = planning_cli.cmd_plan_gate_reblock(
        _plan_reblock_args(sd, "T", reason=bad_reason, approval_id="APR-1")
    )
    assert rc == 2
    assert _plan_oi_resolved_at(sd, "T") is not None  # still lifted (unchanged)
    assert _track_events(sd, "T", "plan_gate_reblock") == []


# ---------------------------------------------------------------------------
# _emit_plan_gate_pass_record return-value contract
# ---------------------------------------------------------------------------

def test_emit_plan_gate_pass_record_returns_false_when_evidence_not_written(tmp_path, monkeypatch):
    """emit_plan_gate_pass returning None (a failed write, which never raises)
    must surface as False - the old code ignored the return and always True.

    Regression for the PR #1412 codex finding: emit_plan_gate_pass is documented
    to return the appended record on success and None on any failure, so the
    try/except around it cannot catch a failed write. None IS the failure signal.
    """
    import plan_gate_evidence
    monkeypatch.setattr(plan_gate_evidence, "emit_plan_gate_pass", lambda **kw: None)

    ok = planning_cli._emit_plan_gate_pass_record(
        repo_root=str(tmp_path),
        track_id="T", project_id="p1", resolver="run", seats=2, scope="light",
    )
    assert ok is False


def test_emit_plan_gate_pass_record_returns_true_when_record_appended(tmp_path, monkeypatch):
    """A successful append (a record comes back) reports True."""
    import plan_gate_evidence
    monkeypatch.setattr(
        plan_gate_evidence, "emit_plan_gate_pass",
        lambda **kw: {"type": "plan_gate_pass", "track_id": kw["track_id"]},
    )

    ok = planning_cli._emit_plan_gate_pass_record(
        repo_root=str(tmp_path),
        track_id="T", project_id="p1", resolver="run", seats=2, scope="light",
    )
    assert ok is True


def test_plan_gate_attest_failed_evidence_write_is_loud_not_silent(tmp_path, capsys, monkeypatch):
    """A failed durable write must not break attest (exit 0, blocker resolved)
    but must be reported loudly on stderr - never a silent success."""
    import plan_gate_evidence
    monkeypatch.setattr(plan_gate_evidence, "emit_plan_gate_pass", lambda **kw: None)

    sd = _build_db_plan_gate(tmp_path)
    tracks_lib.create_track(sd, "T", PROJECT_ID, title="x", goal_state="shipped", phase="queued")
    planning_cli._seed_plan_blocker(sd, "T", PROJECT_ID)
    assert _derived_status(sd, "T") == "blocked"

    rc = planning_cli.cmd_plan_gate_attest(
        _plan_attest_args(sd, "T", reason="shipped pre-gate", approval_id="APR-2")
    )
    assert rc == 0  # gate resolution not broken by the failed evidence write
    assert _plan_oi_resolved_at(sd, "T") is not None
    captured = capsys.readouterr()
    assert "plan_gate_pass evidence NOT written" in captured.err


# ---------------------------------------------------------------------------
# OI-1190: `objective show` surfaces the track's decision_ref (text + --json).
# ---------------------------------------------------------------------------

def _set_decision_ref_raw(state_dir: Path, track_id: str, payload: str, project_id: str = PROJECT_ID) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET decision_ref = ? WHERE track_id = ? AND project_id = ?",
        (payload, track_id, project_id),
    )
    conn.commit()
    conn.close()


def test_objective_show_text_surfaces_decision_ref(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0033(sd)
    tracks_lib.create_track(sd, "feat-show", PROJECT_ID, title="t", goal_state="shipped", phase="queued")
    _set_decision_ref_raw(
        sd, "feat-show",
        json.dumps({
            "reports": ["unified_reports/plan-gate-feat-show-opus-abc12345.md",
                        "unified_reports/plan-gate-feat-show-kimi-6789abcd.md"],
            "decision": "PASS", "rejected_alternatives": [], "set_at": "x", "source": "plan-gate",
        }),
    )

    assert planning_cli.cmd_objective_show(_show_args(sd, "feat-show")) == 0
    out = capsys.readouterr().out
    assert "decision_ref: PASS (2 report(s), 0 rejected alternative(s))" in out


def test_objective_show_text_absent_decision_ref_renders_dash(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0033(sd)
    tracks_lib.create_track(sd, "feat-noref", PROJECT_ID, title="t", goal_state="shipped", phase="queued")

    assert planning_cli.cmd_objective_show(_show_args(sd, "feat-noref")) == 0
    out = capsys.readouterr().out
    assert "decision_ref: -" in out


def test_objective_show_json_includes_decision_ref(tmp_path, capsys):
    sd = _build_db(tmp_path)
    _apply_migration_0033(sd)
    tracks_lib.create_track(sd, "feat-json", PROJECT_ID, title="t", goal_state="shipped", phase="queued")
    payload = '{"decision": "PASS", "reports": [], "rejected_alternatives": [], "set_at": "x", "source": "plan-gate"}'
    _set_decision_ref_raw(sd, "feat-json", payload)

    assert planning_cli.cmd_objective_show(_show_args(sd, "feat-json", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision_ref"] == payload
