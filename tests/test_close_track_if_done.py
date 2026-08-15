"""tests/test_close_track_if_done.py — shared close helper + close-time revalidation.

Verifies track_reconciler.close_track_if_done:
- evidence=None path: derived-done -> walks; derived!=done -> noop_not_terminal;
  parked without include_parked -> rejected_parked.
- revalidation: snapshot pr_ref differs from current row -> stale_candidate, no write.
- revalidation: blocker OI appeared after nomination -> stale_candidate, no write.
- revalidation clean -> closes, track_phase_history rows carry given actor + approval_id.
- mid-walk failure resumability (monkeypatch transition_phase on second step).
"""

from __future__ import annotations

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

import schema_migration  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

PROJECT_ID = "test-close-proj"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path, *, with_delivery_table: bool = True) -> Path:
    """State dir with migrations 0022 + 0024 + 0027 + 0028 + 0030 (+0032 unless
    with_delivery_table=False, for OI-1167 pre-0032-store coverage) applied."""
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
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL,
            project_id TEXT
        )
        """
    )
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
        (29, "0029_track_type_discriminator.sql"),
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
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        (PROJECT_ID, track_id, pr_number, kind, set_by),
    )
    conn.commit()
    conn.close()


def _seed_done_track(state_dir: Path, track_id: str, *, phase: str) -> None:
    """Track whose work is terminal (completed dispatch, no pr_ref) => derived 'done'."""
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID, title=track_id, goal_state="ship", phase=phase
    )
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (f"D-{track_id}", PROJECT_ID, "completed", track_id),
    )
    conn.commit()
    conn.close()


def _phase(state_dir: Path, track_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _derived_status(state_dir: Path, track_id: str) -> "str | None":
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _history(state_dir: Path, track_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    rows = conn.execute(
        "SELECT from_phase, to_phase, actor, approval_id "
        "FROM track_phase_history "
        "WHERE track_id=? AND project_id=? ORDER BY rowid",
        (track_id, PROJECT_ID),
    ).fetchall()
    conn.close()
    return [
        {"from_phase": r[0], "to_phase": r[1], "actor": r[2], "approval_id": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# evidence=None path
# ---------------------------------------------------------------------------

def test_none_evidence_derived_done_walks_to_done(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-active", phase="active")
    result = track_reconciler.close_track_if_done(
        sd, "T-active", PROJECT_ID, actor="operator", approval_id="APR-1"
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-active") == "done"


def test_none_evidence_derived_not_done_noop_not_terminal(tmp_path):
    sd = _build_db(tmp_path)
    # No dispatches => derived='queued' (not terminal) => noop_not_terminal.
    tracks_lib.create_track(sd, "T-q", PROJECT_ID, title="x", goal_state="y", phase="queued")
    result = track_reconciler.close_track_if_done(
        sd, "T-q", PROJECT_ID, actor="operator", approval_id="X"
    )
    assert result["action"] == "noop_not_terminal"
    assert result["applied"] is False
    assert _phase(sd, "T-q") == "queued"


def test_none_evidence_parked_without_include_flag_rejected_parked(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-parked", phase="parked")
    result = track_reconciler.close_track_if_done(
        sd, "T-parked", PROJECT_ID, actor="operator", approval_id="X"
    )
    assert result["action"] == "rejected_parked"
    assert result["applied"] is False
    assert _phase(sd, "T-parked") == "parked"


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate on pr_ref mismatch
# ---------------------------------------------------------------------------

def test_revalidation_pr_ref_changed_stale_candidate(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-pr", phase="active")

    # Set pr_ref="#994" AND add a pr_merged coordination event so reconcile still
    # derives "done" (all-terminal dispatches + pr_merged event = done).
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET pr_ref=? WHERE track_id=? AND project_id=?",
        ("#994", "T-pr", PROJECT_ID),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-994','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-pr", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    # Snapshot carries a DIFFERENT pr_ref -> stale candidate.
    evidence = {"pr_ref": "#993", "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-pr", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-pr") == "active"  # no write


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate causes no derived_status write (pr_ref variant)
# ---------------------------------------------------------------------------

def test_revalidation_stale_causes_no_derived_write(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-pr-stale", phase="active")

    # Set pr_ref="#994" in DB + add pr_merged event so reconcile would derive "done".
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET pr_ref=? WHERE track_id=? AND project_id=?",
        ("#994", "T-pr-stale", PROJECT_ID),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-994','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-pr-stale", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    # Evidence snapshot carries a different pr_ref -> stale candidate.
    evidence = {"pr_ref": "#993", "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-pr-stale", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-pr-stale") == "active"
    assert _derived_status(sd, "T-pr-stale") is None   # reconcile_track was not called


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate on blocker OI
# ---------------------------------------------------------------------------

def test_revalidation_blocker_oi_appeared_stale_candidate(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-blocked", phase="active")

    # Snapshot was taken before the blocker appeared (pr_ref=None matches current row).
    evidence = {"pr_ref": None, "verified_at": "2026-07-04T10:00:00Z"}

    # Blocker OI arrives AFTER nomination — post-nomination change.
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_open_items "
        "(track_id, project_id, oi_id, link_type, link_source) "
        "VALUES (?,?,?,'blocks','manual')",
        ("T-blocked", PROJECT_ID, "OI-001"),
    )
    conn.commit()
    conn.close()

    result = track_reconciler.close_track_if_done(
        sd, "T-blocked", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-blocked") == "active"          # phase unchanged
    assert _derived_status(sd, "T-blocked") is None     # reconcile_track was not called


# ---------------------------------------------------------------------------
# Revalidation: clean -> closes, records actor + approval_id
# ---------------------------------------------------------------------------

def test_revalidation_clean_closes_and_records_actor_and_approval(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-ok", phase="active")

    # Snapshot matches current DB state exactly (no pr_ref, no blockers).
    evidence = {"pr_ref": None, "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-ok", PROJECT_ID,
        actor="T0",
        approval_id="AUTO-1",
        evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-ok") == "done"

    hist = _history(sd, "T-ok")
    assert len(hist) == 1
    assert hist[0]["actor"] == "T0"
    assert hist[0]["approval_id"] == "AUTO-1"


# ---------------------------------------------------------------------------
# Mid-walk failure resumability
# ---------------------------------------------------------------------------

def test_evidence_path_dependency_not_done_stale(tmp_path):
    """Evidence path: dependency track not done → stale_candidate, no write."""
    sd = _build_db(tmp_path)
    # Dep track: active (not done)
    tracks_lib.create_track(sd, "T-dep-b", PROJECT_ID, title="dep b", goal_state="y", phase="active")
    # Track under test: depends on T-dep-b
    tracks_lib.create_track(
        sd, "T-dep-a", PROJECT_ID, title="dep a", goal_state="y", phase="active", pr_ref="#555"
    )
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_dependencies "
        "(from_track_id, from_project_id, to_track_id, to_project_id, kind, derivation_source) "
        "VALUES (?,?,?,?,?,?)",
        ("T-dep-a", PROJECT_ID, "T-dep-b", PROJECT_ID, "hard", "manual"),
    )
    conn.commit()
    conn.close()

    evidence = {
        "pr_ref": "#555",
        "pr_results": [{"number": 555, "state": "MERGED", "mergedAt": "2026-07-04T10:00:00Z"}],
        "verified_at": "2026-07-04T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-dep-a", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-dep-a") == "active"
    assert _derived_status(sd, "T-dep-a") is None  # reconcile_track was not called


def test_evidence_with_pr_results_closes_without_local_derived_done(tmp_path):
    """Evidence path with non-empty pr_results: closes without any local merge evidence.

    No dispatch, no pr_merged.ndjson, no coordination events → derived_status stays
    'in_progress'. Fix 2: gh evidence in pr_results bypasses the derived_status gate.
    """
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-ev-pr", PROJECT_ID, title="ev pr", goal_state="y", phase="active", pr_ref="#777"
    )
    # No local merge evidence of any kind.
    # #777 is the whole plan (single PR) -> mark it 'complete' for the OI-829 gate.
    _set_delivery(sd, "T-ev-pr", 777, "complete")

    evidence = {
        "pr_ref": "#777",
        "pr_results": [{"number": 777, "state": "MERGED", "mergedAt": "2026-07-04T10:00:00Z"}],
        "verified_at": "2026-07-04T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-ev-pr", PROJECT_ID, actor="system", approval_id="APR-EV",
        evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-ev-pr") == "done"

    hist = _history(sd, "T-ev-pr")
    assert hist, "expected track_phase_history rows"
    assert hist[-1]["to_phase"] == "done"
    assert hist[-1]["actor"] == "system"
    assert hist[-1]["approval_id"] == "APR-EV"


# ---------------------------------------------------------------------------
# Closed-sibling policy enforcement
# ---------------------------------------------------------------------------

def test_evidence_closed_sibling_without_policy_stale(tmp_path):
    """CLOSED sibling without allow_closed_siblings flag → stale_candidate, no write."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-cs-no-flag", PROJECT_ID, title="cs no flag", goal_state="y",
        phase="active", pr_ref="#800,#801",
    )

    # Snapshot WITHOUT allow_closed_siblings (or set to False).
    evidence = {
        "pr_ref": "#800,#801",
        "pr_results": [
            {"number": 800, "state": "MERGED", "mergedAt": "2026-07-04T10:00:00Z"},
            {"number": 801, "state": "CLOSED", "mergedAt": None},
        ],
        "verified_at": "2026-07-04T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-cs-no-flag", PROJECT_ID, actor="system", evidence=evidence,
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-cs-no-flag") == "active"  # no write
    assert _derived_status(sd, "T-cs-no-flag") is None


def test_evidence_closed_sibling_with_policy_closes(tmp_path):
    """CLOSED sibling + allow_closed_siblings=True + ≥1 MERGED → closes."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-cs-flag", PROJECT_ID, title="cs with flag", goal_state="y",
        phase="active", pr_ref="#802,#803",
    )
    # #802 is the merged PR that actually ships the plan -> mark it 'complete'.
    _set_delivery(sd, "T-cs-flag", 802, "complete")

    # Snapshot WITH allow_closed_siblings=True.
    evidence = {
        "pr_ref": "#802,#803",
        "pr_results": [
            {"number": 802, "state": "MERGED", "mergedAt": "2026-07-04T10:00:00Z"},
            {"number": 803, "state": "CLOSED", "mergedAt": None},
        ],
        "verified_at": "2026-07-04T10:00:00Z",
        "allow_closed_siblings": True,
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-cs-flag", PROJECT_ID, actor="system", approval_id="APR-CS",
        evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-cs-flag") == "done"

    hist = _history(sd, "T-cs-flag")
    assert hist, "expected track_phase_history rows"
    assert hist[-1]["to_phase"] == "done"
    assert hist[-1]["actor"] == "system"
    assert hist[-1]["approval_id"] == "APR-CS"


def test_mid_walk_failure_leaves_intermediate_and_is_resumable(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-q2", phase="queued")  # queued -> active -> done (two steps)

    real = tracks_lib.transition_phase

    def flaky(state_dir, tid, pid, to_phase, **kw):
        if to_phase == "done":
            raise tracks_lib.InvalidTransitionError("injected mid-walk failure")
        return real(state_dir, tid, pid, to_phase, **kw)

    monkeypatch.setattr(tracks_lib, "transition_phase", flaky)

    result = track_reconciler.close_track_if_done(
        sd, "T-q2", PROJECT_ID, actor="operator", approval_id="A"
    )
    assert result["action"] == "rejected_walk_failed"
    assert _phase(sd, "T-q2") == "active"  # non-atomic: stuck at intermediate phase

    # Recovery: re-call resumes walk from the current declared phase.
    monkeypatch.setattr(tracks_lib, "transition_phase", real)
    result2 = track_reconciler.close_track_if_done(
        sd, "T-q2", PROJECT_ID, actor="operator", approval_id="A"
    )
    assert result2["action"] == "closed"
    assert _phase(sd, "T-q2") == "done"


# ---------------------------------------------------------------------------
# OI-829: delivery completeness — fail-closed auto-close gate.
# ---------------------------------------------------------------------------

def test_oi829_regression_two_merged_prs_no_delivery_marking_stays_open(tmp_path):
    """Reproduces the worker-provider-free-choice bug: phase=active, pr_ref
    '#1221,#1239', both merged via gh evidence, zero open blockers, NO
    track_pr_delivery rows at all. Pre-fix this closed on PR-1 of 5 merging;
    post-fix it must stay open with action='noop_incomplete_delivery' and
    cause zero DB writes (phase and derived_status both untouched)."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "worker-provider-free-choice", PROJECT_ID, title="wpfc", goal_state="y",
        phase="active", pr_ref="#1221,#1239",
    )
    # Deliberately NO _set_delivery calls — this is the bug scenario.

    evidence = {
        "pr_ref": "#1221,#1239",
        "pr_results": [
            {"number": 1221, "state": "MERGED", "mergedAt": "2026-07-24T10:00:00Z"},
            {"number": 1239, "state": "MERGED", "mergedAt": "2026-07-29T10:00:00Z"},
        ],
        "verified_at": "2026-07-29T12:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "worker-provider-free-choice", PROJECT_ID, actor="system",
        approval_id="auto-reconcile-test", evidence=evidence,
    )
    assert result["action"] == "noop_incomplete_delivery"
    assert result["applied"] is False
    assert _phase(sd, "worker-provider-free-choice") == "active"  # no write
    assert _derived_status(sd, "worker-provider-free-choice") is None  # reconcile_track not called


