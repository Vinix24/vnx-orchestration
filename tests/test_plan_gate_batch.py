"""tests/test_plan_gate_batch.py — the plan-gate batch command (OPSCHALING point 4).

``vnx horizon plan-gate batch`` runs the SAME single-track gate
(``planning_cli.run_plan_gate_for_track``) over a set of tracks blocked on an
open OI-PLAN blocker. This proves, against a real store (no live model):

  - selection picks only OPEN OI-PLAN blockers (resolved/absent excluded);
  - ``--track`` + ``--limit`` compose and an unknown track id FAILS LOUD;
  - a thin goal is an OUTCOME (REFUSED_THIN) the batch counts and continues past;
  - a second run resumes from the on-disk progress store and only processes the
    remaining tracks (read back, not trusted from a return value);
  - ``--restart`` re-runs every selected track;
  - ``--dry-run`` shows class + seat count WITHOUT calling any model;
  - the seat count in the output matches the governance ladder.

Real model dispatch is out of scope (as in test_plan_gate_panel.py):
``pgp.run_panel`` is stubbed, but selection, the resume store, the thin-goal
refusal, the dry-run preview, and the seat derivation are the real code.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "scripts" / "lib"
_SCRIPTS = REPO_ROOT / "scripts"
_MIGRATIONS = REPO_ROOT / "schemas" / "migrations"
for _p in (str(_LIB), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_migration  # noqa: E402
import tracks  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402


_THICK_GOAL = "Ship a coherent plan for the widget. " * 10  # 390 chars


def _bootstrap(tmp_path: Path) -> Path:
    """A pre-migrated store (tracks + plan-gate schema), same as
    test_plan_gate_goal_state_plan.py::_bootstrap."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev', "
        "state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT, "
        "priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT, "
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "expires_after TEXT, metadata_json TEXT DEFAULT '{}', "
        "UNIQUE(dispatch_id, project_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coordination_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT, from_state TEXT, "
        "to_state TEXT, actor TEXT, reason TEXT, metadata_json TEXT, occurred_at TEXT, project_id TEXT)"
    )
    conn.commit()
    # Migration 0022 REBUILDS the dispatches table (RENAME + CREATE + DROP), so
    # the canonical columns (output_ref, output_kind, …) must be added AFTER it
    # runs — adding them before is silently wiped by the rebuild.
    for version, filename in [
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.commit()
    for version, filename in [
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (29, "0029_track_type_discriminator.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
        (33, "0033_track_decision_ref.sql"),
    ]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return state_dir


def _pass_result(track_id: str, project_id: str, panel: list) -> dict:
    return {
        "track_id": track_id, "project_id": project_id, "decision": "PASS",
        "summary": {"decision": "PASS", "pass_count": len(panel),
                    "revise_count": 0, "block_count": 0, "rationale": "ok"},
        "panelists": [], "doc_truncation": {"truncated": False},
    }


def _make_track(state_dir: Path, track_id: str, goal: str) -> None:
    """Create a track AND seed it with one deliverable.

    Plan-gate now requires at least one deliverable on the goal_state path
    (see test_plan_gate_deliverables_source.py) — every track in this file
    that is meant to reach the panel needs one. Tracks meant to be refused for
    a too-thin goal are unaffected: the thin-goal check fires before the
    deliverables check, so the extra deliverable is a harmless no-op there.
    """
    tracks.create_track(state_dir, track_id, "p1", "t", goal, phase="queued")
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id, "--output-kind", "pr",
        "--title", f"Ship {track_id}", "--project-id", "p1",
        "--state-dir", str(state_dir),
    ])
    assert rc == 0, f"failed to seed a deliverable for {track_id!r}"


def _seed_blocker(state_dir: Path, track_id: str) -> None:
    assert planning_cli._seed_plan_blocker(state_dir, track_id, "p1"), (
        f"failed to seed plan blocker for {track_id!r}"
    )


