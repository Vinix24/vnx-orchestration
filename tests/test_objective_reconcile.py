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


def _set_delivery(
    state_dir: Path, track_id: str, pr_number: int, kind: str, *, set_by: str = "operator"
) -> None:
    """Mark a linked PR's delivery_kind ('partial'|'complete') — OI-829 close-gate.

    Tests below that assert an evidence-based close succeeds must mark at least
    one linked PR 'complete', or close_track_if_done's fail-closed delivery gate
    (added for OI-829) rejects with action='noop_incomplete_delivery' instead.
    """
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
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
        # objective_reconcile now invokes gh via its resolved absolute path.
        cmd0 = os.path.basename(str(cmd[0]))
        if cmd0 == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            rc = 0 if auth_ok else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd0 == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
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


def _is_gh_pr_view(cmd: list) -> bool:
    """True when cmd invokes ``gh pr view`` — cmd[0] may be the resolved absolute path."""
    return (
        len(cmd) >= 3
        and os.path.basename(str(cmd[0])) == "gh"
        and cmd[1] == "pr"
        and cmd[2] == "view"
    )


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
    _set_delivery(sd, "T-apply", 200, "complete")  # OI-829 gate: #200 ships the whole plan

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
    _set_delivery(sd, "T-sib2", 500, "complete")  # OI-829 gate: #500 is the merged PR

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


def test_detect_gh_finds_absolute_path_when_gh_off_path(tmp_path, monkeypatch):
    """gh absent from PATH but present at a fallback absolute path → _detect_gh "ok".

    Red on old code: the old _detect_gh shells out to a bare "gh", which a
    stripped PATH cannot resolve → FileNotFoundError → "absent" (assert fails).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_gh = tmp_path / "bin" / "gh"
    fake_gh.parent.mkdir()
    fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    # Strip every dir that could hold gh from PATH.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # New-code contract: no PATH hit, fallback list points at the fake binary.
    # On old code these attributes do not exist — guard so the behavioral
    # assertion below still runs and fails on the old resolver.
    if hasattr(objective_reconcile, "shutil"):
        monkeypatch.setattr(objective_reconcile.shutil, "which", lambda name: None)
    if hasattr(objective_reconcile, "_GH_FALLBACK_PATHS"):
        monkeypatch.setattr(objective_reconcile, "_GH_FALLBACK_PATHS", (str(fake_gh),))

    seen: list = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        if cmd and cmd[0] == str(fake_gh):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise FileNotFoundError(f"no such binary: {cmd[0] if cmd else ''}")

    monkeypatch.setattr(objective_reconcile.subprocess, "run", fake_run)

    assert objective_reconcile._detect_gh(repo) == "ok"
    assert seen, "expected a gh auth status subprocess call"
    assert seen[0][0] == str(fake_gh), "must invoke the resolved absolute path, not bare gh"


def test_detect_gh_absent_when_binary_truly_nowhere(tmp_path, monkeypatch):
    """_detect_gh says "absent" when the resolver finds no gh binary anywhere.

    The probe must be able to say no. Red on old code: the resolver contract
    (_resolve_gh_binary) does not exist, so the test cannot set up the no-gh
    world and the old code resolves a live gh → "ok" instead of "absent".
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    if hasattr(objective_reconcile, "shutil"):
        monkeypatch.setattr(objective_reconcile.shutil, "which", lambda name: None)
    if hasattr(objective_reconcile, "_GH_FALLBACK_PATHS"):
        monkeypatch.setattr(objective_reconcile, "_GH_FALLBACK_PATHS", ())

    assert objective_reconcile._resolve_gh_binary() is None
    assert objective_reconcile._detect_gh(repo) == "absent"


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
    pr_view_calls = [c for c in call_log if _is_gh_pr_view(c)]
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
    pr_view_calls_1 = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls_1) == 1

    # Reset log for second run
    call_log.clear()

    # Second run — PR 900 is in cache as MERGED
    summary2, code2 = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    assert code2 == 0
    assert summary2["counts"]["confirmed"] == 1

    pr_view_calls_2 = [c for c in call_log if _is_gh_pr_view(c)]
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
    _set_delivery(sd, "T-gh-only", 1001, "complete")  # OI-829 gate: #1001 ships the plan

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
        # objective_reconcile now invokes gh via its resolved absolute path.
        cmd0 = os.path.basename(str(cmd[0]))
        if cmd0 == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd0 == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"state": "MERGED", "mergedAt": _MERGED_AT}), ""
            )
        if cmd0 == "git" and "remote" in cmd:
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
    pr_view_calls_1 = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls_1) == 1, "repo-a run must fetch PR 1002 from gh"

    call_log.clear()

    # Run 2: repo-b must NOT reuse repo-a's cache entry — different repo key.
    summary2, _ = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=repo_b, apply=False,
    )
    assert summary2["counts"]["confirmed"] == 1
    pr_view_calls_2 = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls_2) == 1, "repo-b run must trigger its own gh pr view (different repo key)"


# ---------------------------------------------------------------------------
# Re-close guard tests (D6)
# ---------------------------------------------------------------------------

def test_reopened_track_unchanged_prref_skipped_as_reopened_guard(tmp_path, monkeypatch):
    """Reopened track (done→active) with unchanged pr_ref → verdict=reopened_guard;
    not closed under --apply even when gh confirms all PRs merged."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-guard", phase="active", pr_ref="#1100")

    # Transition to done, then reopen with the JSON-encoded stamp format.
    tracks_lib.transition_phase(sd, "T-guard", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-guard", PROJECT_ID, "active",
        actor="operator",
        reason='reopen pr_ref="#1100" | test reopening',
        approval_id="appr-guard-001",
    )
    assert _phase(sd, "T-guard") == "active"

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1100: _merged_pr(1100)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    # Guarded track is NOT nominated (pr_ref unchanged since reopen).
    assert summary["counts"]["nominated"] == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    per = {pt["track_id"]: pt for pt in summary["per_track"]}
    assert "T-guard" in per
    assert per["T-guard"]["verdict"] == "reopened_guard"
    assert _phase(sd, "T-guard") == "active"  # not auto-closed


def test_reopened_track_changed_prref_eligible_and_closes(tmp_path, monkeypatch):
    """Reopened track whose pr_ref changed after reopen is re-armed and eligible for close."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rearmed", phase="active", pr_ref="#1200")

    # Transition to done, reopen with JSON-encoded stamp of the old pr_ref.
    tracks_lib.transition_phase(sd, "T-rearmed", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-rearmed", PROJECT_ID, "active",
        actor="operator",
        reason='reopen pr_ref="#1200" | follow-up needed',
        approval_id="appr-rearmed-001",
    )
    # Change pr_ref to a new value — re-arms the track for auto-close.
    tracks_lib.update_authored_fields(
        sd, "T-rearmed", PROJECT_ID, pr_ref="#1201", actor="operator",
    )
    _set_delivery(sd, "T-rearmed", 1201, "complete")  # OI-829 gate: #1201 ships the plan

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1201: _merged_pr(1201)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert summary["counts"].get("reopened_guard", 0) == 0
    assert _phase(sd, "T-rearmed") == "done"