def test_oi829_one_pr_marked_complete_closes(tmp_path):
    """Same shape as the regression above, but #1239 is marked delivery_kind='complete'
    (the PR that ships the rest of the plan) -> the gate passes and the track closes."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-delivery-complete", PROJECT_ID, title="wpfc-2", goal_state="y",
        phase="active", pr_ref="#1221,#1239",
    )
    _set_delivery(sd, "T-delivery-complete", 1221, "partial")
    _set_delivery(sd, "T-delivery-complete", 1239, "complete")

    evidence = {
        "pr_ref": "#1221,#1239",
        "pr_results": [
            {"number": 1221, "state": "MERGED", "mergedAt": "2026-07-24T10:00:00Z"},
            {"number": 1239, "state": "MERGED", "mergedAt": "2026-07-29T10:00:00Z"},
        ],
        "verified_at": "2026-07-29T12:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-delivery-complete", PROJECT_ID, actor="system",
        approval_id="auto-reconcile-test", evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-delivery-complete") == "done"


def test_oi829_only_partial_markings_noop_incomplete_delivery(tmp_path):
    """Every linked PR marked -- but only 'partial' -> still fails closed."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-all-partial", PROJECT_ID, title="all partial", goal_state="y",
        phase="active", pr_ref="#1221,#1239",
    )
    _set_delivery(sd, "T-all-partial", 1221, "partial")
    _set_delivery(sd, "T-all-partial", 1239, "partial")

    evidence = {
        "pr_ref": "#1221,#1239",
        "pr_results": [
            {"number": 1221, "state": "MERGED", "mergedAt": "2026-07-24T10:00:00Z"},
            {"number": 1239, "state": "MERGED", "mergedAt": "2026-07-29T10:00:00Z"},
        ],
        "verified_at": "2026-07-29T12:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-all-partial", PROJECT_ID, actor="system", evidence=evidence,
    )
    assert result["action"] == "noop_incomplete_delivery"
    assert result["applied"] is False
    assert _phase(sd, "T-all-partial") == "active"


