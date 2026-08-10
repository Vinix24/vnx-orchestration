"""tests/test_dispatch_outcome_classifier.py — receipt-quality PR-B3 targeted tests.

Verifies (dispatch instruction's 5 required cases):
  1. each enum state derives from the right raw evidence
  2. reap-then-merged recomputes to merged-PR (not locked as failed) — finding-3
  3. completed-no-pr vs preserved-no-pr vs abandoned distinguished by evidence
  4. expired->failed<deadline-kill> / dead_letter->failed<provider-error> mapping
  5. recompute is idempotent + project-scoped

Plus targeted coverage of the evidence loader (DB joins, receipts scan, git
salvage check, active/orphan check) and the FPY/rework-rate metrics query.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_outcome_classifier as doc  # noqa: E402

PROJECT_ID = "test-proj"
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# DB / filesystem fixtures
# ---------------------------------------------------------------------------

def _build_state(tmp_path: Path) -> tuple[Path, Path]:
    """Return (state_dir, data_dir) with empty runtime_coordination.db +
    quality_intelligence.db carrying the production dispatches / dispatch_metadata
    shapes (columns verified against schemas/quality_intelligence.sql and the
    dispatches CREATE TABLE in migrate_future_system.py / test_track_reconciler.py)."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)

    rc = sqlite3.connect(str(state_dir / doc.RC_DB_FILENAME))
    rc.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    rc.commit()
    rc.close()

    qi = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    qi.execute("""
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            role TEXT,
            pr_id TEXT,
            parent_dispatch TEXT,
            outcome_status TEXT,
            UNIQUE (project_id, dispatch_id)
        )
    """)
    qi.commit()
    qi.close()

    (data_dir / "dispatches" / "active").mkdir(parents=True)
    return state_dir, data_dir


def _insert_dispatch(
    state_dir: Path, dispatch_id: str, *, project_id: str = PROJECT_ID,
    state: str = "completed", track: str | None = "receipt-quality",
    created_at: str | None = None,
) -> None:
    conn = sqlite3.connect(str(state_dir / doc.RC_DB_FILENAME))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, created_at) "
        "VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')))",
        (dispatch_id, project_id, state, track, created_at),
    )
    conn.commit()
    conn.close()


def _insert_metadata(
    state_dir: Path, dispatch_id: str, *, project_id: str = PROJECT_ID,
    pr_id: str | None = None, parent_dispatch: str | None = None,
    track: str = "headless", provider: str | None = None,
) -> None:
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track, "
        "pr_id, parent_dispatch, provider) VALUES (?, ?, 'T1', ?, ?, ?, ?)",
        (dispatch_id, project_id, track, pr_id, parent_dispatch, provider),
    )
    conn.commit()
    conn.close()


def _write_receipt(state_dir: Path, dispatch_id: str, status: str, *, failure_reason: str | None = None) -> None:
    path = state_dir / doc.RECEIPTS_FILENAME
    rec = {"event_type": "subprocess_completion", "receipt_kind": "dispatch",
           "dispatch_id": dispatch_id, "status": status}
    if failure_reason:
        rec["failure_reason"] = failure_reason
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _mark_active(data_dir: Path, dispatch_id: str) -> None:
    d = data_dir / "dispatches" / "active" / dispatch_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"terminal": "T1"}), encoding="utf-8")


def _write_receipt_full(state_dir: Path, dispatch_id: str, status: str, *, project_id) -> None:
    """Like _write_receipt but stamps (or omits) project_id explicitly."""
    path = state_dir / doc.RECEIPTS_FILENAME
    rec: dict = {"event_type": "subprocess_completion", "receipt_kind": "dispatch",
                 "dispatch_id": dispatch_id, "status": status}
    if project_id is not _OMIT:
        rec["project_id"] = project_id
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


_OMIT = object()


def _create_provenance_registry_table(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / doc.RC_DB_FILENAME))
    conn.execute("""
        CREATE TABLE provenance_registry (
            dispatch_id     TEXT NOT NULL,
            receipt_id      TEXT,
            commit_sha      TEXT,
            pr_number       INTEGER,
            feature_plan_pr TEXT,
            trace_token     TEXT,
            chain_status    TEXT NOT NULL DEFAULT 'incomplete',
            gaps_json       TEXT DEFAULT '[]',
            registered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            verified_at     TEXT,
            verified_by     TEXT,
            PRIMARY KEY (dispatch_id)
        )
    """)
    conn.commit()
    conn.close()