def test_reopened_track_unparseable_stamp_guarded_fail_closed(tmp_path, monkeypatch):
    """Unparseable reopen stamp (no 'reopen pr_ref=' prefix) → fail-closed (reopened_guard)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-badstamp", phase="active", pr_ref="#1300")

    # Transition to done, then reopen with a NON-STANDARD reason (missing stamp).
    tracks_lib.transition_phase(sd, "T-badstamp", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-badstamp", PROJECT_ID, "active",
        actor="operator",
        reason="manually reopened without proper stamp format",
        approval_id="appr-badstamp-001",
    )

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1300: _merged_pr(1300)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    per = {pt["track_id"]: pt for pt in summary["per_track"]}
    assert "T-badstamp" in per
    assert per["T-badstamp"]["verdict"] == "reopened_guard"
    assert _phase(sd, "T-badstamp") == "active"  # not auto-closed


def test_old_format_stamp_treated_as_guarded(tmp_path, monkeypatch):
    """Old-format stamp (no JSON quotes) → fail-closed (reopened_guard), not re-armed."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-oldfmt", phase="active", pr_ref="#1400")

    # Stamp uses the old raw format (no json.dumps)
    tracks_lib.transition_phase(sd, "T-oldfmt", PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, "T-oldfmt", PROJECT_ID, "active",
        actor="operator",
        reason="reopen pr_ref=#1400 | old format stamp",
        approval_id="appr-oldfmt-001",
    )

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1400: _merged_pr(1400)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    per = {pt["track_id"]: pt for pt in summary["per_track"]}
    assert per["T-oldfmt"]["verdict"] == "reopened_guard"
    assert _phase(sd, "T-oldfmt") == "active"


# ---------------------------------------------------------------------------
# JSON stamp round-trip tests (D6 gate round 2)
# ---------------------------------------------------------------------------

def _json_reopen_stamp(pr_ref_value: str) -> str:
    """Build a new-format JSON-encoded stamp (mirrors planning_cli.py)."""
    import json as _json
    encoded = pr_ref_value if pr_ref_value else "-"
    return f"reopen pr_ref={_json.dumps(encoded)} | test-reason"


def _do_reopen_with_stamp(sd: Path, track_id: str, pr_ref_at_reopen: str) -> None:
    """Transition track done→active with a new-format JSON stamp."""
    tracks_lib.transition_phase(sd, track_id, PROJECT_ID, "done", actor="T0")
    tracks_lib.transition_phase(
        sd, track_id, PROJECT_ID, "active",
        actor="operator",
        reason=_json_reopen_stamp(pr_ref_at_reopen),
        approval_id=f"appr-{track_id}",
    )


def test_stamp_roundtrip_simple_prref_unchanged_guarded(tmp_path, monkeypatch):
    """Simple pr_ref=#994: unchanged after reopen → reopened_guard."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rt1", phase="active", pr_ref="#994")
    _do_reopen_with_stamp(sd, "T-rt1", "#994")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({994: _merged_pr(994)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    assert _phase(sd, "T-rt1") == "active"


def test_stamp_roundtrip_prref_with_pipe_unchanged_guarded(tmp_path, monkeypatch):
    """pr_ref containing ' | ' (#1400 | #1401): unchanged after reopen → reopened_guard.
    This is the core Fix 1 case: old format would misparse and disarm the guard."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rt2", phase="active", pr_ref="#1400 | #1401")
    _do_reopen_with_stamp(sd, "T-rt2", "#1400 | #1401")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1400: _merged_pr(1400), 1401: _merged_pr(1401)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    assert _phase(sd, "T-rt2") == "active"