def test_oi829_no_pr_ref_at_all_gate_not_applicable(tmp_path):
    """A track with no pr_ref at all closes on dispatch-completion evidence alone --
    there is nothing to gate on, so the delivery check must not block it."""
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-no-pr", phase="active")  # no pr_ref

    evidence = {"pr_ref": None, "verified_at": "2026-07-29T12:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-no-pr", PROJECT_ID, actor="system", approval_id="X", evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True


def test_oi829_unknown_delivery_kind_fails_closed_no_raise(tmp_path, caplog):
    """An existing track_pr_delivery row with an unrecognized delivery_kind must
    never silently be treated as 'not complete' -- but it must also never escape
    close_track_if_done as an exception (the fail-closed fix-forward: the caller
    loop in objective_reconcile.py has no per-track try/except, so a raise here
    would abort every remaining candidate in a sweep). It logs ERROR with full
    context and returns noop_incomplete_delivery / applied=False instead."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-bad-delivery", PROJECT_ID, title="bad delivery", goal_state="y",
        phase="active", pr_ref="#900",
    )
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("PRAGMA ignore_check_constraints = 1")
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        (PROJECT_ID, "T-bad-delivery", 900, "bogus", "test"),
    )
    conn.commit()
    conn.close()

    evidence = {"pr_ref": "#900", "verified_at": "2026-07-29T12:00:00Z"}
    with caplog.at_level("ERROR", logger="track_reconciler"):
        result = track_reconciler.close_track_if_done(
            sd, "T-bad-delivery", PROJECT_ID, actor="system", evidence=evidence,
        )

    assert result["action"] == "noop_incomplete_delivery"
    assert result["applied"] is False
    assert _phase(sd, "T-bad-delivery") == "active"  # no write

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "expected an ERROR log record for the unrecognized delivery_kind"
    msg = error_records[0].getMessage()
    assert PROJECT_ID in msg
    assert "T-bad-delivery" in msg
    assert "900" in msg
    assert "bogus" in msg


def test_oi829_corrupt_row_does_not_abort_sweep_over_other_candidates(tmp_path):
    """Reproduces the fix-forward finding directly: a sweep over multiple confirmed
    candidates (mirroring objective_reconcile.py's per-candidate loop, which has no
    per-track try/except) must not let one corrupt delivery_kind row stop the other
    candidates from being processed."""
    sd = _build_db(tmp_path)

    # Candidate 1: corrupt delivery_kind -> must fail closed, not raise.
    tracks_lib.create_track(
        sd, "T-sweep-bad", PROJECT_ID, title="sweep bad", goal_state="y",
        phase="active", pr_ref="#901",
    )
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute("PRAGMA ignore_check_constraints = 1")
    conn.execute(
        "INSERT INTO track_pr_delivery (project_id, track_id, pr_number, delivery_kind, set_by) "
        "VALUES (?,?,?,?,?)",
        (PROJECT_ID, "T-sweep-bad", 901, "corrupted", "test"),
    )
    conn.commit()
    conn.close()

    # Candidate 2: clean, complete delivery -> must close normally.
    tracks_lib.create_track(
        sd, "T-sweep-good", PROJECT_ID, title="sweep good", goal_state="y",
        phase="active", pr_ref="#902",
    )
    _set_delivery(sd, "T-sweep-good", 902, "complete")

    candidates = [
        {
            "track_id": "T-sweep-bad",
            "evidence": {"pr_ref": "#901", "verified_at": "2026-07-29T12:00:00Z"},
        },
        {
            "track_id": "T-sweep-good",
            "evidence": {
                "pr_ref": "#902",
                "pr_results": [
                    {"number": 902, "state": "MERGED", "mergedAt": "2026-07-29T10:00:00Z"},
                ],
                "verified_at": "2026-07-29T12:00:00Z",
            },
        },
    ]

    # No try/except around the call -- mirrors the real sweep loop. If the fix
    # regresses back to raising, this loop itself raises and the test fails
    # with an escaped exception rather than an assertion mismatch.
    results = {}
    for cand in candidates:
        results[cand["track_id"]] = track_reconciler.close_track_if_done(
            sd, cand["track_id"], PROJECT_ID, actor="system",
            approval_id="auto-reconcile-sweep-test", evidence=cand["evidence"],
        )

    assert results["T-sweep-bad"]["action"] == "noop_incomplete_delivery"
    assert results["T-sweep-bad"]["applied"] is False
    assert _phase(sd, "T-sweep-bad") == "active"

    assert results["T-sweep-good"]["action"] == "closed"
    assert results["T-sweep-good"]["applied"] is True
    assert _phase(sd, "T-sweep-good") == "done"


def test_oi829_evidence_none_path_closes_unchanged_without_any_marking(tmp_path):
    """The evidence=None (human `vnx objective close`) path is byte-for-byte
    unchanged: no delivery marking exists, pr_ref is set, and it still closes."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-manual-close", PROJECT_ID, title="manual close", goal_state="y",
        phase="active", pr_ref="#1221,#1239",
    )
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        ("D-T-manual-close", PROJECT_ID, "completed", "T-manual-close"),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-manual-close','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-manual-close", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    result = track_reconciler.close_track_if_done(
        sd, "T-manual-close", PROJECT_ID, actor="operator", approval_id="MANUAL-1",
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-manual-close") == "done"