def _stub_panel(monkeypatch, tmp_path: Path, captured: list) -> None:
    """Stub model dispatch + seat config + blocker resolution so the batch runs
    end-to-end against a real store without a live provider or repo config."""
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")

    def _capture_run_panel(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        captured.append({"doc_path": doc_path, "doc_text": doc_text, "panel": panel})
        return _pass_result(track_id, project_id, panel)

    monkeypatch.setattr(pgp, "run_panel", _capture_run_panel)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)


def _batch_namespace(state_dir: Path, **overrides) -> argparse.Namespace:
    base = dict(
        project_id="p1", state_dir=str(state_dir),
        track=None, limit=None, restart=False, dry_run=False, json=False,
        panel_seats="", seat_timeout=None, dispatch_paths=None, task_class=None,
        irreversible=False, repo_root=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_selection_picks_only_open_plan_blockers(tmp_path):
    """Only tracks with an OPEN OI-PLAN blocker are selected: a resolved blocker
    and a never-seeded track are both excluded."""
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "open", _THICK_GOAL)
    _make_track(state_dir, "resolved", _THICK_GOAL)
    _make_track(state_dir, "never", _THICK_GOAL)
    _seed_blocker(state_dir, "open")
    _seed_blocker(state_dir, "resolved")
    # Lift the blocker on "resolved": it must drop out of the selection.
    planning_cli._resolve_plan_blocker(state_dir, "resolved", "p1", reason="test", resolver="test")

    selected = planning_cli._resolve_batch_selection(
        state_dir, "p1", track_filter=None, limit=None,
    )
    assert selected == ["open"]
    assert planning_cli.list_plan_blocked_tracks(state_dir, "p1") == ["open"]


def test_limit_and_track_compose_and_unknown_fails_loud(tmp_path):
    """--track and --limit compose (filter first, then cap); an unknown --track
    id raises instead of silently skipping."""
    state_dir = _bootstrap(tmp_path)
    for tid in ("a", "b", "c", "d"):
        _make_track(state_dir, tid, _THICK_GOAL)
        _seed_blocker(state_dir, tid)

    # Explicit set honors the given order and does not silently exclude.
    assert planning_cli._resolve_batch_selection(
        state_dir, "p1", track_filter=["c", "a"], limit=None,
    ) == ["c", "a"]

    # --limit caps an explicit set.
    assert planning_cli._resolve_batch_selection(
        state_dir, "p1", track_filter=["c", "a"], limit=1,
    ) == ["c"]

    # --limit caps the full open-blocker set.
    assert planning_cli._resolve_batch_selection(
        state_dir, "p1", track_filter=None, limit=2,
    ) == ["a", "b"]

    with pytest.raises(ValueError):
        planning_cli._resolve_batch_selection(
            state_dir, "p1", track_filter=["unknown-id"], limit=None,
        )


# ---------------------------------------------------------------------------
# Thin goal + resume/restart/interrupt (via the injectable loop)
# ---------------------------------------------------------------------------

def _execute(state_dir, selection, monkeypatch, tmp_path, *, run_kwargs=None, restart=False, interrupt_check=None):
    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)
    return captured, planning_cli._execute_batch(
        selection, state_dir=str(state_dir), project_id="p1",
        run_kwargs=run_kwargs or {}, restart=restart, min_goal_chars=200,
        interrupt_check=interrupt_check,
    )


def test_thin_goal_is_an_outcome_and_batch_continues(tmp_path, monkeypatch):
    """A thin goal is counted as GEWEIGERD-te-dun (REFUSED_THIN), never crashes
    the batch, and does not stop the other tracks from gating."""
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "thin", "too short")
    _make_track(state_dir, "thick", _THICK_GOAL)
    _seed_blocker(state_dir, "thin")
    _seed_blocker(state_dir, "thick")

    captured, summary = _execute(state_dir, ["thin", "thick"], monkeypatch, tmp_path)

    assert summary["interrupted"] is False
    assert summary["skipped"] == []
    assert summary["tally"] == {
        "PASS": 1, "REVISE": 0, "REFUSED_THIN": 1, "REFUSED_NO_DELIVERABLES": 0, "ERROR": 0,
    }

    by_track = {r["track_id"]: r for r in summary["results"]}
    assert by_track["thin"]["outcome"] == "REFUSED_THIN"
    assert by_track["thin"]["seats"] == 0
    assert by_track["thin"]["thin_length"] == len("too short".strip())
    assert by_track["thick"]["outcome"] == "PASS"

    # Only the thick goal reached the panel — the thin one was refused first.
    assert len(captured) == 1
    assert captured[0]["doc_path"] is None
    assert _THICK_GOAL.strip() in captured[0]["doc_text"]
    assert "Ship thick" in captured[0]["doc_text"]  # the seeded deliverable's title