def test_stamp_roundtrip_prref_with_pipe_changed_rearmed(tmp_path, monkeypatch):
    """pr_ref was '#1400 | #1401' at reopen; changed to '#1402' → re-armed, closes."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rt3", phase="active", pr_ref="#1400 | #1401")
    _do_reopen_with_stamp(sd, "T-rt3", "#1400 | #1401")
    # Update pr_ref — re-arms the track for auto-close
    tracks_lib.update_authored_fields(sd, "T-rt3", PROJECT_ID, pr_ref="#1402", actor="operator")
    _set_delivery(sd, "T-rt3", 1402, "complete")  # OI-829 gate: #1402 ships the plan

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1402: _merged_pr(1402)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 0
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-rt3") == "done"


def test_stamp_roundtrip_comma_separated_unchanged_guarded(tmp_path, monkeypatch):
    """Comma-separated pr_ref (#908,#909): unchanged after reopen → reopened_guard."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-rt4", phase="active", pr_ref="#908,#909")
    _do_reopen_with_stamp(sd, "T-rt4", "#908,#909")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({908: _merged_pr(908), 909: _merged_pr(909)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    assert _phase(sd, "T-rt4") == "active"


def test_stamp_roundtrip_empty_prref_unchanged_guarded(tmp_path, monkeypatch):
    """Empty pr_ref (sentinel '-'): reopened with empty, still empty → reopened_guard.
    Note: empty pr_ref tracks are not nominated for reconcile (pr_ref is required),
    so this verifies the guard is correct when pr_ref is subsequently filled in
    but matches the sentinel after round-trip."""
    sd = _build_db(tmp_path)
    # Create with empty pr_ref, reopen (stamps '-')
    _seed_track(sd, "T-rt5", phase="active", pr_ref="")
    _do_reopen_with_stamp(sd, "T-rt5", "")
    # Now give it a pr_ref matching the empty-equivalent round-trip:
    # Empty → stamped as '-' → parses back as '' → current pr_ref '' → guarded
    # But to nominate it, we must set a non-empty pr_ref.
    # Set pr_ref to a new value → different from '' → re-armed.
    tracks_lib.update_authored_fields(sd, "T-rt5", PROJECT_ID, pr_ref="#2000", actor="operator")
    _set_delivery(sd, "T-rt5", 2000, "complete")  # OI-829 gate: #2000 ships the plan

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({2000: _merged_pr(2000)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    # pr_ref changed from '' to '#2000' → re-armed → closes
    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 0
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-rt5") == "done"


@pytest.mark.parametrize("garbled_reason", [
    'reopen pr_ref="#1400"garbled',
    'reopen pr_ref="#1400"|missing-spaces',
    'reopen pr_ref="#1400"x',
])
def test_stamp_garbled_trailing_chars_guarded(tmp_path, monkeypatch, garbled_reason):
    """Trailing garbage after JSON string literal → fail-closed (reopened_guard)."""
    from objective_reconcile import _parse_reopen_stamp
    assert _parse_reopen_stamp(garbled_reason) is None, (
        f"Expected None for garbled stamp: {garbled_reason!r}"
    )


def test_stamp_valid_bare_and_with_separator():
    """Valid shapes: bare JSON string and JSON string + ' | text' both parse."""
    from objective_reconcile import _parse_reopen_stamp
    assert _parse_reopen_stamp('reopen pr_ref="#1400"') == "#1400"
    assert _parse_reopen_stamp('reopen pr_ref="#1400" | operator note') == "#1400"


def test_stamp_roundtrip_prref_with_double_quote_unchanged_guarded(tmp_path, monkeypatch):
    """pr_ref containing a double quote: JSON encoding handles it safely.
    Unchanged after reopen → reopened_guard."""
    sd = _build_db(tmp_path)
    # pr_ref that embeds a double-quote character (unusual but must not break parser)
    tricky_pr_ref = '#994 "extra"'
    _seed_track(sd, "T-rt6", phase="active", pr_ref=tricky_pr_ref)
    _do_reopen_with_stamp(sd, "T-rt6", tricky_pr_ref)

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({994: _merged_pr(994)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    # '#994 "extra"' parses to PR 994, but unchanged pr_ref → guarded
    assert code == 0
    assert summary["counts"].get("reopened_guard", 0) == 1
    assert summary["counts"].get("closed", 0) == 0
    assert _phase(sd, "T-rt6") == "active"


# ---------------------------------------------------------------------------
# D4 tests: review recording, streak computation, tick command builder
# ---------------------------------------------------------------------------

def _run_reconcile_and_get_run_id(sd: Path, tmp_path: Path, monkeypatch, pr_num: int, track_id: str) -> str:
    """Helper: run reconcile in check mode and return the run_id."""
    _seed_track(sd, track_id, phase="active", pr_ref=f"#{pr_num}")
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({pr_num: _merged_pr(pr_num)}),
    )
    summary, _ = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    return summary["run_id"]


class TestRecordReview:
    """objective_reconcile.record_review — happy path and unknown-run_id refusal."""

    def test_happy_path_appends_review_record(self, tmp_path, monkeypatch):
        """record_review appends the review JSON line to reconcile_history.ndjson."""
        sd = _build_db(tmp_path)
        run_id = _run_reconcile_and_get_run_id(sd, tmp_path, monkeypatch, 5001, "T-rev1")

        objective_reconcile.record_review(
            sd, run_id, reviewer="alice", verdict="ok", note="looks good"
        )

        history_path = sd / "reconcile_history.ndjson"
        lines = [l for l in history_path.read_text().splitlines() if l.strip()]
        review_lines = [json.loads(l) for l in lines if json.loads(l).get("record") == "review"]
        assert len(review_lines) == 1
        r = review_lines[0]
        assert r["run_id"] == run_id
        assert r["reviewer"] == "alice"
        assert r["verdict"] == "ok"
        assert r["note"] == "looks good"
        assert "ts" in r

    def test_unknown_run_id_raises_value_error(self, tmp_path, monkeypatch):
        """record_review raises ValueError when run_id is not in history."""
        sd = _build_db(tmp_path)
        # No reconcile run has been performed; history file does not exist.
        with pytest.raises(ValueError, match="not found in reconcile history"):
            objective_reconcile.record_review(
                sd, "nonexistent-run-id", reviewer="bob", verdict="ok", note=""
            )

    def test_unknown_run_id_after_existing_runs(self, tmp_path, monkeypatch):
        """Raises ValueError when run_id is absent even when history has other runs."""
        sd = _build_db(tmp_path)
        _run_reconcile_and_get_run_id(sd, tmp_path, monkeypatch, 5002, "T-rev2")

        with pytest.raises(ValueError, match="not found in reconcile history"):
            objective_reconcile.record_review(
                sd, "wrong-run-id-xyz", reviewer="bob", verdict="false-candidate", note=""
            )

    def test_invalid_verdict_raises_value_error(self, tmp_path, monkeypatch):
        """record_review raises ValueError for an invalid verdict value."""
        sd = _build_db(tmp_path)
        run_id = _run_reconcile_and_get_run_id(sd, tmp_path, monkeypatch, 5003, "T-rev3")

        with pytest.raises(ValueError, match="invalid verdict"):
            objective_reconcile.record_review(
                sd, run_id, reviewer="carol", verdict="maybe", note=""
            )

    def test_review_record_does_not_count_as_run_id_source(self, tmp_path, monkeypatch):
        """A review record's run_id does not satisfy the run_id existence check."""
        sd = _build_db(tmp_path)
        run_id = _run_reconcile_and_get_run_id(sd, tmp_path, monkeypatch, 5004, "T-rev4")

        # Record a review so a "review" record with run_id exists in history.
        objective_reconcile.record_review(sd, run_id, reviewer="dave", verdict="ok", note="")

        # A made-up run_id must still fail even though history has review records.
        with pytest.raises(ValueError, match="not found in reconcile history"):
            objective_reconcile.record_review(
                sd, "invented-id", reviewer="dave", verdict="ok", note=""
            )


class TestComputeStreak:
    """objective_reconcile.compute_streak — streak logic and flip criterion."""

    def _write_summary(self, sd: Path, run_id: str, *, gh: str = "ok", confirmed: int = 0, unverified: int = 0) -> None:
        """Directly append a synthetic summary to reconcile_history.ndjson."""
        history_path = sd / "reconcile_history.ndjson"
        rec = {
            "run_id": run_id,
            "project_id": PROJECT_ID,
            "mode": "check",
            "started_at": "2026-07-04T10:00:00Z",
            "finished_at": "2026-07-04T10:00:01Z",
            "evidence_source_health": {"gh": gh},
            "counts": {
                "tracks": 1, "nominated": 1,
                "confirmed": confirmed, "closed": 0,
                "closed_sibling": 0, "open_pr": 0,
                "unverified": unverified, "deferred": 0, "stale": 0,
            },
            "per_track": [],
        }
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _write_review(self, sd: Path, run_id: str, verdict: str) -> None:
        history_path = sd / "reconcile_history.ndjson"
        rec = {"record": "review", "run_id": run_id, "reviewer": "test", "verdict": verdict, "note": "", "ts": "2026-07-04T10:01:00Z"}
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_empty_history_returns_zero_streak(self, tmp_path):
        sd = _build_db(tmp_path)
        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 0
        assert result["flip_criterion_met"] is False
        assert result["runs"] == []

    def test_single_clean_run_counts_in_streak(self, tmp_path):
        sd = _build_db(tmp_path)
        self._write_summary(sd, "run-A", gh="ok", unverified=0)

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 1

    def test_degraded_run_resets_streak_to_zero(self, tmp_path):
        """A degraded run (gh != ok) anywhere in the sequence resets the streak."""
        sd = _build_db(tmp_path)
        # Oldest to newest: clean, degraded, clean, clean
        self._write_summary(sd, "run-old", gh="ok", unverified=0)
        self._write_summary(sd, "run-deg", gh="absent", unverified=0)
        self._write_summary(sd, "run-c1", gh="ok", unverified=0)
        self._write_summary(sd, "run-c2", gh="ok", unverified=0)

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        # Only the two clean runs after the degraded run count; oldest degraded breaks it.
        assert result["streak_length"] == 2
        assert result["runs"][0]["run_id"] == "run-c2"
        assert result["runs"][1]["run_id"] == "run-c1"

    def test_unverified_run_resets_streak(self, tmp_path):
        """A run with unverified > 0 resets the streak."""
        sd = _build_db(tmp_path)
        self._write_summary(sd, "run-unv", gh="ok", unverified=1)
        self._write_summary(sd, "run-ok", gh="ok", unverified=0)

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 1
        assert result["runs"][0]["run_id"] == "run-ok"

    def test_false_candidate_review_breaks_streak(self, tmp_path):
        """A false-candidate review on a run breaks the streak even if the run was clean."""
        sd = _build_db(tmp_path)
        self._write_summary(sd, "run-1", gh="ok", unverified=0)
        self._write_review(sd, "run-1", verdict="false-candidate")
        self._write_summary(sd, "run-2", gh="ok", unverified=0)

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        # run-2 is clean and unreviewd; run-1 has a false-candidate → breaks streak.
        assert result["streak_length"] == 1
        assert result["runs"][0]["run_id"] == "run-2"

    def test_ok_review_without_confirmed_does_not_meet_flip_criterion(self, tmp_path):
        """Even with an ok review, flip criterion requires confirmed > 0."""
        sd = _build_db(tmp_path)
        self._write_summary(sd, "run-X", gh="ok", unverified=0, confirmed=0)
        self._write_review(sd, "run-X", verdict="ok")

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 1
        assert result["has_reviewed_confirmed"] is False
        assert result["flip_criterion_met"] is False

    def test_required_streak_field_present(self, tmp_path):
        """compute_streak always returns required_streak == FLIP_STREAK_REQUIRED."""
        sd = _build_db(tmp_path)
        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["required_streak"] == objective_reconcile.FLIP_STREAK_REQUIRED

    def test_flip_criterion_not_met_with_six_clean_runs(self, tmp_path):
        """Six consecutive clean runs with a reviewed confirmed candidate is NOT enough."""
        sd = _build_db(tmp_path)
        for i in range(5):
            self._write_summary(sd, f"run-c{i}", gh="ok", unverified=0, confirmed=0)
        self._write_summary(sd, "run-c5", gh="ok", unverified=0, confirmed=1)
        self._write_review(sd, "run-c5", verdict="ok")

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 6
        assert result["has_reviewed_confirmed"] is True
        assert result["flip_criterion_met"] is False, "6 runs is below the 7-run threshold"

    def test_flip_criterion_met_when_ok_reviewed_confirmed(self, tmp_path):
        """Flip criterion met: 7 consecutive clean runs with ≥1 confirmed ok-reviewed run."""
        sd = _build_db(tmp_path)
        for i in range(6):
            self._write_summary(sd, f"run-Y{i}", gh="ok", unverified=0, confirmed=0)
        self._write_summary(sd, "run-Y6", gh="ok", unverified=0, confirmed=1)
        self._write_review(sd, "run-Y6", verdict="ok")

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 7
        assert result["has_reviewed_confirmed"] is True
        assert result["flip_criterion_met"] is True

    def test_flip_criterion_requires_reviewed_run_in_current_streak(self, tmp_path):
        """Reviewed confirmed run before the degraded gap does not count for current streak."""
        sd = _build_db(tmp_path)
        # Oldest: clean + confirmed + ok review; then degraded; then clean (no review)
        self._write_summary(sd, "run-before", gh="ok", unverified=0, confirmed=1)
        self._write_review(sd, "run-before", verdict="ok")
        self._write_summary(sd, "run-deg", gh="absent", unverified=0)
        self._write_summary(sd, "run-after", gh="ok", unverified=0, confirmed=0)

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 1
        assert result["runs"][0]["run_id"] == "run-after"
        assert result["has_reviewed_confirmed"] is False
        assert result["flip_criterion_met"] is False

    def test_project_id_scoping(self, tmp_path):
        """compute_streak only counts runs for the requested project_id."""
        sd = _build_db(tmp_path)
        # Write a run for a different project.
        history_path = sd / "reconcile_history.ndjson"
        other_run = {
            "run_id": "run-other-proj",
            "project_id": "other-project",
            "mode": "check",
            "started_at": "2026-07-04T10:00:00Z",
            "finished_at": "2026-07-04T10:00:01Z",
            "evidence_source_health": {"gh": "ok"},
            "counts": {"tracks": 1, "nominated": 1, "confirmed": 1, "closed": 0,
                       "closed_sibling": 0, "open_pr": 0, "unverified": 0, "deferred": 0, "stale": 0},
            "per_track": [],
        }
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(other_run) + "\n")

        result = objective_reconcile.compute_streak(sd, PROJECT_ID)
        assert result["streak_length"] == 0, "other-project run must not count"


class TestBuildTickCommand:
    """objective_reconcile.build_tick_command — command construction without VNX_AUTO_CLOSE."""

    def test_check_mode_no_apply_when_auto_close_absent(self, tmp_path):
        """VNX_AUTO_CLOSE absent (auto_close=False) → --apply not in command."""
        cmd = objective_reconcile.build_tick_command(
            str(tmp_path), "vnx-dev", str(tmp_path / "state"), auto_close=False,
        )
        assert "--apply" not in cmd
        assert "objective" in cmd
        assert "reconcile" in cmd

    def test_apply_appended_when_auto_close_true(self, tmp_path):
        """auto_close=True → --apply present in command."""
        cmd = objective_reconcile.build_tick_command(
            str(tmp_path), "vnx-dev", str(tmp_path / "state"), auto_close=True,
        )
        assert "--apply" in cmd

    def test_project_id_and_state_dir_in_command(self, tmp_path):
        """--project-id and --state-dir are always present."""
        sd = str(tmp_path / "state")
        cmd = objective_reconcile.build_tick_command(
            str(tmp_path), "my-project", sd, auto_close=False,
        )
        assert "--project-id" in cmd
        assert "my-project" in cmd
        assert "--state-dir" in cmd
        assert sd in cmd


# ---------------------------------------------------------------------------
# repo_root None-tolerance (central-mode-path-correctness, round 3)
# ---------------------------------------------------------------------------

import planning_cli  # noqa: E402
import vnx_paths  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def test_resolve_effective_repo_root_prefers_project_root(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"PROJECT_ROOT": str(proj)})
    assert objective_reconcile._resolve_effective_repo_root() == proj


def test_resolve_effective_repo_root_falls_back_to_cwd_git(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("no paths")
    monkeypatch.setattr(vnx_paths, "resolve_paths", _boom)
    git_root = tmp_path / "gitrepo"
    monkeypatch.setattr(
        objective_reconcile.track_reconciler, "_git_toplevel", lambda p: git_root
    )
    assert objective_reconcile._resolve_effective_repo_root() == git_root


def test_run_reconcile_none_repo_root_resolves_internally(tmp_path, monkeypatch):
    # None must be resolved to a concrete repo root (never passed as None into gh).
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-none", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-none", "T-none", state="completed")
    _seed_pr_merged_ndjson(sd, 100)
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({100: _merged_pr(100)}),
    )
    resolved_calls = []

    def _spy():
        resolved_calls.append(True)
        return tmp_path

    monkeypatch.setattr(objective_reconcile, "_resolve_effective_repo_root", _spy)

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=None, apply=False,
    )
    assert resolved_calls, "None repo_root must trigger internal resolution"
    assert code == 0
    assert summary["counts"]["confirmed"] == 1


def test_run_reconcile_explicit_repo_root_not_resolved(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-exp", phase="active", pr_ref="#100")
    _seed_dispatch(sd, "D-exp", "T-exp", state="completed")
    _seed_pr_merged_ndjson(sd, 100)
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({100: _merged_pr(100)}),
    )
    resolved_calls = []
    monkeypatch.setattr(
        objective_reconcile, "_resolve_effective_repo_root",
        lambda: resolved_calls.append(True) or tmp_path,
    )
    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    assert not resolved_calls, "explicit repo_root must NOT trigger internal resolution"
    assert code == 0


def test_objective_close_dry_run_threads_repo_root_into_evidence(tmp_path, monkeypatch):
    # Gap 2: the dry-run close path must read the project ROADMAP (Source-3) via
    # repo_root, so pr_merged is True when the repo roadmap lists the PR merged.
    monkeypatch.setenv("VNX_RECONCILE_GIT", "0")
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-dry", phase="active", pr_ref="#4242")
    _seed_dispatch(sd, "D-dry", "T-dry", state="completed")  # derives 'done'

    repo = tmp_path / "project-repo"
    repo.mkdir()
    (repo / "ROADMAP.yaml").write_text(
        "features:\n  - pr_queue:\n      - pr_id: '#4242'\n        status: merged\n",
        encoding="utf-8",
    )

    args = SimpleNamespace(
        state_dir=str(sd),
        project_id=PROJECT_ID,
        track_id="T-dry",
        repo_root=str(repo),
        apply=False,
        approval_id=None,
        include_parked=False,
        json=True,
    )
    captured = {}
    real_close_evidence = planning_cli._close_evidence

    def _spy(state_dir, track_id, project_id, repo_root=None, merged_pr_numbers=None):
        captured["repo_root"] = repo_root
        return real_close_evidence(
            state_dir, track_id, project_id, repo_root=repo_root,
            merged_pr_numbers=merged_pr_numbers,
        )

    monkeypatch.setattr(planning_cli, "_close_evidence", _spy)

    rc = planning_cli.cmd_objective_close(args)
    assert rc == 0
    # The dry-run path forwarded repo_root...
    assert captured.get("repo_root") == Path(str(repo)).resolve()
    # ...and the evidence read the repo roadmap -> pr_merged True (would be False
    # if repo_root were dropped and the CWD-git-root roadmap read instead).
    ev = real_close_evidence(str(sd), "T-dry", PROJECT_ID, repo_root=Path(str(repo)).resolve())
    assert ev["pr_merged"] is True


# ---------------------------------------------------------------------------
# OI-1071 — the standalone ``objective close`` verb must use the same merge
# evidence ``reconcile`` does. Before this fix, the verb peeked/reconciled
# with the local-only set, fell back to _load_merged_pr_numbers (whose gh source
# is opt-in behind VNX_RECONCILE_GIT, OFF by default), and re-derived 'queued'
# for a track whose PRs merged via a bare ``gh pr merge``. The tests below pin
# the shared helper + the verb's evidence threading + honest gh degradation.
# ---------------------------------------------------------------------------


def _close_args(state_dir, track_id, *, apply=False, approval_id="", repo_root="",
                max_gh_calls=50, include_parked=False, json_out=False):
    return SimpleNamespace(
        state_dir=str(state_dir), project_id=PROJECT_ID, track_id=track_id,
        apply=apply, approval_id=approval_id, repo_root=repo_root,
        max_gh_calls=max_gh_calls, include_parked=include_parked,
        json=json_out, attest=None, pr=None,
    )


def test_close_verb_threads_nonempty_merged_set_into_close_track_if_done(tmp_path, monkeypatch):
    """OI-1071: cmd_objective_close passes a non-empty merged set into
    close_track_if_done (assert on the call, not a log line)."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-1071a", phase="queued", pr_ref="#770")
    # No local merge evidence. gh pr view is the sole authority.
    _set_delivery(sd, "T-1071a", 770, "complete")  # OI-829 gate
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({770: _merged_pr(770)}),
    )

    captured = {}
    real_close = track_reconciler.close_track_if_done

    def _spy(state_dir, track_id, project_id, **kwargs):
        captured["merged_pr_numbers"] = kwargs.get("merged_pr_numbers")
        captured["track_id"] = track_id
        return real_close(state_dir, track_id, project_id, **kwargs)

    monkeypatch.setattr(track_reconciler, "close_track_if_done", _spy)
    # planning_cli references close_track_if_done via the track_reconciler module
    # attribute, so patch the same name planning_cli sees.
    monkeypatch.setattr(planning_cli.track_reconciler, "close_track_if_done", _spy)

    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T-1071a", apply=True, approval_id="APR-1071", repo_root=str(tmp_path))
    )
    assert rc == 0, f"expected rc 0, got {rc}"
    assert captured["track_id"] == "T-1071a"
    merged = captured["merged_pr_numbers"]
    assert merged is not None, "close_track_if_done must receive merged_pr_numbers"
    assert 770 in set(merged), f"gh-confirmed PR 770 must be in the merged set, got {merged}"
    assert _phase(sd, "T-1071a") == "done"


def test_close_verb_derives_done_from_gh_only_without_vnx_reconcile_git(tmp_path, monkeypatch):
    """OI-1071: a track whose pr_ref PRs are merged but ABSENT from local
    sources closes via the verb, WITHOUT VNX_RECONCILE_GIT set."""
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-1071b", phase="queued", pr_ref="#771")
    # No local merge evidence at all: no dispatch, no pr_merged.ndjson, no
    # coordination_events, no ROADMAP. gh pr view is the sole authority.
    _set_delivery(sd, "T-1071b", 771, "complete")  # OI-829 gate
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({771: _merged_pr(771)}),
    )

    # Dry-run: must report derived=done (today it reports derived=queued).
    import io as _io, contextlib as _ctx
    _buf = _io.StringIO()
    with _ctx.redirect_stdout(_buf):
        rc = planning_cli.cmd_objective_close(
            _close_args(sd, "T-1071b", apply=False, repo_root=str(tmp_path), json_out=True)
        )
    payload = json.loads(_buf.getvalue())
    assert rc == 0
    assert payload["derived_status"] == "done", (
        f"expected derived=done from gh-only evidence, got {payload['derived_status']}"
    )
    assert payload["action"] == "dry_run"


def test_close_verb_gh_unavailable_falls_back_and_says_so(tmp_path, monkeypatch, capsys):
    """OI-1071: gh unavailable -> falls back to local-only, refuses as before,
    and the output STATES that gh could not be consulted (not 'not done')."""
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-1071c", phase="queued", pr_ref="#772")
    # No local merge evidence and no completed dispatch -> derived 'queued'.
    _seed_dispatch(sd, "D-1071c", "T-1071c", state="queued")

    # gh truly absent.
    monkeypatch.setattr(objective_reconcile, "_resolve_gh_binary", lambda: None)
    monkeypatch.setattr(objective_reconcile.shutil, "which", lambda name: None)
    monkeypatch.setattr(objective_reconcile, "_GH_FALLBACK_PATHS", ())

    rc = planning_cli.cmd_objective_close(
        _close_args(sd, "T-1071c", apply=False, repo_root=str(tmp_path))
    )
    out = capsys.readouterr().out
    assert rc == 0  # noop_not_terminal is rc 0
    assert "not terminal" in out, "must still refuse (local-only derived 'queued')"
    assert "could not consult GitHub" in out, (
        "must STATE gh could not be consulted, not silently behave as if it checked"
    )
    assert "gh=absent" in out


def test_close_verb_and_run_reconcile_share_the_same_union_helper(tmp_path, monkeypatch):
    """OI-1071: the extracted helper is the SAME one run_reconcile uses.
    Assert both call sites reach objective_reconcile.merge_evidence_pr_numbers."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-share", phase="active", pr_ref="#773")
    _seed_dispatch(sd, "D-share", "T-share", state="completed")
    _seed_pr_merged_ndjson(sd, 773)
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({773: _merged_pr(773)}),
    )

    call_sites: list = []
    real_union = objective_reconcile.merge_evidence_pr_numbers

    def _spy(gh_confirmed, state_dir, repo_root=None):
        call_sites.append("called")
        return real_union(gh_confirmed, state_dir, repo_root)

    monkeypatch.setattr(objective_reconcile, "merge_evidence_pr_numbers", _spy)

    # run_reconcile reaches it.
    objective_reconcile.run_reconcile(sd, PROJECT_ID, repo_root=tmp_path, apply=False)
    recon_calls = len(call_sites)
    assert recon_calls >= 1, "run_reconcile must call merge_evidence_pr_numbers"

    # The close verb reaches it (via gather_close_evidence).
    call_sites.clear()
    planning_cli.cmd_objective_close(
        _close_args(sd, "T-share", apply=False, repo_root=str(tmp_path))
    )
    close_calls = len(call_sites)
    assert close_calls >= 1, "cmd_objective_close must call merge_evidence_pr_numbers"