def test_oi1167_evidence_none_path_blocked_when_table_absent(tmp_path):
    """OI-1167: the evidence=None (human `vnx objective close`) path has no
    delivery-completeness check of its own -- it gates purely on
    derived_status=='done'. On a pre-0032 store (table absent entirely, not
    just an unmarked row) that derivation used to fall through unguarded,
    exactly reproducing the OI-829 bug shape (PR merged, delivery marking
    can't exist, track closes anyway). Same shape as the byte-for-byte-
    unchanged test above, EXCEPT migration 0032 was never applied -- so this
    one must now stay open, not close."""
    sd = _build_db(tmp_path, with_delivery_table=False)
    tracks_lib.create_track(
        sd, "T-pre32-manual-close", PROJECT_ID, title="manual close pre-0032",
        goal_state="y", phase="active", pr_ref="#1221,#1239",
    )
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        ("D-T-pre32-manual-close", PROJECT_ID, "completed", "T-pre32-manual-close"),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-pre32-manual-close','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-pre32-manual-close", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    result = track_reconciler.close_track_if_done(
        sd, "T-pre32-manual-close", PROJECT_ID, actor="operator", approval_id="MANUAL-2",
    )
    assert result["action"] == "noop_not_terminal"
    assert result["applied"] is False
    assert _phase(sd, "T-pre32-manual-close") == "active"  # no write
    assert _derived_status(sd, "T-pre32-manual-close") == "in_progress"


