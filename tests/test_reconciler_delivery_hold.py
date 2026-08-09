"""tests/test_reconciler_delivery_hold.py — OI-1098 delivery-hold.

The reconciler used to WRITE the per-PR ``delivery`` marking (planning_cli
``--delivery``) but never READ it: a track linked with ``--delivery partial``
was still nominated CONFIRMED-done by ``vnx horizon reconcile``.

This file pins the decided contract (fail-closed AND visible):

- Derivation: one explicit non-'complete' marking on ANY currently-linked PR
  vetoes every PR-evidence-based 'done' (merged coordination event, pr_ref
  merged-subset, no-dispatch pr_ref path). Declared 'done' stays authoritative.
- Visibility: reconcile_track/peek_derived_status carry a ``delivery_hold``
  dict with an operator-readable reason; run_reconcile reports the track as
  verdict 'delivery_hold' instead of nominating it (and never consults gh
  for it).
- Legacy (measured on the real vnx-dev tracks DB, 2026-08-09: only 'partial'
  and 'complete' values exist): a PR with NO track_pr_delivery row is NOT an
  explicit 'partial' — post-0032 links always write a row (--delivery
  defaults to 'partial'), so a missing row means pre-0032 legacy and does
  NOT hold. A DB without the 0032 table behaves as before.
- Fail-closed edge: an unrecognized delivery_kind holds (and logs), never
  reads as 'complete'.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import objective_reconcile  # noqa: E402
import schema_migration  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

PROJECT_ID = "test-delivery-hold"


@pytest.fixture(autouse=True)
def _clear_schema_preflight_hooks():
    """Isolate schema_migration._PREFLIGHT_HOOKS (mirrors test_objective_reconcile)."""
    import importlib

    sm = None
    try:
        sm = importlib.import_module("schema_migration")
        saved = {k: list(v) for k, v in sm._PREFLIGHT_HOOKS.items()}
        sm._PREFLIGHT_HOOKS.clear()
    except (ImportError, AttributeError):
        saved = None
    yield
    if saved is not None and sm is not None:
        sm._PREFLIGHT_HOOKS.clear()
        sm._PREFLIGHT_HOOKS.update(saved)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path, *, with_delivery_table: bool = True) -> Path:
    """State dir with migrations 0022+0024+0027+0028+0030 (+0032 unless told otherwise)."""
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

    migrations = [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ]
    if with_delivery_table:
        migrations.append((32, "0032_track_pr_delivery.sql"))
    for ver, fname in migrations:
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    conn.close()
    return state_dir


def _set_delivery(
    state_dir: Path, track_id: str, pr_number: int, kind: str, *, set_by: str = "operator"
) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    if kind not in ("partial", "complete"):
        conn.execute("PRAGMA ignore_check_constraints = 1")
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        (PROJECT_ID, track_id, pr_number, kind, set_by),
    )
    conn.commit()
    conn.close()


def _seed_track(
    state_dir: Path,
    track_id: str,
    *,
    phase: str = "active",
    pr_ref: Optional[str] = None,
) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID,
        title=f"Track {track_id}",
        goal_state=f"ship {track_id}",
        phase=phase,
        pr_ref=pr_ref,
    )


def _seed_dispatch(
    state_dir: Path, dispatch_id: str, track_id: str, *, state: str = "completed"
) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_event(state_dir: Path, dispatch_id: str) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        (f"ev-{dispatch_id}", "pr_merged", "dispatch", dispatch_id, PROJECT_ID),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_ndjson(state_dir: Path, pr_number: int) -> None:
    events_dir = state_dir.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    with open(events_dir / "pr_merged.ndjson", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "pr_merged", "pr_number": pr_number}) + "\n")


# ---------------------------------------------------------------------------
# gh subprocess mock (same shape as tests/test_objective_reconcile.py)
# ---------------------------------------------------------------------------

_MERGED_AT = "2026-08-01T12:00:00Z"


def _make_gh_mock(pr_responses: Dict[int, Any], call_log: Optional[list] = None):
    def fake_run(cmd, **kwargs):
        if call_log is not None:
            call_log.append(list(cmd))
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "bad cmd")
        cmd0 = os.path.basename(str(cmd[0]))
        if cmd0 == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd0 == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
            pr_num = int(cmd[3])
            resp = pr_responses.get(pr_num)
            if resp is None:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(resp), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def _merged_pr(number: int) -> Dict[str, str]:
    return {"state": "MERGED", "mergedAt": _MERGED_AT}


def _is_gh_pr_view(cmd: list) -> bool:
    return (
        len(cmd) >= 3
        and os.path.basename(str(cmd[0])) == "gh"
        and cmd[1] == "pr"
        and cmd[2] == "view"
    )


# ---------------------------------------------------------------------------
# Derivation tests (track_reconciler.reconcile_track / peek_derived_status)
# ---------------------------------------------------------------------------

def test_all_complete_markings_derives_done_unchanged(tmp_path):
    """All linked PRs explicitly 'complete' → PR-evidence 'done' (unchanged behaviour)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-complete", phase="active", pr_ref="#100,#101")
    _seed_dispatch(sd, "D-complete", "T-complete", state="completed")
    _seed_pr_merged_event(sd, "D-complete")
    _set_delivery(sd, "T-complete", 100, "complete")
    _set_delivery(sd, "T-complete", 101, "complete")

    result = track_reconciler.reconcile_track(sd, "T-complete", PROJECT_ID)

    assert result["derived_status"] == "done"
    assert "delivery_hold" not in result


