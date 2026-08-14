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