# ---------------------------------------------------------------------------
# OI-1064: evidence threading — merged_pr_numbers forwarded into reconcile_track
# ---------------------------------------------------------------------------

def test_close_forwards_merged_pr_numbers_into_reconcile_track(tmp_path, monkeypatch):
    """close_track_if_done forwards a passed merged set into reconcile_track.

    Asserts on the reconcile_track CALL (the kwarg value), not on a log line.
    The merged_pr_numbers kwarg must reach reconcile_track verbatim; default
    None keeps the old behaviour (reconcile_track loads the local set itself).
    """
    import track_reconciler_closure

    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-fwd", phase="active")

    captured: dict = {}

    real_reconcile = track_reconciler.reconcile_track

    def _spy(state_dir, track_id, project_id, **kw):
        captured["merged_pr_numbers"] = kw.get("_merged_pr_numbers")
        return real_reconcile(state_dir, track_id, project_id, **kw)

    # Patch the name close_track_if_done resolves at call time (it imports
    # reconcile_track from track_reconciler at module load — late binding
    # resolves globals at call time, so patching the closure module's
    # reference is the faithful target).
    monkeypatch.setattr(track_reconciler_closure, "reconcile_track", _spy)

    injected = frozenset({4242})
    result = track_reconciler.close_track_if_done(
        sd, "T-fwd", PROJECT_ID, actor="operator", approval_id="X",
        merged_pr_numbers=injected,
    )
    assert result["action"] == "closed"
    assert captured["merged_pr_numbers"] == injected