def test_one_partial_pr_vetoes_done_and_reason_visible(tmp_path):
    """OI-1098 core: ONE explicit 'partial' among the linked PRs vetoes the
    PR-evidence 'done' — and the hold + reason ride the result dict (visible)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-partial", phase="active", pr_ref="#100,#101")
    _seed_dispatch(sd, "D-partial", "T-partial", state="completed")
    _seed_pr_merged_event(sd, "D-partial")
    _set_delivery(sd, "T-partial", 100, "partial")
    _set_delivery(sd, "T-partial", 101, "complete")

    result = track_reconciler.reconcile_track(sd, "T-partial", PROJECT_ID)

    assert result["derived_status"] == "in_progress"  # terminal dispatches, done vetoed
    hold = result.get("delivery_hold")
    assert hold is not None
    assert hold["partial"] == 1
    assert hold["complete"] == 1
    assert hold["total"] == 2
    assert "1 of 2" in hold["reason"]

    # peek (read-only dry-run path) carries the same visibility contract.
    peeked = track_reconciler.peek_derived_status(sd, "T-partial", PROJECT_ID)
    assert peeked["derived_status"] == "in_progress"
    assert peeked.get("delivery_hold", {}).get("reason") == hold["reason"]


def test_unmarked_legacy_pr_derives_done(tmp_path):
    """Legacy choice (pinned): a linked PR with NO track_pr_delivery row is
    not an explicit 'partial' — pre-0032 links never had the chance to be
    marked, and post-0032 links always write a row. Unmarked ⇒ no hold."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-legacy", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-legacy", "T-legacy", state="completed")
    _seed_pr_merged_event(sd, "D-legacy")

    result = track_reconciler.reconcile_track(sd, "T-legacy", PROJECT_ID)

    assert result["derived_status"] == "done"
    assert "delivery_hold" not in result


