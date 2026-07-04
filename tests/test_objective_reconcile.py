"""tests/test_objective_reconcile.py — vnx objective reconcile (D3).

Verifies objective_reconcile.run_reconcile:

- check mode: nominates by pr_ref+phase; lists CONFIRMED; declared phase untouched;
  summary + history written; exit 0.
- apply mode: CONFIRMED candidate closes via real close_track_if_done; track_phase_history
  rows carry actor=system + auto-reconcile approval_id.
- multi-PR partial-merge (OPEN sibling) → not confirmed (open_pr).
- CLOSED sibling → closed_sibling skip; same + allow_closed_siblings + ≥1 merged → closes.
- OPEN PR → open_pr skip; exit 0.
- gh absent → all unverified, exit 3, nothing closed.
- --max-gh-calls 1 with 2 candidates → second deferred, exit 0.
- MERGED cache: second run does not re-invoke gh for a previously-MERGED PR.
- parked/done tracks never nominated.
"""
from __future__ import annotations

import json
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

PROJECT_ID = "test-recon-proj"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """State dir with migrations 0022 + 0024 + 0027 + 0028 + 0030 applied."""
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

    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    for ver, fname in (
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
        (30, "0030_track_oi_resolved_at.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    conn.close()
    return state_dir


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
    state_dir: Path,
    dispatch_id: str,
    track_id: str,
    *,
    state: str = "completed",
) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_ndjson(state_dir: Path, pr_number: int) -> None:
    """Write a pr_merged event to the events NDJSON so _load_merged_pr_numbers finds it."""
    events_dir = state_dir.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    ndjson = events_dir / "pr_merged.ndjson"
    with open(ndjson, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "pr_merged", "pr_number": pr_number}) + "\n")