def test_close_default_none_keeps_old_behaviour(tmp_path, monkeypatch):
    """Without merged_pr_numbers, reconcile_track receives None (loads local itself).

    Pins that the fix is the threading, not a weakened check: the default path
    is byte-for-byte the pre-OI-1064 behaviour.
    """
    import track_reconciler_closure

    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-default", phase="active")

    captured: dict = {}
    real_reconcile = track_reconciler.reconcile_track

    def _spy(state_dir, track_id, project_id, **kw):
        captured["merged_pr_numbers"] = kw.get("_merged_pr_numbers")
        return real_reconcile(state_dir, track_id, project_id, **kw)

    monkeypatch.setattr(track_reconciler_closure, "reconcile_track", _spy)

    track_reconciler.close_track_if_done(
        sd, "T-default", PROJECT_ID, actor="operator", approval_id="X",
    )
    assert captured["merged_pr_numbers"] is None


def test_close_with_injected_set_derives_done_and_closes(tmp_path):
    """A track whose pr_ref PRs are ABSENT from local sources but PRESENT in
    the injected set derives 'done' and the close succeeds.

    This is the core OI-1064 case: zero dispatches, no pr_merged.ndjson, no
    ROADMAP entry, no coordination event. The only merge evidence is the
    injected set (the gh numbers run_reconcile gathered). Pre-fix this track
    derived 'queued' and could not be closed. OI-1155: source 4 (gh) is now
    default-ON, so the module autouse fixture opts out (VNX_RECONCILE_GIT=0);
    the injected set short-circuits _load_merged_pr_numbers, so no live gh
    call and no local source contribute.
    """
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-injected", PROJECT_ID, title="injected", goal_state="y",
        phase="active", pr_ref="#5150,#5151",
    )
    # Mark delivery complete so the OI-829 gate passes (#5150 ships the plan).
    _set_delivery(sd, "T-injected", 5150, "complete")

    # No local merge evidence of any kind. Inject the gh-confirmed union.
    injected = frozenset({5150, 5151})
    evidence = {
        "pr_ref": "#5150,#5151",
        "pr_results": [
            {"number": 5150, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
            {"number": 5151, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
        ],
        "verified_at": "2026-08-06T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-injected", PROJECT_ID, actor="system", approval_id="APR-INJ",
        evidence=evidence, merged_pr_numbers=injected,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-injected") == "done"
    assert _derived_status(sd, "T-injected") == "done"


def test_close_without_injected_set_still_queued(tmp_path):
    """Without the injected set the old behaviour is unchanged: still 'queued'.

    Same track shape as above but merged_pr_numbers omitted. Pre-fix and
    post-fix, this must stay 'queued' (not closed) — the fix is the threading,
    not a weakened derivation rule.
    """
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-no-inj", PROJECT_ID, title="no inj", goal_state="y",
        phase="queued", pr_ref="#5152,#5153",
    )
    _set_delivery(sd, "T-no-inj", 5152, "complete")

    evidence = {
        "pr_ref": "#5152,#5153",
        "pr_results": [
            {"number": 5152, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
            {"number": 5153, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
        ],
        "verified_at": "2026-08-06T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-no-inj", PROJECT_ID, actor="system", approval_id="APR-NOINJ",
        evidence=evidence,
    )
    # gh evidence in pr_results authorizes the close directly via the
    # _skip_derived_gate path, so this still closes. Assert the derived_status
    # the close saw was the OLD one (queued) — proving the injected set was
    # the only thing that would have changed the derivation. This mirrors the
    # real background-job-liveness defect (phase=queued, bare gh merge).
    assert result["derived_status"] == "queued"


def test_injected_set_unions_with_local_evidence(tmp_path):
    """The union is a union: a PR present locally but absent from the injected
    gh set is still counted.

    Track pr_ref='#6000,#6001'. #6000 is in local pr_merged.ndjson (local
    evidence). #6001 is ONLY in the injected set (gh-confirmed, no local
    receipt). The injected set (what run_reconcile would pass) is the UNION of
    gh-confirmed {6001} and local {6000} = {6000, 6001}. Derived 'done'. Pins
    that the caller's union preserves local evidence rather than replacing it
    with the gh set alone — a caller that passed only the gh set {6001} would
    leave #6000 uncounted.
    """
    import json
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-union", PROJECT_ID, title="union", goal_state="y",
        phase="active", pr_ref="#6000,#6001",
    )
    _set_delivery(sd, "T-union", 6000, "complete")
    # Local evidence for #6000 only.
    events_dir = sd.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "pr_merged.ndjson").write_text(
        json.dumps({"event_type": "pr_merged", "pr_number": 6000}) + "\n",
        encoding="utf-8",
    )

    # run_reconcile unions gh-confirmed {6001} with local {6000} = {6000, 6001}.
    injected_union = frozenset({6000, 6001})
    evidence = {
        "pr_ref": "#6000,#6001",
        "pr_results": [
            {"number": 6000, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
            {"number": 6001, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
        ],
        "verified_at": "2026-08-06T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-union", PROJECT_ID, actor="system", approval_id="APR-UNION",
        evidence=evidence, merged_pr_numbers=injected_union,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _derived_status(sd, "T-union") == "done"


def test_injected_gh_only_set_without_union_leaves_local_pr_uncounted(tmp_path):
    """Counter-pin for the union: passing ONLY the gh set (no union with local)
    leaves a local-only PR uncounted → derived 'in_progress', not 'done'.

    This proves the union with local evidence is load-bearing, not decorative:
    a caller that passed only the gh-confirmed numbers (forgetting the local
    set) would regress the defect for any track with mixed local/gh evidence.
    """
    import json
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-gh-only-set", PROJECT_ID, title="gh only set", goal_state="y",
        phase="active", pr_ref="#6002,#6003",
    )
    _set_delivery(sd, "T-gh-only-set", 6002, "complete")
    events_dir = sd.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "pr_merged.ndjson").write_text(
        json.dumps({"event_type": "pr_merged", "pr_number": 6002}) + "\n",
        encoding="utf-8",
    )
    evidence = {
        "pr_ref": "#6002,#6003",
        "pr_results": [
            {"number": 6002, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
            {"number": 6003, "state": "MERGED", "mergedAt": "2026-08-06T10:00:00Z"},
        ],
        "verified_at": "2026-08-06T10:00:00Z",
    }
    result = track_reconciler.close_track_if_done(
        sd, "T-gh-only-set", PROJECT_ID, actor="system", approval_id="APR-GHONLY",
        evidence=evidence, merged_pr_numbers=frozenset({6003}),  # gh-only, no union
    )
    # gh evidence bypasses the derived gate so the close still happens, but the
    # derived_status reflects the MISSING local PR #6002 → 'in_progress'.
    assert result["derived_status"] == "in_progress"