def test_pre0032_db_without_table_derives_done(tmp_path):
    """A DB without migration 0032 has no explicit markings at all → no hold."""
    sd = _build_db(tmp_path, with_delivery_table=False)
    _seed_track(sd, "T-pre32", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-pre32", "T-pre32", state="completed")
    _seed_pr_merged_event(sd, "D-pre32")

    result = track_reconciler.reconcile_track(sd, "T-pre32", PROJECT_ID)

    assert result["derived_status"] == "done"
    assert "delivery_hold" not in result


def test_declared_done_with_partial_marking_stays_done(tmp_path):
    """Declared phase is authoritative: the hold vetoes PR-EVIDENCE paths only,
    never a human-declared 'done' (declared-done stability is unchanged)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-declared", phase="done", pr_ref="#100")
    _seed_dispatch(sd, "D-declared", "T-declared", state="completed")
    _set_delivery(sd, "T-declared", 100, "partial")

    result = track_reconciler.reconcile_track(sd, "T-declared", PROJECT_ID)

    assert result["derived_status"] == "done"
    assert "delivery_hold" not in result


def test_no_dispatch_prref_evidence_partial_not_done(tmp_path):
    """No-dispatch pr_ref evidence path: merged pr_ref would derive 'done', but
    the explicit 'partial' marking vetoes it (declared=active → in_progress)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-nodispatch", phase="active", pr_ref="#100")
    _seed_pr_merged_ndjson(sd, 100)
    _set_delivery(sd, "T-nodispatch", 100, "partial")

    result = track_reconciler.reconcile_track(sd, "T-nodispatch", PROJECT_ID)

    assert result["derived_status"] == "in_progress"
    assert result.get("delivery_hold") is not None


def test_stale_row_from_unlinked_pr_does_not_hold(tmp_path):
    """A delivery row for a PR no longer in the current pr_ref is stale and
    must NOT hold the track (only currently-linked PRs count)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-stale-row", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-stale-row", "T-stale-row", state="completed")
    _seed_pr_merged_event(sd, "D-stale-row")
    _set_delivery(sd, "T-stale-row", 100, "complete")
    _set_delivery(sd, "T-stale-row", 999, "partial")  # unlinked leftover

    result = track_reconciler.reconcile_track(sd, "T-stale-row", PROJECT_ID)

    assert result["derived_status"] == "done"
    assert "delivery_hold" not in result


def test_corrupt_delivery_kind_fails_closed(tmp_path, caplog):
    """An unrecognized delivery_kind on an existing row holds (fail-closed)
    and logs at ERROR — never silently reads as 'complete'."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-corrupt", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-corrupt", "T-corrupt", state="completed")
    _seed_pr_merged_event(sd, "D-corrupt")
    _set_delivery(sd, "T-corrupt", 100, "corrupted")

    with caplog.at_level("ERROR", logger="track_reconciler"):
        result = track_reconciler.reconcile_track(sd, "T-corrupt", PROJECT_ID)

    assert result["derived_status"] == "in_progress"
    hold = result.get("delivery_hold")
    assert hold is not None
    assert "unrecognized" in hold["reason"]
    assert any("unrecognized delivery_kind" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Nomination tests (objective_reconcile.run_reconcile)
# ---------------------------------------------------------------------------

def test_partial_track_not_nominated_but_visible(tmp_path, monkeypatch):
    """Fail-closed AND visible at nomination: the held track is not a
    candidate (gh is never consulted for it) but appears in per_track with
    verdict 'delivery_hold' and a readable reason. Exit stays 0 (a hold is
    not a degradation)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-hold", phase="active", pr_ref="#100,#101")
    _seed_pr_merged_ndjson(sd, 100)
    _seed_pr_merged_ndjson(sd, 101)
    _set_delivery(sd, "T-hold", 100, "partial")
    _set_delivery(sd, "T-hold", 101, "partial")

    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({100: _merged_pr(100), 101: _merged_pr(101)}, call_log),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["nominated"] == 0
    assert summary["counts"]["confirmed"] == 0
    assert summary["counts"]["delivery_hold"] == 1

    per = summary["per_track"]
    assert len(per) == 1
    assert per[0]["track_id"] == "T-hold"
    assert per[0]["verdict"] == "delivery_hold"
    assert "2 of 2" in per[0]["reason"]

    # gh was never asked about the held track's PRs.
    assert not any(_is_gh_pr_view(cmd) for cmd in call_log)


def test_complete_track_nominated_and_confirmed_unchanged(tmp_path, monkeypatch):
    """Track with all linked PRs 'complete' → nominated + CONFIRMED (unchanged)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-ship", phase="active", pr_ref="#100")
    _seed_pr_merged_ndjson(sd, 100)
    _set_delivery(sd, "T-ship", 100, "complete")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run", _make_gh_mock({100: _merged_pr(100)})
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["nominated"] == 1
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["delivery_hold"] == 0
    assert summary["per_track"][0]["verdict"] == "CONFIRMED"


def test_unmarked_legacy_track_nominated(tmp_path, monkeypatch):
    """Legacy track with pr_ref but zero delivery rows → still nominated
    (grandfathered; pinning the step-1 measurement choice)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-old", phase="active", pr_ref="#100")
    _seed_pr_merged_ndjson(sd, 100)

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run", _make_gh_mock({100: _merged_pr(100)})
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["nominated"] == 1
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["delivery_hold"] == 0


def test_apply_mode_does_not_close_held_track(tmp_path, monkeypatch):
    """--apply: the held track is never nominated, so no close is attempted;
    declared phase is untouched and the hold is reported."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-no-close", phase="active", pr_ref="#100")
    _seed_pr_merged_ndjson(sd, 100)
    _set_delivery(sd, "T-no-close", 100, "partial")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run", _make_gh_mock({100: _merged_pr(100)})
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"]["closed"] == 0
    assert summary["counts"]["delivery_hold"] == 1
    assert summary["per_track"][0]["verdict"] == "delivery_hold"

    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    phase = conn.execute(
        "SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
        ("T-no-close", PROJECT_ID),
    ).fetchone()[0]
    conn.close()
    assert phase == "active"