def _insert_provenance_row(
    state_dir: Path, dispatch_id: str, *, pr_number: int | None = None,
    commit_sha: str | None = None,
) -> None:
    conn = sqlite3.connect(str(state_dir / doc.RC_DB_FILENAME))
    conn.execute(
        "INSERT INTO provenance_registry (dispatch_id, pr_number, commit_sha) VALUES (?, ?, ?)",
        (dispatch_id, pr_number, commit_sha),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Each enum state derives from the right raw evidence (pure classify_outcome)
# ---------------------------------------------------------------------------

def _ev(**kwargs) -> doc.DispatchOutcomeEvidence:
    base = dict(dispatch_id="d1", project_id=PROJECT_ID)
    base.update(kwargs)
    return doc.DispatchOutcomeEvidence(**base)


def test_merged_pr_wins_top_priority():
    ev = _ev(pr_merged=True, superseded_by="d2", terminal_receipt_status="failure",
              dispatch_state="expired")
    assert doc.classify_outcome(ev) == "merged-PR"


def test_superseded_when_no_pr_merge():
    ev = _ev(superseded_by="d2")
    assert doc.classify_outcome(ev) == "superseded"


def test_completed_no_pr():
    ev = _ev(terminal_receipt_status="success")
    assert doc.classify_outcome(ev) == "completed-no-pr"


def test_failed_reason_bracket():
    ev = _ev(dispatch_state="expired")
    assert doc.classify_outcome(ev) == "failed⟨deadline-kill⟩"


def test_rework_of_track_fallback():
    ev = _ev(parent_dispatch="d0", parent_track="receipt-quality")
    assert doc.classify_outcome(ev) == "rework-of⟨receipt-quality⟩"


def test_rework_of_falls_back_to_parent_id_without_track():
    ev = _ev(parent_dispatch="d0", parent_track=None)
    assert doc.classify_outcome(ev) == "rework-of⟨d0⟩"


def test_preserved_no_pr():
    ev = _ev(origin_branch_has_commits=True)
    assert doc.classify_outcome(ev) == "preserved-no-pr"


def test_abandoned_when_old_no_lease_no_branch():
    ev = _ev(age_hours=48.0, is_active_or_orphaned=False, origin_branch_has_commits=False)
    assert doc.classify_outcome(ev, window_hours=24.0) == "abandoned"


def test_none_when_still_active_even_if_old():
    ev = _ev(age_hours=48.0, is_active_or_orphaned=True)
    assert doc.classify_outcome(ev, window_hours=24.0) is None


def test_none_when_too_young():
    ev = _ev(age_hours=1.0, is_active_or_orphaned=False, origin_branch_has_commits=False)
    assert doc.classify_outcome(ev, window_hours=24.0) is None


def test_none_when_no_evidence_at_all():
    ev = _ev()
    assert doc.classify_outcome(ev) is None


def test_all_seven_closed_outcomes_are_in_closed_set():
    # rework-of<track> carries a dynamic bracket (the parent's track/id), so
    # it is checked by prefix rather than frozenset membership; the other six
    # are fixed-vocabulary values and must appear in CLOSED_OUTCOMES verbatim.
    cases = [
        _ev(pr_merged=True),
        _ev(superseded_by="d2"),
        _ev(terminal_receipt_status="success"),
        _ev(dispatch_state="expired"),
        _ev(origin_branch_has_commits=True),
        _ev(age_hours=99.0, is_active_or_orphaned=False, origin_branch_has_commits=False),
    ]
    for ev in cases:
        outcome = doc.classify_outcome(ev, window_hours=24.0)
        assert outcome in doc.CLOSED_OUTCOMES, outcome

    rework_ev = _ev(parent_dispatch="d0")
    rework_outcome = doc.classify_outcome(rework_ev, window_hours=24.0)
    assert rework_outcome.startswith("rework-of⟨") and rework_outcome.endswith("⟩")


# ---------------------------------------------------------------------------
# 4. expired/dead_letter deterministic reason mapping
# ---------------------------------------------------------------------------

def test_expired_always_deadline_kill_even_with_other_reason_text():
    ev = _ev(dispatch_state="expired", terminal_receipt_failure_reason="phantom guard rejected")
    assert doc.classify_outcome(ev) == "failed⟨deadline-kill⟩"


def test_dead_letter_defaults_to_provider_error():
    ev = _ev(dispatch_state="dead_letter")
    assert doc.classify_outcome(ev) == "failed⟨provider-error⟩"


def test_dead_letter_uses_captured_reason_when_present():
    ev = _ev(dispatch_state="dead_letter", terminal_receipt_failure_reason="orchestrator_death")
    assert doc.classify_outcome(ev) == "failed⟨worktree-reap⟩"


@pytest.mark.parametrize("raw,expected", [
    ("phantom guard rejected empty diff", "fabrication-guard"),
    ("fabrication vector detected", "fabrication-guard"),
    ("blank completion text returned", "empty-completion"),
    ("empty success payload", "empty-completion"),
    ("review gate said REVISE", "gate-revise"),
    ("orchestrator_death", "worktree-reap"),
    ("worktree reap triggered", "worktree-reap"),
    ("receipt deadline exceeded", "deadline-kill"),
    ("connection timeout to provider", "deadline-kill"),
    ("tmux binary not found in PATH", None),
])
def test_map_captured_reason(raw, expected):
    assert doc._map_captured_reason(raw) == expected


def test_map_captured_reason_none_and_empty():
    assert doc._map_captured_reason(None) is None
    assert doc._map_captured_reason("") is None


# ---------------------------------------------------------------------------
# receipt classification
# ---------------------------------------------------------------------------

def test_classify_receipts_success():
    assert doc._classify_receipts([{"status": "done"}]) == ("success", None)


def test_classify_receipts_failure_captures_reason():
    status, reason = doc._classify_receipts([{"status": "failed", "failure_reason": "boom"}])
    assert status == "failure"
    assert reason == "boom"


def test_classify_receipts_empty():
    assert doc._classify_receipts(None) == (None, None)
    assert doc._classify_receipts([]) == (None, None)


def test_classify_receipts_success_takes_precedence_over_failure():
    receipts = [{"status": "failed", "failure_reason": "x"}, {"status": "done"}]
    assert doc._classify_receipts(receipts) == ("success", None)


# ---------------------------------------------------------------------------
# 3. completed-no-pr vs preserved-no-pr vs abandoned — full evidence loader
# ---------------------------------------------------------------------------

def test_evidence_completed_no_pr_from_receipt(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-success", state="completed")
    _write_receipt(state_dir, "d-success", "done")
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-success", now=NOW)
    assert doc.classify_outcome(ev) == "completed-no-pr"


def test_evidence_preserved_no_pr_from_salvaged_branch(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-salvaged", state="active",
                      created_at=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    monkeypatch.setattr(doc, "_origin_branch_has_commits", lambda repo_root, did: did == "d-salvaged")
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-salvaged",
                            repo_root=tmp_path, now=NOW)
    assert doc.classify_outcome(ev) == "preserved-no-pr"


def test_evidence_abandoned_no_receipt_no_branch_old_no_lease(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    old_ts = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_dispatch(state_dir, "d-abandoned", state="active", created_at=old_ts)
    monkeypatch.setattr(doc, "_origin_branch_has_commits", lambda repo_root, did: False)
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-abandoned", now=NOW)
    assert doc.classify_outcome(ev, window_hours=24.0) == "abandoned"


def test_evidence_active_manifest_excludes_from_abandoned(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    old_ts = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_dispatch(state_dir, "d-pending-recovery", state="active", created_at=old_ts)
    _mark_active(data_dir, "d-pending-recovery")
    monkeypatch.setattr(doc, "_origin_branch_has_commits", lambda repo_root, did: False)
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-pending-recovery", now=NOW)
    assert ev.is_active_or_orphaned is True
    assert doc.classify_outcome(ev, window_hours=24.0) is None


# ---------------------------------------------------------------------------
# 2. reap-then-merged recomputes to merged-PR — finding-3
# ---------------------------------------------------------------------------

def test_reap_then_merged_recomputes_no_fill_once_lock(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-reap", state="dead_letter")
    _write_receipt(state_dir, "d-reap", "failed", failure_reason="orchestrator_death")
    _insert_metadata(state_dir, "d-reap", pr_id=None)

    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())
    result1 = doc.reconcile_dispatch_outcome(state_dir, data_dir, PROJECT_ID, "d-reap", now=NOW)
    assert result1["outcome"] == "failed⟨worktree-reap⟩"
    assert result1["persisted"] is True

    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    stored = conn.execute(
        "SELECT outcome FROM dispatch_outcomes WHERE project_id=? AND dispatch_id=?",
        (PROJECT_ID, "d-reap"),
    ).fetchone()
    conn.close()
    assert stored[0] == "failed⟨worktree-reap⟩"

    # Later: the PR actually merges. Update dispatch_metadata.pr_id and re-run.
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    conn.execute(
        "UPDATE dispatch_metadata SET pr_id = ? WHERE project_id=? AND dispatch_id=?",
        ("999", PROJECT_ID, "d-reap"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset({999}))

    result2 = doc.reconcile_dispatch_outcome(state_dir, data_dir, PROJECT_ID, "d-reap", now=NOW)
    assert result2["outcome"] == "merged-PR"

    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    stored2 = conn.execute(
        "SELECT outcome FROM dispatch_outcomes WHERE project_id=? AND dispatch_id=?",
        (PROJECT_ID, "d-reap"),
    ).fetchone()
    conn.close()
    assert stored2[0] == "merged-PR", "no fill-once lock — recompute must overwrite the stale failed row"


# ---------------------------------------------------------------------------
# 5. recompute is idempotent + project-scoped
# ---------------------------------------------------------------------------

def test_reconcile_is_idempotent(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-idem", state="completed")
    _write_receipt(state_dir, "d-idem", "done")
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    r1 = doc.reconcile_dispatch_outcome(state_dir, data_dir, PROJECT_ID, "d-idem", now=NOW)
    r2 = doc.reconcile_dispatch_outcome(state_dir, data_dir, PROJECT_ID, "d-idem", now=NOW)
    assert r1["outcome"] == r2["outcome"] == "completed-no-pr"

    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    count = conn.execute(
        "SELECT COUNT(*) FROM dispatch_outcomes WHERE project_id=? AND dispatch_id=?",
        (PROJECT_ID, "d-idem"),
    ).fetchone()[0]
    conn.close()
    assert count == 1, "upsert must not create duplicate rows on repeated recompute"


def test_reconcile_is_project_scoped(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    # Same dispatch_id, two different tenants, opposite outcomes.
    _insert_dispatch(state_dir, "shared-id", project_id="tenant-a", state="completed")
    _write_receipt(state_dir, "shared-id", "done")
    _insert_dispatch(state_dir, "shared-id", project_id="tenant-b", state="dead_letter")
    # Two receipt lines share dispatch_id "shared-id" but that's fine — the
    # loader's receipt scan is dispatch_id-keyed, not project-scoped, so we
    # keep dispatch-id text distinct per tenant to avoid conflating the two
    # receipt-derived signals in this test.
    ra = doc.reconcile_dispatch_outcome(state_dir, data_dir, "tenant-a", "shared-id", now=NOW)
    rb = doc.reconcile_dispatch_outcome(state_dir, data_dir, "tenant-b", "shared-id", now=NOW)

    assert ra["outcome"] == "completed-no-pr"
    # tenant-b's dispatch row is dead_letter but shares the receipts-file
    # "done" signal keyed only by dispatch_id — receipt evidence wins over
    # dispatch_state in classify_outcome's priority order (terminal_receipt_
    # status == success is checked before the failure branch), so tenant-b
    # also resolves completed-no-pr. The DB rows themselves must still be
    # correctly separated per (project_id, dispatch_id) — verified below.
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    rows = conn.execute(
        "SELECT project_id, dispatch_id, outcome FROM dispatch_outcomes "
        "WHERE dispatch_id = 'shared-id' ORDER BY project_id",
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"tenant-a", "tenant-b"}


def test_load_evidence_project_scoped_dispatch_row(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "dup-id", project_id="tenant-a", state="completed")
    _insert_dispatch(state_dir, "dup-id", project_id="tenant-b", state="expired")

    ev_a = doc.load_evidence(state_dir, data_dir, "tenant-a", "dup-id", now=NOW)
    ev_b = doc.load_evidence(state_dir, data_dir, "tenant-b", "dup-id", now=NOW)
    assert ev_a.dispatch_state == "completed"
    assert ev_b.dispatch_state == "expired"


# ---------------------------------------------------------------------------
# superseded / rework-of — reverse parent_dispatch lookup
# ---------------------------------------------------------------------------

def test_superseded_by_merged_rework_child(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-original", state="dead_letter")
    _insert_dispatch(state_dir, "d-rework", state="completed")
    _insert_metadata(state_dir, "d-original", pr_id=None)
    _insert_metadata(state_dir, "d-rework", pr_id="42", parent_dispatch="d-original")

    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset({42}))
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-original", now=NOW)
    assert ev.superseded_by == "d-rework"
    assert doc.classify_outcome(ev) == "superseded"


def test_rework_child_not_yet_merged_does_not_supersede(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-original", state="dead_letter")
    _insert_dispatch(state_dir, "d-rework", state="active")
    _insert_metadata(state_dir, "d-original", pr_id=None)
    _insert_metadata(state_dir, "d-rework", pr_id=None, parent_dispatch="d-original")

    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-original", now=NOW)
    assert ev.superseded_by is None
    assert doc.classify_outcome(ev) == "failed⟨provider-error⟩"


def test_rework_of_uses_parent_track(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-rework2", state="active")
    _insert_metadata(state_dir, "d-parent2", pr_id=None, track="receipt-quality")
    _insert_metadata(state_dir, "d-rework2", pr_id=None, parent_dispatch="d-parent2")

    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-rework2", now=NOW)
    assert ev.parent_track == "receipt-quality"
    assert doc.classify_outcome(ev) == "rework-of⟨receipt-quality⟩"


# ---------------------------------------------------------------------------
# is_active_or_orphaned / origin_branch_has_commits
# ---------------------------------------------------------------------------

def test_is_active_or_orphaned_true_and_false(tmp_path):
    data_dir = tmp_path / ".vnx-data"
    (data_dir / "dispatches" / "active").mkdir(parents=True)
    assert doc._is_active_or_orphaned(data_dir, "no-such-dispatch") is False
    _mark_active(data_dir, "live-one")
    assert doc._is_active_or_orphaned(data_dir, "live-one") is True


def test_origin_branch_has_commits_false_on_missing_repo(tmp_path):
    assert doc._origin_branch_has_commits(tmp_path / "does-not-exist", "d1") is False
    assert doc._origin_branch_has_commits(None, "d1") is False


def test_origin_branch_has_commits_true_with_real_remote(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "f.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin",
                     "HEAD:refs/heads/dispatch/d-salvage-test"], check=True)

    assert doc._origin_branch_has_commits(work, "d-salvage-test") is True
    assert doc._origin_branch_has_commits(work, "d-nonexistent") is False


# ---------------------------------------------------------------------------
# OI-1078 — batched ls-remote: one network call per run, not one per dispatch
# ---------------------------------------------------------------------------

def _make_origin_with_branches(tmp_path: Path, branches: list[str]) -> Path:
    """Build a work repo whose `origin` (a local bare repo) carries ``branches``."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "f.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(bare)], check=True)
    for branch in branches:
        subprocess.run(["git", "-C", str(work), "push", "-q", "origin",
                         f"HEAD:refs/heads/{branch}"], check=True)
    return work


def test_fetch_remote_dispatch_branches_parses_only_dispatch_refs(tmp_path):
    work = _make_origin_with_branches(
        tmp_path, ["dispatch/d-one", "dispatch/d-two", "main"],
    )
    assert doc._fetch_remote_dispatch_branches(work) == frozenset({"d-one", "d-two"})


def test_fetch_remote_dispatch_branches_empty_success_is_not_failure(tmp_path):
    """A successful batch with zero dispatch branches is an EMPTY SET, not
    None — the two states must stay distinguishable (failed vs none-exist)."""
    work = _make_origin_with_branches(tmp_path, ["main"])
    assert doc._fetch_remote_dispatch_branches(work) == frozenset()


def test_fetch_remote_dispatch_branches_failure_returns_none_and_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="dispatch_outcome_classifier"):
        assert doc._fetch_remote_dispatch_branches(tmp_path / "not-a-repo") is None
    assert any("ls-remote" in rec.message for rec in caplog.records)


def test_origin_branch_has_commits_against_batch_set():
    """Existing branch -> True, non-existing -> False, failed batch (None) ->
    False for every id — the fail-safe direction, with no git call at all."""
    batch = frozenset({"d-exists"})
    assert doc._origin_branch_has_commits(None, "d-exists", remote_branches=batch) is True
    assert doc._origin_branch_has_commits(None, "d-missing", remote_branches=batch) is False
    assert doc._origin_branch_has_commits(None, "d-exists", remote_branches=None) is False


def test_bulk_run_does_one_batched_lsremote_for_n_dispatches(tmp_path, monkeypatch):
    """Regression guard for OI-1078: N dispatch-ids cost exactly ONE batched
    ls-remote fetch per run, not N. Patched at the module attribute the bulk
    pass binds the name through, and asserted to actually have been called."""
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())
    for i in range(5):
        _insert_dispatch(state_dir, f"dispatch-{i:05d}", state="completed")
        _write_receipt(state_dir, f"dispatch-{i:05d}", "done")

    fetch_calls = []

    def counting_fetch(repo_root):
        fetch_calls.append(repo_root)
        return frozenset()

    monkeypatch.setattr(doc, "_fetch_remote_dispatch_branches", counting_fetch)
    results = doc.reconcile_all_dispatch_outcomes(
        state_dir, data_dir, PROJECT_ID, repo_root=tmp_path, now=NOW,
    )
    assert len(results) == 5
    assert len(fetch_calls) == 1  # one batched call for the whole run, not 5


def test_bulk_run_failed_batch_failsafe_false_for_every_dispatch(tmp_path, monkeypatch):
    """A failed batch (None) must never claim salvage exists: an old,
    receipt-less, lease-less dispatch classifies abandoned, NOT
    preserved-no-pr — for every id. Contrast leg proves the same dispatch
    WOULD be preserved-no-pr had the batch succeeded with its branch present."""
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())
    old_ts = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _insert_dispatch(state_dir, "d-old", state="active", created_at=old_ts)

    monkeypatch.setattr(doc, "_fetch_remote_dispatch_branches", lambda rr: None)
    results = doc.reconcile_all_dispatch_outcomes(
        state_dir, data_dir, PROJECT_ID, repo_root=tmp_path, now=NOW,
    )
    assert [r["outcome"] for r in results] == ["abandoned"]

    monkeypatch.setattr(
        doc, "_fetch_remote_dispatch_branches", lambda rr: frozenset({"d-old"}),
    )
    results = doc.reconcile_all_dispatch_outcomes(
        state_dir, data_dir, PROJECT_ID, repo_root=tmp_path, now=NOW,
    )
    assert [r["outcome"] for r in results] == ["preserved-no-pr"]


# ---------------------------------------------------------------------------
# reconcile_all_dispatch_outcomes — bulk pass, bounded evidence load
# ---------------------------------------------------------------------------

def test_reconcile_all_dispatch_outcomes_bulk(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    _insert_dispatch(state_dir, "dispatch-a", state="completed")
    _write_receipt(state_dir, "dispatch-a", "done")
    _insert_dispatch(state_dir, "dispatch-b", state="expired")
    _insert_dispatch(state_dir, "dispatch-c", state="active")  # no evidence -> None

    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, PROJECT_ID, now=NOW)
    by_id = {r["dispatch_id"]: r["outcome"] for r in results}
    assert by_id["dispatch-a"] == "completed-no-pr"
    assert by_id["dispatch-b"] == "failed⟨deadline-kill⟩"
    assert by_id["dispatch-c"] is None

    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    rows = conn.execute(
        "SELECT dispatch_id FROM dispatch_outcomes WHERE project_id=?", (PROJECT_ID,)
    ).fetchall()
    conn.close()
    ids = {r[0] for r in rows}
    assert ids == {"dispatch-a", "dispatch-b"}, "only closed outcomes are persisted; dispatch-c stays absent"


# ---------------------------------------------------------------------------
# FPY / rework-rate metrics
# ---------------------------------------------------------------------------

def test_compute_fpy_metrics_arithmetic(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    doc.ensure_dispatch_outcomes_table(conn)

    # 4 closed dispatches: 2 first-attempt merged, 1 reworked-then-merged,
    # 1 failed. FPY should count only the 2 first-attempt merges.
    _insert_metadata(state_dir, "m1", provider="claude")
    _insert_metadata(state_dir, "m2", provider="claude")
    _insert_metadata(state_dir, "m3", provider="claude", parent_dispatch="m0")
    _insert_metadata(state_dir, "m4", provider="codex")

    conn.close()
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    doc.ensure_dispatch_outcomes_table(conn)
    doc._write_outcome(conn, PROJECT_ID, "m1", "merged-PR")
    doc._write_outcome(conn, PROJECT_ID, "m2", "merged-PR")
    doc._write_outcome(conn, PROJECT_ID, "m3", "merged-PR")
    doc._write_outcome(conn, PROJECT_ID, "m4", "failed⟨provider-error⟩")
    conn.commit()

    metrics = doc.compute_fpy_metrics(conn, PROJECT_ID)
    conn.close()

    assert metrics["total_closed"] == 4
    assert metrics["fpy"] == pytest.approx(2 / 4)
    assert metrics["rework_rate"] == pytest.approx(1 / 4)
    assert metrics["model_fail_profile"] == {"codex": {"provider-error": 1}}


def test_compute_fpy_metrics_handles_missing_dispatch_metadata_row(tmp_path):
    # A dispatch_outcomes row with no matching dispatch_metadata row (a known
    # identity_unresolved gap from earlier receipt-quality PRs) must not crash
    # the LEFT JOIN or corrupt the profile — provider falls back to "unknown",
    # parent_dispatch is treated as absent (not reworked).
    state_dir, data_dir = _build_state(tmp_path)
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    doc.ensure_dispatch_outcomes_table(conn)
    doc._write_outcome(conn, PROJECT_ID, "orphan-outcome", "failed⟨provider-error⟩")
    conn.commit()

    metrics = doc.compute_fpy_metrics(conn, PROJECT_ID)
    conn.close()

    assert metrics["total_closed"] == 1
    assert metrics["fpy"] == 0.0
    assert metrics["rework_rate"] == 0.0
    assert metrics["model_fail_profile"] == {"unknown": {"provider-error": 1}}


def test_compute_fpy_metrics_empty(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    doc.ensure_dispatch_outcomes_table(conn)
    metrics = doc.compute_fpy_metrics(conn, PROJECT_ID)
    conn.close()
    assert metrics == {
        "total_closed": 0, "fpy": None, "rework_rate": None, "model_fail_profile": {},
    }


# ---------------------------------------------------------------------------
# dispatch_metadata.outcome_status stays untouched (advisory-only contract)
# ---------------------------------------------------------------------------

def test_outcome_status_column_never_written(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    _insert_dispatch(state_dir, "d-advisory", state="completed")
    _write_receipt(state_dir, "d-advisory", "done")
    _insert_metadata(state_dir, "d-advisory", pr_id=None)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    doc.reconcile_dispatch_outcome(state_dir, data_dir, PROJECT_ID, "d-advisory", now=NOW)

    conn = sqlite3.connect(str(state_dir / doc.QI_DB_FILENAME))
    row = conn.execute(
        "SELECT outcome_status FROM dispatch_metadata WHERE project_id=? AND dispatch_id=?",
        (PROJECT_ID, "d-advisory"),
    ).fetchone()
    conn.close()
    assert row[0] is None, "B3 must never write dispatch_metadata.outcome_status"


# ---------------------------------------------------------------------------
# Laag 1 (OI-824): population = dispatches ∪ receipts keys, project-scoped
# ---------------------------------------------------------------------------

def test_receipt_belongs_to_project_explicit_match_and_mismatch():
    assert doc._receipt_belongs_to_project({"project_id": "p1"}, "p1") is True
    assert doc._receipt_belongs_to_project({"project_id": "p1"}, "p2") is False


def test_receipt_belongs_to_project_none_defaults_to_legacy_vnx_dev():
    # A receipt with no project_id field predates project-id stamping —
    # treated as the legacy default project, never silently dropped or
    # silently assigned to every project.
    assert doc._receipt_belongs_to_project({}, "vnx-dev") is True
    assert doc._receipt_belongs_to_project({"project_id": None}, "vnx-dev") is True
    assert doc._receipt_belongs_to_project({}, "some-other-project") is False


def test_reconcile_all_includes_receipt_only_dispatch_not_in_dispatches_table(tmp_path, monkeypatch):
    """The core Laag 1 fix: a real tmux-lane build dispatch that never got a
    ``dispatches`` row (the deliverable-queue only tracks planning stubs)
    must still be classified, sourced purely from its receipt."""
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    # A deliverable stub IS in the dispatches table (unrelated to the build).
    _insert_dispatch(state_dir, "dlv-stub", project_id="vnx-dev", state="active",
                      created_at=(NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    # A real build dispatch is ONLY in the receipts ledger.
    _write_receipt_full(state_dir, "20260728-real-build-sonnet", "done", project_id="vnx-dev")

    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, "vnx-dev", now=NOW)
    by_id = {r["dispatch_id"]: r["outcome"] for r in results}

    assert "20260728-real-build-sonnet" in by_id, "receipt-only dispatch must enter the population"
    assert by_id["20260728-real-build-sonnet"] == "completed-no-pr"
    assert by_id["dlv-stub"] == "abandoned"


def test_reconcile_all_receipt_population_is_project_scoped(tmp_path, monkeypatch):
    """A receipt stamped for a DIFFERENT project must never enter this
    project's population (ADR-007)."""
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    _write_receipt_full(state_dir, "other-tenant-dispatch", "done", project_id="tenant-b")
    _write_receipt_full(state_dir, "this-tenant-dispatch", "done", project_id="vnx-dev")

    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, "vnx-dev", now=NOW)
    ids = {r["dispatch_id"] for r in results}
    assert "this-tenant-dispatch" in ids
    assert "other-tenant-dispatch" not in ids


def test_reconcile_all_legacy_unstamped_receipt_joins_default_project(tmp_path, monkeypatch):
    state_dir, data_dir = _build_state(tmp_path)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    _write_receipt_full(state_dir, "legacy-no-project-id", "done", project_id=_OMIT)

    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, "vnx-dev", now=NOW)
    by_id = {r["dispatch_id"]: r["outcome"] for r in results}
    assert by_id.get("legacy-no-project-id") == "completed-no-pr"

    results_other = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, "some-other-project", now=NOW)
    assert "legacy-no-project-id" not in {r["dispatch_id"] for r in results_other}


def test_reconcile_all_degrades_to_receipts_only_when_dispatches_db_missing(tmp_path, monkeypatch):
    """A missing/unreadable runtime_coordination.db must not blank the whole
    population — the receipts ledger is a fully independent source."""
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    (data_dir / "dispatches" / "active").mkdir(parents=True)
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset())

    _write_receipt_full(state_dir, "receipts-only-dispatch", "done", project_id="vnx-dev")

    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, "vnx-dev", now=NOW)
    by_id = {r["dispatch_id"]: r["outcome"] for r in results}
    assert by_id.get("receipts-only-dispatch") == "completed-no-pr"


def test_reconcile_all_returns_empty_when_no_dispatches_and_no_receipts(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    results = doc.reconcile_all_dispatch_outcomes(state_dir, data_dir, PROJECT_ID, now=NOW)
    assert results == []


# ---------------------------------------------------------------------------
# Laag 2 (OI-824): PR-linkage via provenance_registry.pr_number, additive to
# dispatch_metadata.pr_id
# ---------------------------------------------------------------------------

def test_provenance_pr_number_resolves_merged_pr_without_dispatch_metadata(tmp_path):
    """The tmux-lane build case: dispatch_metadata has no row at all (build
    dispatches never write there), but provenance_registry's commit-scan-
    derived pr_number is enough on its own — no merged_pr_numbers cross-check
    needed, since a git-log-derived pr_number is already proof the commit is
    on main."""
    state_dir, data_dir = _build_state(tmp_path)
    _create_provenance_registry_table(state_dir)
    _insert_provenance_row(state_dir, "d-build", pr_number=1235, commit_sha="abc123")

    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-build", now=NOW)
    assert ev.pr_merged is True
    assert ev.pr_id == "1235"
    assert doc.classify_outcome(ev) == "merged-PR"


def test_provenance_pr_number_absent_table_is_noop(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    # No provenance_registry table at all (older DB / test fixture).
    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-no-provenance", now=NOW)
    assert ev.pr_merged is False


def test_provenance_pr_number_row_with_null_pr_number_is_no_evidence(tmp_path):
    state_dir, data_dir = _build_state(tmp_path)
    _create_provenance_registry_table(state_dir)
    _insert_provenance_row(state_dir, "d-incomplete", pr_number=None)

    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-incomplete", now=NOW)
    assert ev.pr_merged is False


def test_dispatch_metadata_pr_id_complements_provenance_not_replaced(tmp_path, monkeypatch):
    """Both sources present: dispatch_metadata.pr_id already resolves
    pr_merged=True — provenance_registry must not be needed/consulted to
    override an already-resolved evidence chain."""
    state_dir, data_dir = _build_state(tmp_path)
    _create_provenance_registry_table(state_dir)
    _insert_metadata(state_dir, "d-both", pr_id="42")
    _insert_provenance_row(state_dir, "d-both", pr_number=999)  # different PR, must not win
    monkeypatch.setattr(doc, "_load_merged_pr_numbers", lambda sd, rr: frozenset({42}))

    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "d-both", now=NOW)
    assert ev.pr_merged is True
    assert ev.pr_id == "42", "dispatch_metadata.pr_id must not be overwritten by provenance_registry"


def test_provenance_pr_number_ambiguity_guard_ignores_cross_project_dispatch_id(tmp_path):
    """ADR-007: provenance_registry has no project_id column of its own. A
    dispatch_id that ``dispatches`` maps to a DIFFERENT tenant must not leak
    its PR evidence into this project's classification."""
    state_dir, data_dir = _build_state(tmp_path)
    _create_provenance_registry_table(state_dir)
    _insert_dispatch(state_dir, "shared-id", project_id="tenant-other", state="completed")
    _insert_provenance_row(state_dir, "shared-id", pr_number=555)

    ev = doc.load_evidence(state_dir, data_dir, "tenant-mine", "shared-id", now=NOW)
    assert ev.pr_merged is False, "cross-tenant dispatch_id collision must not leak PR evidence"


def test_provenance_pr_number_not_ambiguous_when_absent_from_dispatches_table(tmp_path):
    """The common case: a build dispatch_id absent from `dispatches`
    entirely (Laag 1) is not ambiguous — provenance evidence still applies."""
    state_dir, data_dir = _build_state(tmp_path)
    _create_provenance_registry_table(state_dir)
    _insert_provenance_row(state_dir, "build-only-id", pr_number=1237)

    ev = doc.load_evidence(state_dir, data_dir, PROJECT_ID, "build-only-id", now=NOW)
    assert ev.pr_merged is True