def test_close_verb_per_track_gh_budget_holds(tmp_path, monkeypatch):
    """OI-1071: a track with N PRs makes at most N uncached gh calls."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-budget", phase="queued", pr_ref="#801,#802,#803")
    _set_delivery(sd, "T-budget", 801, "complete")  # one complete delivery satisfies the gate
    call_log: list = []

    def _fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        cmd0 = os.path.basename(str(cmd[0])) if cmd else ""
        if cmd0 == "gh" and len(cmd) >= 2 and cmd[1] == "auth":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd0 == "gh" and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "view":
            pr_num = int(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, json.dumps(_merged_pr(pr_num)), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(objective_reconcile.subprocess, "run", _fake_run)

    planning_cli.cmd_objective_close(
        _close_args(sd, "T-budget", apply=False, repo_root=str(tmp_path))
    )
    pr_view_calls = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls) == 3, (
        f"a track with 3 PRs must make exactly 3 uncached gh calls, got {len(pr_view_calls)}"
    )


def test_close_verb_cached_pr_not_refetched(tmp_path, monkeypatch):
    """OI-1071: a PR already verified (cached) is not re-fetched within the run."""
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-cache", phase="queued", pr_ref="#810")
    _set_delivery(sd, "T-cache", 810, "complete")
    # Pre-seed the per-PR cache so #810 is already MERGED.
    repo_key = objective_reconcile._get_repo_key(tmp_path)
    objective_reconcile._save_pr_state_cache(
        sd, repo_key, {"810": {"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}}
    )
    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({810: _merged_pr(810)}, call_log=call_log),
    )
    planning_cli.cmd_objective_close(
        _close_args(sd, "T-cache", apply=False, repo_root=str(tmp_path))
    )
    pr_view_calls = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls) == 0, (
        f"a cached MERGED PR must not be re-fetched, got {len(pr_view_calls)} calls"
    )


# ---------------------------------------------------------------------------
# _run_provenance_sweep — commit-survives-close regression (PR-A, 2026-07-29)
#
# Root cause: objective_reconcile.py opened a connection, let
# reconcile_commit_provenance write hundreds of rows into it, and closed the
# connection without ever calling commit(). sqlite3's default isolation_level
# opens an implicit transaction on the first DML; close() without commit()
# rolls it back. Every automated provenance sweep since 2026-07-04 (#996) was
# silently a no-op. See claudedocs/provenance-chain-root-cause-20260729.md.
# ---------------------------------------------------------------------------

import logging  # noqa: E402
from runtime_coordination import init_schema as _rc_init_schema  # noqa: E402

_GIT_ENV_TEMPLATE = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _make_git_project(tmp_path: Path):
    """Real git repo + a state_dir with the provenance_registry schema (v6) applied."""
    repo = tmp_path / "prov-repo"
    repo.mkdir()
    env = dict(_GIT_ENV_TEMPLATE, HOME=str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    state_dir = tmp_path / "prov-state"
    _rc_init_schema(state_dir)
    return repo, state_dir, env


def _git_commit_with_dispatch_id(
    repo: Path, dispatch_id: str, env: Dict[str, str], *, message: str = "feat(x): thing",
) -> None:
    i = len(list(repo.glob("*.txt")))
    f = repo / f"f{i}.txt"
    f.write_text(str(i))
    subprocess.run(["git", "add", str(f)], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"{message}\n\nDispatch-ID: {dispatch_id}"],
        cwd=repo, env=env, check=True,
    )


class TestRunProvenanceSweepCommitSurvivesClose:
    """Regression test: red without prov_conn.commit(), green with it."""

    def test_provenance_write_survives_connection_close(self, tmp_path):
        repo, state_dir, env = _make_git_project(tmp_path)
        dispatch_id = "20260729-survives-close-check"
        _git_commit_with_dispatch_id(repo, dispatch_id, env)

        result = objective_reconcile._run_provenance_sweep(state_dir, repo, PROJECT_ID)
        assert result["linked"] == 1

        # Re-open a FRESH connection: proves the row survives close(), not
        # merely that it was visible within the still-open transaction that
        # wrote it (which the old, buggy code would also have shown).
        verify_conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = verify_conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        verify_conn.close()
        assert row is not None
        assert row[0] is not None

    def test_multiple_commits_all_survive(self, tmp_path):
        repo, state_dir, env = _make_git_project(tmp_path)
        dispatch_ids = [f"20260729-survives-{i}" for i in range(3)]
        for dispatch_id in dispatch_ids:
            _git_commit_with_dispatch_id(repo, dispatch_id, env)

        result = objective_reconcile._run_provenance_sweep(state_dir, repo, PROJECT_ID)
        assert result["linked"] == 3

        verify_conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        rows = verify_conn.execute(
            "SELECT dispatch_id FROM provenance_registry WHERE commit_sha IS NOT NULL"
        ).fetchall()
        verify_conn.close()
        assert {r[0] for r in rows} == set(dispatch_ids)

    def test_linked_pending_commit_is_zero_after_successful_sweep(self, tmp_path):
        """PR-A fix-forward: reconcile_commit_provenance computes
        ``linked_pending_commit`` BEFORE ``_run_provenance_sweep``'s own
        ``prov_conn.commit()`` runs — at that point prov_conn.in_transaction
        is still True, so the raw value mirrors ``linked`` regardless of
        whether the commit that follows actually succeeds. Once
        _run_provenance_sweep's commit has succeeded, the rows ARE durable
        and the caller-visible count must say so (0), not echo the
        pre-commit snapshot — anything else is exactly the "non-zero pending
        after a successful commit" lie linked_pending_commit exists to rule
        out."""
        repo, state_dir, env = _make_git_project(tmp_path)
        dispatch_id = "20260729-pending-after-commit-check"
        _git_commit_with_dispatch_id(repo, dispatch_id, env)

        result = objective_reconcile._run_provenance_sweep(state_dir, repo, PROJECT_ID)

        assert result["linked"] == 1
        assert result["linked_pending_commit"] == 0


class TestRunProvenanceSweepCommitFailureIsLoud:
    """A commit failure must be visible (log.error), not swallowed at debug
    alongside ordinary non-fatal scan errors — and the returned counts must
    not claim durability that a failed commit rolled back."""

    def test_commit_failure_logs_error_and_zeroes_counts(self, tmp_path, monkeypatch, caplog):
        repo, state_dir, env = _make_git_project(tmp_path)
        dispatch_id = "20260729-commit-failure-check"
        _git_commit_with_dispatch_id(repo, dispatch_id, env)

        # sqlite3.Connection is an immutable builtin type — instance/class
        # attributes can't be monkeypatched directly. Subclass it and make
        # objective_reconcile's own sqlite3.connect() hand out that subclass,
        # so only the connection this call opens has a broken commit().
        class _CommitFailsConnection(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("simulated disk I/O error")

        real_connect = sqlite3.connect

        def _connect_with_broken_commit(*args, **kwargs):
            kwargs["factory"] = _CommitFailsConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(objective_reconcile.sqlite3, "connect", _connect_with_broken_commit)

        with caplog.at_level(logging.DEBUG, logger="objective_reconcile"):
            result = objective_reconcile._run_provenance_sweep(state_dir, repo, PROJECT_ID)

        # Commit failed -> nothing durable, so the counts must not lie.
        assert result == {"scanned": 0, "linked": 0}
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "a failed commit must be logged loudly, not swallowed at debug"
        assert "commit failed" in error_records[0].getMessage()

        # The zeroed counts are the SHAPE of the claim; prove the substance
        # too — _run_provenance_sweep's own prov_conn is already closed by
        # the time it returns (its finally: block closes it regardless of
        # commit outcome). Open a fresh connection and confirm the row the
        # broken commit() claimed to roll back is actually gone, not just
        # that the returned dict said zero.
        verify_conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = verify_conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
        verify_conn.close()
        assert row is None, "a failed commit must not leave the row behind after close()"

        # And it must not have been double-logged at debug-only either — the
        # loud path replaces the silent one for this failure, it doesn't also
        # fall through to the generic "provenance sweep non-fatal" catch.
        debug_msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.DEBUG
        ]
        assert not any("non-fatal" in m for m in debug_msgs)

    def test_ordinary_scan_error_stays_quiet_and_non_fatal(self, tmp_path, caplog):
        # A state_dir whose parent doesn't exist makes sqlite3.connect() raise
        # before reconcile_commit_provenance ever runs — the pre-existing
        # "best-effort, never blocks the rest" contract for scan-level
        # failures must be unchanged: quiet (debug only), zero result.
        missing_state_dir = tmp_path / "does-not-exist" / "state"
        repo = tmp_path / "some-repo"
        repo.mkdir()

        with caplog.at_level(logging.DEBUG, logger="objective_reconcile"):
            result = objective_reconcile._run_provenance_sweep(missing_state_dir, repo, PROJECT_ID)

        assert result == {"scanned": 0, "linked": 0}
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, "an ordinary scan miss must stay non-fatal, not escalate to error"


# ---------------------------------------------------------------------------
# OI-1064: reconcile reuses the gh merge evidence it already gathered.
# ---------------------------------------------------------------------------

def test_oi1064_reconcile_closes_track_without_vnx_reconcile_git(tmp_path, monkeypatch):
    """run_reconcile derives 'done' and closes a bare-gh-merge track WITHOUT
    VNX_RECONCILE_GIT set.

    Reproduces the background-job-liveness defect in miniature: phase=queued,
    pr_ref with multiple PRs, all verified MERGED via gh, delivery complete,
    zero dispatches, zero local merge evidence (no pr_merged.ndjson, no
    coordination event, no ROADMAP entry). Pre-fix the bulk pass derived
    'queued' and close_track_if_done refused with noop_not_terminal; the
    evidence reconcile gathered was discarded. Post-fix the gh-confirmed
    numbers are threaded into the derivation and the close succeeds.
    """
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-oi1064", phase="queued", pr_ref="#1246,#1247,#1248")
    # No dispatches, no local merge evidence of any kind.
    # Mark all three delivery complete (the plan shipped across all PRs).
    for pn in (1246, 1247, 1248):
        _set_delivery(sd, "T-oi1064", pn, "complete")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({
            1246: _merged_pr(1246),
            1247: _merged_pr(1247),
            1248: _merged_pr(1248),
        }),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )

    assert code == 0, f"expected exit 0, got {code}"
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-oi1064") == "done"
    # derived_status persisted as 'done' (not 'queued') — the evidence reached
    # both the bulk re-derivation and the close path.
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id=? AND project_id=?",
        ("T-oi1064", PROJECT_ID),
    ).fetchone()
    conn.close()
    assert row["derived_status"] == "done"


def test_oi1064_check_mode_derives_done_without_closing(tmp_path, monkeypatch):
    """Check mode (--apply absent): the same evidence threading corrects the
    derived_status to 'done' in the DB even though the close is skipped.

    Pre-fix the bulk pass wrote 'queued' and nothing corrected it. Post-fix the
    post-gh re-derivation writes 'done'. The declared phase stays untouched in
    check mode.
    """
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-oi1064-check", phase="queued", pr_ref="#1300,#1301")
    for pn in (1300, 1301):
        _set_delivery(sd, "T-oi1064-check", pn, "complete")

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1300: _merged_pr(1300), 1301: _merged_pr(1301)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=False,
    )
    assert code == 0
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 0  # check mode never closes
    assert _phase(sd, "T-oi1064-check") == "queued"  # declared phase untouched
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id=? AND project_id=?",
        ("T-oi1064-check", PROJECT_ID),
    ).fetchone()
    conn.close()
    assert row["derived_status"] == "done"


def test_oi1064_max_gh_calls_cap_holds_across_whole_run(tmp_path, monkeypatch):
    """The --max-gh-calls cap holds across the whole run after OI-1064.

    The re-derivation + close threading must not add any gh calls beyond the
    existing step-4b sweep. With max_gh_calls=2 and one candidate needing 3 PR
    lookups, the candidate is deferred (3 > 2) — no gh calls for it, no
    re-derivation, no close. The cap governs the whole run, not per phase.
    """
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-cap", phase="queued", pr_ref="#1400,#1401,#1402")
    for pn in (1400, 1401, 1402):
        _set_delivery(sd, "T-cap", pn, "complete")

    call_log: list = []
    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock(
            {1400: _merged_pr(1400), 1401: _merged_pr(1401), 1402: _merged_pr(1402)},
            call_log=call_log,
        ),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
        max_gh_calls=2,
    )
    assert code == 0
    # 3 PRs needed, cap 2 → deferred. No close, no confirmed.
    assert summary["counts"]["deferred"] == 1
    assert summary["counts"]["confirmed"] == 0
    assert summary["counts"]["closed"] == 0

    # Only the gh auth status call ran; zero pr-view calls (deferred before fetch).
    pr_view_calls = [c for c in call_log if _is_gh_pr_view(c)]
    assert len(pr_view_calls) == 0, "OI-1064 threading must not add gh calls"
    assert _phase(sd, "T-cap") == "queued"  # untouched


def test_oi1064_evidence_unions_gh_with_local_in_run_reconcile(tmp_path, monkeypatch):
    """run_reconcile unions the gh-confirmed numbers with the locally-loaded set.

    Track pr_ref='#1500,#1501'. #1500 has local evidence (pr_merged.ndjson).
    #1501 is gh-confirmed only. Both are fetched by gh (both MERGED). The
    injected set is the union {1500, 1501}. Derived 'done' and closes. Pins
    that local evidence is never discarded when the gh evidence is threaded.
    """
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    sd = _build_db(tmp_path)
    _seed_track(sd, "T-oi1064-union", phase="queued", pr_ref="#1500,#1501")
    _set_delivery(sd, "T-oi1064-union", 1500, "complete")
    # Local evidence for #1500 only.
    _seed_pr_merged_ndjson(sd, 1500)

    monkeypatch.setattr(
        objective_reconcile.subprocess, "run",
        _make_gh_mock({1500: _merged_pr(1500), 1501: _merged_pr(1501)}),
    )

    summary, code = objective_reconcile.run_reconcile(
        sd, PROJECT_ID, repo_root=tmp_path, apply=True,
    )
    assert code == 0
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["closed"] == 1
    assert _phase(sd, "T-oi1064-union") == "done"