def test_resume_skips_completed_and_reads_store_back(tmp_path, monkeypatch):
    """Run 1 processes 2 of 4 (interrupted); run 2 reads the store back and only
    processes the remaining 2 — each track gates exactly once."""
    state_dir = _bootstrap(tmp_path)
    for tid in ("a", "b", "c", "d"):
        _make_track(state_dir, tid, _THICK_GOAL)
        _seed_blocker(state_dir, tid)

    calls: list = []
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", lambda *a, **k: True)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    def _capture_run_panel(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        calls.append(track_id)
        return _pass_result(track_id, project_id, panel)

    monkeypatch.setattr(pgp, "run_panel", _capture_run_panel)

    counter = {"n": 0}

    def _stop_after_two():
        counter["n"] += 1
        return counter["n"] > 2

    run1 = planning_cli._execute_batch(
        ["a", "b", "c", "d"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={}, restart=False, min_goal_chars=200,
        interrupt_check=_stop_after_two,
    )
    assert [r["track_id"] for r in run1["results"]] == ["a", "b"]
    assert run1["interrupted"] is True
    assert run1["tally"]["PASS"] == 2

    # The progress store is on disk and carries the two completed tracks.
    store = planning_cli.load_batch_progress(state_dir)
    assert set(store) == {"a", "b"}

    run2 = planning_cli._execute_batch(
        ["a", "b", "c", "d"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={}, restart=False, min_goal_chars=200,
    )
    assert [r["track_id"] for r in run2["results"]] == ["c", "d"]
    assert run2["skipped"] == ["a", "b"]
    assert run2["interrupted"] is False

    # Each track gated exactly once across the two runs — no silent re-run.
    assert calls == ["a", "b", "c", "d"]


def test_restart_reruns_all_selected_tracks(tmp_path, monkeypatch):
    """--restart ignores the resume store and re-runs every selected track."""
    state_dir = _bootstrap(tmp_path)
    for tid in ("a", "b", "c", "d"):
        _make_track(state_dir, tid, _THICK_GOAL)
        _seed_blocker(state_dir, tid)

    captured, _ = _execute(state_dir, ["a", "b"], monkeypatch, tmp_path)
    assert len(captured) == 2

    captured2, run2 = _execute(
        state_dir, ["a", "b", "c", "d"], monkeypatch, tmp_path, restart=True,
    )
    assert len(captured2) == 4
    assert run2["skipped"] == []
    assert sorted(r["track_id"] for r in run2["results"]) == ["a", "b", "c", "d"]


# ---------------------------------------------------------------------------
# --dry-run + rendered output + seat ladder
# ---------------------------------------------------------------------------

def test_dry_run_calls_no_model_and_shows_class_and_seats(tmp_path, monkeypatch, capsys):
    """--dry-run previews class + seat count and calls NO model and writes NO
    progress store."""
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "feat", _THICK_GOAL)
    _make_track(state_dir, "thin", "too short")
    _seed_blocker(state_dir, "feat")
    _seed_blocker(state_dir, "thin")

    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")

    def _no_model(*a, **k):
        raise AssertionError("--dry-run must not call any model")

    monkeypatch.setattr(pgp, "run_panel", _no_model)
    monkeypatch.setattr(planning_cli, "_resolve_plan_blocker", _no_model)

    rc = planning_cli.cmd_plan_gate_batch(_batch_namespace(state_dir, dry_run=True))
    out = capsys.readouterr().out

    assert rc == 0
    assert "no model will be called" in out
    assert "class=" in out
    assert "seats=" in out
    assert "thin-goal" in out  # the thin goal is flagged in the preview
    assert not (state_dir / planning_cli._BATCH_PROGRESS_FILENAME).exists()


def test_seat_count_matches_ladder(tmp_path, monkeypatch):
    """The derived seat count matches the governance ladder: default=2, a new
    feature (task_class 01_code_generation)=all seats."""
    state_dir = _bootstrap(tmp_path)

    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    _make_track(state_dir, "feat-default", _THICK_GOAL)
    _seed_blocker(state_dir, "feat-default")
    _make_track(state_dir, "feat-new", _THICK_GOAL)
    _seed_blocker(state_dir, "feat-new")

    summary = planning_cli._execute_batch(
        ["feat-default", "feat-new"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={}, restart=False, min_goal_chars=200,
    )
    by_track = {r["track_id"]: r for r in summary["results"]}
    assert by_track["feat-default"]["seats"] == 2, "default variant -> opus + kimi = 2 seats"
    assert len(captured[0]["panel"]) == 2

    captured2: list = []
    _stub_panel(monkeypatch, tmp_path, captured2)
    summary2 = planning_cli._execute_batch(
        ["feat-new"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={"task_class": "01_code_generation"}, restart=True,
        min_goal_chars=200,
    )
    assert summary2["results"][0]["seats"] == len(pgp.DEFAULT_PANEL), (
        "new feature always runs the full panel"
    )
    assert len(captured2[0]["panel"]) == len(pgp.DEFAULT_PANEL)


# ---------------------------------------------------------------------------
# Stop-rule inheritance: the batch drives the SAME shared path, so a track at
# the round threshold gets the tiebreaker, not a third full panel.
# ---------------------------------------------------------------------------

def test_batch_inherits_tiebreaker_after_two_panel_rounds(tmp_path, monkeypatch):
    """A track that already has two panel rounds gets the tiebreaker in the
    batch, NOT a third full panel.

    The batch calls ``run_plan_gate_for_track`` (the single shared path the
    interactive ``plan-gate run`` also drives). That path carries the stop-rule
    from #1520, so a track at the threshold routes to ``run_tiebreaker`` — the
    batch does not grow a second panel call that would skip the stop-rule. This
    asserts on WHICH function ran (tiebreaker vs panel), not a log line.
    """
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "feat-tb", _THICK_GOAL)
    _seed_blocker(state_dir, "feat-tb")

    # Force two completed panel rounds into an isolated seat ledger so the next
    # gate run hits the stop-rule threshold (read back from disk, not a return
    # value — the same "meet de state" discipline as the tiebreaker suite).
    ledger = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: ledger)
    pgt.record_round(ledger, track_id="feat-tb", project_id="p1", round_number=1, outcome="panel")
    pgt.record_round(ledger, track_id="feat-tb", project_id="p1", round_number=2, outcome="panel")
    assert pgt.read_round_count(ledger, "feat-tb", "p1") == 2

    panel_calls = {"n": 0}
    tb_calls = {"n": 0}

    def _no_panel(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        panel_calls["n"] += 1
        return _pass_result(track_id, project_id, panel)

    def _tiebreaker(doc_path, *, doc_text=None, track_id, project_id, round_number,
                    last_round_findings, data_dir, timeout_seconds, config, model_arg=None):
        tb_calls["n"] += 1
        return pgt.TiebreakerResult(
            outcome="START", model="fable-5", round=round_number,
            required_change="", rationale="good enough",
        )

    monkeypatch.setattr(pgp, "run_panel", _no_panel)
    monkeypatch.setattr(pgt, "run_tiebreaker", _tiebreaker)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    # _resolve_plan_blocker stays REAL so the tiebreaker resolution actually
    # lands on the track (and the blocker actually clears).
    summary = planning_cli._execute_batch(
        ["feat-tb"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={}, restart=False, min_goal_chars=200,
    )

    assert summary["interrupted"] is False
    assert summary["skipped"] == []
    # Exactly ONE tiebreaker, zero full-panel runs: the stop-rule inherited.
    assert tb_calls["n"] == 1
    assert panel_calls["n"] == 0

    rec = summary["results"][0]
    assert rec["outcome"] == "PASS"
    assert rec["decision"] == "START"
    assert rec["variant"] == "tiebreaker"
    assert rec["seats"] == 0
    assert rec["still_blocked"] is False
    assert "tiebreaker START" in rec["detail"]


def test_panel_round_records_governance_variant_and_trace_to_seat_ledger(tmp_path, monkeypatch):
    """A non-PASS panel round persists the governance decision that sized it.

    ``governance_variant`` (the seat-ladder outcome) + ``gov_trace`` (the reason:
    weight, seat count, chosen-by) used to be printed to stderr and dropped — the
    on-disk ``plan_gate_round`` record carried the round number and outcome but
    not WHY the round had the seats it had. This drives the REAL
    ``run_plan_gate_for_track`` write path (only ``run_panel`` is stubbed, to a
    REVISE so ``record_round`` fires) and reads the seat ledger back from disk,
    so the assertion is on what LANDED, not on an in-memory dict. Fails as soon
    as either field falls out of the ``record_round`` call in
    ``run_plan_gate_for_track`` (or out of ``record_round``'s record dict).
    """
    monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: tmp_path / "absent.yaml")
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "feat-gov", _THICK_GOAL)
    _seed_blocker(state_dir, "feat-gov")

    ledger = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: ledger)

    def _revise(doc_path, *, doc_text=None, track_id, project_id, panel, data_dir, **kw):
        return {
            "track_id": track_id, "project_id": project_id, "decision": "REVISE",
            "summary": {"decision": "REVISE", "pass_count": 0,
                        "revise_count": 2, "block_count": 0,
                        "rationale": "gaps remain"},
            "panelists": [], "doc_truncation": {"truncated": False},
        }

    monkeypatch.setattr(pgp, "run_panel", _revise)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    summary = planning_cli._execute_batch(
        ["feat-gov"], state_dir=str(state_dir), project_id="p1",
        run_kwargs={}, restart=False, min_goal_chars=200,
    )
    assert summary["results"][0]["outcome"] == "REVISE"

    from ndjson_hash_chain import walk_chain  # noqa: PLC0415
    rounds = [
        rec for _ln, rec, _h in walk_chain(ledger)
        if rec.get("type") == pgt.ROUND_RECORD_TYPE
    ]
    assert rounds, "expected a plan_gate_round record for the REVISE panel round"
    rec = rounds[-1]
    assert rec["governance_variant"] == "default", (
        "the derived seat-ladder variant must land on the round record"
    )
    assert "weight=default" in rec["gov_trace"], (
        "the gov_trace must carry the derived weight"
    )
    assert "seat(s) by derived" in rec["gov_trace"], (
        "the gov_trace must carry the seat count + chosen-by"
    )


def test_batch_renders_tally_line_and_outcome_labels(tmp_path, monkeypatch, capsys):
    """The rendered batch output carries the per-outcome tally, the per-track
    seat count, and the Dutch REFUSED_THIN label — and the tally sums to the
    processed count."""
    state_dir = _bootstrap(tmp_path)
    _make_track(state_dir, "feat", _THICK_GOAL)
    _make_track(state_dir, "thin", "too short")
    _seed_blocker(state_dir, "feat")
    _seed_blocker(state_dir, "thin")

    captured: list = []
    _stub_panel(monkeypatch, tmp_path, captured)

    rc = planning_cli.cmd_plan_gate_batch(_batch_namespace(state_dir))
    out = capsys.readouterr().out

    assert rc == 0
    assert "Tally:" in out
    assert "PASS=1" in out
    assert "GEWEIGERD-te-dun=1" in out
    assert "2 processed" in out
    assert "2 seat(s)" in out  # the PASS track carried the default 2-seat ladder


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