def _seed_pr_merged_event(state_dir: Path, dispatch_id: str) -> None:
    """Insert a pr_merged coordination event so _compute_derived_status derives 'done'."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        (f"ev-{dispatch_id}", "pr_merged", "dispatch", dispatch_id, PROJECT_ID),
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
# gh subprocess mock helpers
# ---------------------------------------------------------------------------

_MERGED_AT = "2026-07-01T12:00:00Z"


def _make_gh_mock(
    pr_responses: Dict[int, Any],
    *,
    auth_ok: bool = True,
    call_log: Optional[list] = None,
):
    """Return a fake subprocess.run. pr_responses: {pr_num: dict|None('error')}."""

    def fake_run(cmd, **kwargs):
        if call_log is not None:
            call_log.append(list(cmd))
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "bad cmd")
        if cmd[0] == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            rc = 0 if auth_ok else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd[0] == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
            pr_num = int(cmd[3])
            resp = pr_responses.get(pr_num)
            if resp is None:
                return subprocess.CompletedProcess(cmd, 1, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(resp), "")
        # git commands and anything else → success with empty output
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def _absent_gh(*args, **kwargs):
    raise FileNotFoundError("gh: command not found")


def _merged_pr(number: int) -> Dict[str, str]:
    return {"state": "MERGED", "mergedAt": _MERGED_AT}


def _open_pr() -> Dict[str, str]:
    return {"state": "OPEN", "mergedAt": ""}


def _closed_pr() -> Dict[str, str]:
    return {"state": "CLOSED", "mergedAt": ""}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_check_mode_nominates_confirmed_no_phase_write(tmp_path, monkeypatch):
    """Check mode: CONFIRMED candidate found; declared phase untouched; summary+history written; exit 0."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-check", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-check", "T-check", state="completed")
    _seed_pr_merged_ndjson(sd, 100)

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({100: _merged_pr(100)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["mode"] == "check"
    assert summary["counts"]["nominated"] == 1
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 0  # check mode never closes
    per = summary["per_track"]
    assert len(per) == 1
    assert per[0]["verdict"] == "CONFIRMED"
    assert per[0]["track_id"] == "T-check"

    # declared phase must be UNTOUCHED
    assert _phase(sd, "T-check") == "active"

    # summary file written
    summary_path = sd / "reconcile_summary.json"
    assert summary_path.exists()
    loaded = json.loads(summary_path.read_text())
    assert loaded["run_id"] == summary["run_id"]

    # history NDJSON appended
    history_path = sd / "reconcile_history.ndjson"
    assert history_path.exists()
    lines = [l for l in history_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["run_id"] == summary["run_id"]


def test_apply_mode_confirmed_closes_and_records_actor(tmp_path, monkeypatch):
    """Apply mode: CONFIRMED candidate closes; track_phase_history has actor=system and auto-reconcile approval_id.

    Local merge evidence seeding removed — gh evidence alone now authorizes close (Fix 2).
    """
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-apply", phase="active", pr_ref="#200")
    # No local merge evidence: no dispatch, no pr_merged.ndjson, no coordination events.
    # gh pr view is the sole authority.

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({200: _merged_pr(200)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1

    # Phase walked to done
    assert _phase(sd, "T-apply") == "done"

    # track_phase_history has the right actor and approval_id
    hist = _history(sd, "T-apply")
    assert hist, "expected track_phase_history rows"
    last = hist[-1]
    assert last["to_phase"] == "done"
    assert last["actor"] == "system"
    assert last["approval_id"] is not None
    assert last["approval_id"].startswith("auto-reconcile-")


def test_multi_pr_partial_merge_open_sibling_not_confirmed(tmp_path, monkeypatch):
    """Multi-PR: one MERGED, one OPEN → open_pr skip, not confirmed."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-multi", phase="active", pr_ref="#300,#301")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({300: _merged_pr(300), 301: _open_pr()}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["confirmed"] == 0
    assert summary["counts"]["open_pr"] == 1
    per = summary["per_track"]
    assert per[0]["verdict"] == "open_pr"
    assert _phase(sd, "T-multi") == "active"


def test_closed_sibling_without_flag_skipped(tmp_path, monkeypatch):
    """CLOSED sibling without --allow-closed-siblings → closed_sibling skip."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-sib", phase="active", pr_ref="#400,#401")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({400: _merged_pr(400), 401: _closed_pr()}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["closed_sibling"] == 1
    assert summary["counts"]["confirmed"] == 0
    per = summary["per_track"]
    assert per[0]["verdict"] == "closed_sibling"


def test_closed_sibling_with_flag_and_merged_confirms(tmp_path, monkeypatch):
    """CLOSED sibling + --allow-closed-siblings + ≥1 MERGED → CONFIRMED and closes in apply mode.

    Local merge evidence seeding removed — gh evidence alone now authorizes close (Fix 2).
    """
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-sib2", phase="active", pr_ref="#500,#501")
    # No local merge evidence: gh evidence (MERGED+CLOSED sibling) is the authority.

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({500: _merged_pr(500), 501: _closed_pr()}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
        allow_closed_siblings=True,
    )

    assert code == 0
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-sib2") == "done"


def test_open_pr_skip(tmp_path, monkeypatch):
    """Single OPEN PR → open_pr skip; exit 0."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-open", phase="active", pr_ref="#600")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({600: _open_pr()}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    assert summary["counts"]["open_pr"] == 1
    assert summary["counts"]["confirmed"] == 0
    per = summary["per_track"]
    assert per[0]["verdict"] == "open_pr"


def test_gh_absent_all_unverified_exit3_nothing_closed(tmp_path, monkeypatch):
    """gh absent → all candidates unverified, exit 3, no closes."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-nogh", phase="active", pr_ref="#700")
    _seed_pr_merged_ndjson(sd, 700)

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _absent_gh,
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 3
    assert summary["evidence_source_health"]["gh"] == "absent"
    assert summary["counts"]["unverified"] == 1
    assert summary["counts"]["closed"] == 0
    assert _phase(sd, "T-nogh") == "active"  # untouched


def test_max_gh_calls_defers_second_candidate(tmp_path, monkeypatch):
    """--max-gh-calls 1 with 2 candidates → first proceeds, second is deferred; exit 0."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-c1", phase="active", pr_ref="#800")
    _seed_track(sd, "T-c2", phase="active", pr_ref="#801")
    _seed_pr_merged_ndjson(sd, 800)

    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({800: _merged_pr(800), 801: _merged_pr(801)}, call_log=call_log),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
        max_gh_calls=1,
    )

    assert code == 0
    # Only one candidate proceeds (1 live gh call used); the other is deferred
    assert summary["counts"]["deferred"] == 1
    assert summary["counts"]["confirmed"] + summary["counts"]["deferred"] == 2

    # Count pr-view calls (excluding auth call)
    pr_view_calls = [c for c in call_log if len(c) >= 3 and c[:3] == ["gh", "pr", "view"]]
    assert len(pr_view_calls) == 1


def test_merged_cache_second_run_no_gh_pr_view(tmp_path, monkeypatch):
    """Second run for a previously-MERGED PR must not re-invoke gh pr view."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-cache", phase="active", pr_ref="#900")
    _seed_pr_merged_ndjson(sd, 900)

    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({900: _merged_pr(900)}, call_log=call_log),
    )

    # First run — fetches PR 900 live
    summary1, code1 = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    assert code1 == 0
    assert summary1["counts"]["confirmed"] == 1
    pr_view_calls_1 = [c for c in call_log if len(c) >= 3 and c[:3] == ["gh", "pr", "view"]]
    assert len(pr_view_calls_1) == 1

    # Reset log for second run
    call_log.clear()

    # Second run — PR 900 is in cache as MERGED
    summary2, code2 = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    assert code2 == 0
    assert summary2["counts"]["confirmed"] == 1

    pr_view_calls_2 = [c for c in call_log if len(c) >= 3 and c[:3] == ["gh", "pr", "view"]]
    assert len(pr_view_calls_2) == 0, "second run must NOT re-fetch a cached MERGED PR"


def test_parked_and_done_tracks_never_nominated(tmp_path, monkeypatch):
    """Parked and done tracks are never nominated regardless of pr_ref."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-parked", phase="parked", pr_ref="#991")
    _seed_track(sd, "T-done", phase="done", pr_ref="#992")
    _seed_track(sd, "T-active", phase="active", pr_ref="#993")
    _seed_pr_merged_ndjson(sd, 993)

    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({993: _merged_pr(993)}, call_log=call_log),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )

    assert code == 0
    # Only T-active is nominated
    assert summary["counts"]["nominated"] == 1
    track_ids = [pt["track_id"] for pt in summary["per_track"]]
    assert "T-active" in track_ids
    assert "T-parked" not in track_ids
    assert "T-done" not in track_ids


def test_apply_closes_on_gh_evidence_only(tmp_path, monkeypatch):
    """CONFIRMED candidate with NO local merge evidence anywhere must close under --apply.

    No pr_merged.ndjson, no coordination events, no dispatch, no ROADMAP.yaml.
    gh pr view is the sole authority; derived_status stays non-done without local evidence.
    With Fix 2, gh evidence in pr_results bypasses the derived_status gate.
    """
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-gh-only", phase="active", pr_ref="#1001")
    # Intentionally no local merge evidence of any kind.

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1001: _merged_pr(1001)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-gh-only") == "done"


def test_cache_is_repo_scoped(tmp_path, monkeypatch):
    """Two repo roots (different fake origin remotes), same PR number:
    second repo must trigger its own gh pr view call, not reuse the first repo's cache entry.
    """
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-scoped", phase="active", pr_ref="#1002")

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    call_log: list = []

    def mock_run(cmd, **kwargs):
        call_log.append(list(cmd))
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[0] == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"state": "MERGED", "mergedAt": _MERGED_AT}), ""
            )
        if cmd[0] == "git" and "remote" in cmd:
            cwd = str(kwargs.get("cwd", ""))
            if "repo-a" in cwd:
                return subprocess.CompletedProcess(
                    cmd, 0, "https://github.com/fake/repo-a\n", ""
                )
            if "repo-b" in cwd:
                return subprocess.CompletedProcess(
                    cmd, 0, "https://github.com/fake/repo-b\n", ""
                )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(objective_reconcile.subprocess, "run", mock_run)

    # Run 1: repo-a fetches PR #1002 from gh and caches it under the repo-a key.
    summary1, _ = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=repo_a, apply=False,
    )
    assert summary1["counts"]["confirmed"] == 1
    pr_view_calls_1 = [c for c in call_log if len(c) >= 3 and c[:3] == ["gh", "pr", "view"]]
    assert len(pr_view_calls_1) == 1, "repo-a run must fetch PR 1002 from gh"

    call_log.clear()

    # Run 2: repo-b must NOT reuse repo-a's cache entry — different repo key.
    summary2, _ = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=repo_b, apply=False,
    )
    assert summary2["counts"]["confirmed"] == 1
    pr_view_calls_2 = [c for c in call_log if len(c) >= 3 and c[:3] == ["gh", "pr", "view"]]
    assert len(pr_view_calls_2) == 1, "repo-b run must trigger its own gh pr view (different repo key)"
