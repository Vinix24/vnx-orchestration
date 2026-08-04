"""tests/test_build_t0_state_freshness.py — fabric-freshness reconcile wiring.

Self-contained (no cross-test-module imports). Verifies the SessionStart
hot-path advisory reconcile that keeps the t0_state projection fresh:
- no project_id -> skipped marker (reason="no_project_id"), never raises
- pre-migration store (no derived_status column) -> reason="precondition_unmet"
- a track whose PR merged but whose declared phase is stale -> drift surfaced
- the reconcile resolves the CENTRAL tracks SSOT, not the ambient local store
- the compact index summary mirrors the full marker
- autoclose_degraded is read from reconcile_summary.json (true when gh is
  absent, false for a healthy summary, true when the summary is missing)
- the SessionStart hook command is valid bash, de-swallows stderr, keeps exit 0
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_t0_state as bts  # noqa: E402
import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402

PROJECT_ID = "test-proj"
_MARKER_KEYS = {
    "derived_refreshed", "tracks", "drifted", "drifted_tracks", "reason", "seconds",
    "store", "last_reconcile_at", "last_reconcile_age_hours", "gh_health",
    "nominated_not_closed", "autoclose_degraded", "autoclose_reason",
}


# ---------------------------------------------------------------------------
# Self-contained migration-applied tracks DB (decoupled from test_track_reconciler)
# ---------------------------------------------------------------------------

def _build_tracks_db(tmp_path: Path) -> Path:
    """state_dir with the dispatch/event tables + migrations 0022/0024/0027/0028."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL, project_id TEXT
        )
        """
    )
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
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return state_dir


def _seed_track(state_dir: Path, track_id: str, *, phase: str = "active", pr_ref=None) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID,
        title=f"Track {track_id}", goal_state=f"ship {track_id}", phase=phase, pr_ref=pr_ref,
    )


def _seed_dispatch(state_dir: Path, dispatch_id: str, track_id: str, *, state="completed", pr_ref=None) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) VALUES (?,?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id, pr_ref),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_event(state_dir: Path, dispatch_id: str) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        """
        INSERT INTO coordination_events
            (event_id, event_type, entity_type, entity_id, occurred_at, project_id)
        VALUES (?, 'pr_merged', 'dispatch', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)
        """,
        (f"ev-{dispatch_id}", dispatch_id, PROJECT_ID),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Reconcile marker
# ---------------------------------------------------------------------------

def test_no_project_id_is_skipped_not_an_error(tmp_path):
    marker = bts._reconcile_tracks_fresh(tmp_path, "")
    assert marker["derived_refreshed"] is False
    assert marker["reason"] == "no_project_id"
    assert marker["drifted"] == 0
    assert set(marker) == _MARKER_KEYS


def test_pre_migration_store_reports_precondition_unmet(tmp_path, monkeypatch):
    # Force local-only resolution (no central store) so the bare DB is the target.
    monkeypatch.setattr(bts, "resolve_central_data_dir", None)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    sqlite3.connect(str(state_dir / "runtime_coordination.db")).close()

    marker = bts._reconcile_tracks_fresh(state_dir, PROJECT_ID)
    assert marker["derived_refreshed"] is False
    assert marker["reason"] == "precondition_unmet"


def test_drift_is_surfaced_when_pr_merged_but_phase_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(bts, "resolve_central_data_dir", None)  # use state_dir directly
    state_dir = _build_tracks_db(tmp_path)
    _seed_track(state_dir, "T-stale", phase="active", pr_ref="PR-900")
    _seed_dispatch(state_dir, "D-1", "T-stale", state="completed", pr_ref="PR-900")
    _seed_pr_merged_event(state_dir, "D-1")

    marker = bts._reconcile_tracks_fresh(state_dir, PROJECT_ID)

    assert marker["derived_refreshed"] is True
    assert marker["tracks"] >= 1
    assert marker["drifted"] >= 1
    stale = next(d for d in marker["drifted_tracks"] if d["track_id"] == "T-stale")
    assert stale["declared_phase"] == "active"
    assert stale["derived_status"] == "done"


def test_in_sync_track_does_not_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(bts, "resolve_central_data_dir", None)
    state_dir = _build_tracks_db(tmp_path)
    _seed_track(state_dir, "T-sync", phase="queued")

    marker = bts._reconcile_tracks_fresh(state_dir, PROJECT_ID)
    assert marker["derived_refreshed"] is True
    assert all(d["track_id"] != "T-sync" for d in marker["drifted_tracks"])


def test_reconcile_resolves_central_store_not_ambient(tmp_path, monkeypatch):
    # Central holds the tracks; the ambient state_dir is an empty local store.
    central = _build_tracks_db(tmp_path / "central_root")
    _seed_track(central, "T-central", phase="queued")
    ambient = tmp_path / "ambient" / "state"
    ambient.mkdir(parents=True)
    sqlite3.connect(str(ambient / "runtime_coordination.db")).close()

    # resolve_central_data_dir(project) -> the central root that holds /state
    monkeypatch.setattr(bts, "resolve_central_data_dir", lambda pid: central.parent)

    assert bts._resolve_tracks_store(ambient, PROJECT_ID) == central
    marker = bts._reconcile_tracks_fresh(ambient, PROJECT_ID)
    assert marker["derived_refreshed"] is True  # found the central tracks, not the empty ambient
    assert marker["store"] == str(central)


# ---------------------------------------------------------------------------
# Index summary
# ---------------------------------------------------------------------------

def test_index_summary_is_compact_and_mirrors_marker():
    full = {
        "derived_refreshed": True, "tracks": 47, "drifted": 18,
        "drifted_tracks": [{"track_id": "x"}], "reason": None, "seconds": 0.4, "store": "/s",
        "last_reconcile_at": "2026-08-01T08:00:00Z", "last_reconcile_age_hours": 2.0,
        "gh_health": "ok", "nominated_not_closed": 0,
        "autoclose_degraded": False, "autoclose_reason": "ok",
    }
    summary = bts._track_freshness_summary(full)
    assert summary == {
        "derived_refreshed": True, "drifted": 18, "tracks": 47, "reason": None,
        "autoclose_degraded": False, "autoclose_reason": "ok",
    }
    assert "drifted_tracks" not in summary  # heavy detail excluded from the index

    none = bts._track_freshness_summary(None)
    assert none == {
        "derived_refreshed": False, "drifted": 0, "tracks": 0, "reason": None,
        "autoclose_degraded": False, "autoclose_reason": None,
    }


# ---------------------------------------------------------------------------
# Auto-close reconciler health (from reconcile_summary.json)
# ---------------------------------------------------------------------------

def test_autoclose_degraded_reflects_summary_health(tmp_path):
    """gh absent in the summary → autoclose_degraded True; a healthy summary → False.

    Red on old code: the auto-close health block did not exist, so no
    autoclose_degraded field was ever produced.
    """
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)

    state_dir = tmp_path / "degraded"
    state_dir.mkdir(parents=True)
    (state_dir / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "2026-07-31T12:45:55Z",
        "evidence_source_health": {"gh": "absent"},
        "counts": {"nominated": 8, "closed": 0, "unverified": 8},
    }), encoding="utf-8")
    degraded = bts._autoclose_health(state_dir, now=now)
    assert degraded["autoclose_degraded"] is True
    assert degraded["gh_health"] == "absent"
    assert degraded["nominated_not_closed"] == 8
    assert degraded["autoclose_reason"] == "gh_absent"

    state_dir = tmp_path / "healthy"
    state_dir.mkdir(parents=True)
    (state_dir / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "2026-08-01T08:00:00Z",
        "evidence_source_health": {"gh": "ok"},
        "counts": {"nominated": 2, "closed": 0, "unverified": 0},
    }), encoding="utf-8")
    healthy = bts._autoclose_health(state_dir, now=now)
    assert healthy["autoclose_degraded"] is False
    assert healthy["gh_health"] == "ok"
    assert healthy["nominated_not_closed"] == 2
    assert healthy["autoclose_reason"] == "ok"


def test_autoclose_degraded_true_when_summary_missing(tmp_path):
    """A missing reconcile_summary.json is itself a degraded signal, never omitted."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    health = bts._autoclose_health(
        state_dir, now=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    )
    assert health["autoclose_degraded"] is True
    assert health["autoclose_reason"] == "no_reconcile_summary"
    assert health["last_reconcile_at"] is None


def test_autoclose_degraded_true_when_last_run_stale(tmp_path):
    """A healthy-but-old summary (older than the 24 h threshold) is degraded."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    # 2026-07-31T08:00Z -> 2026-08-01T10:00Z = 26 h > _AUTOCLOSE_STALE_HOURS (24).
    (state_dir / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "2026-07-31T08:00:00Z",
        "evidence_source_health": {"gh": "ok"},
        "counts": {"nominated": 0, "closed": 0, "unverified": 0},
    }), encoding="utf-8")
    health = bts._autoclose_health(
        state_dir, now=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    )
    assert health["autoclose_degraded"] is True
    assert health["autoclose_reason"] == "stale"


def test_autoclose_degraded_when_finished_at_indeterminate(tmp_path):
    """Missing or unparseable finished_at → degraded with reason=indeterminate_age.

    Red on old code: age_hours=None → stale=False, and with gh=ok and no stuck
    backlog the probe returned autoclose_degraded=False, reason=ok — falsely
    healthy when the last-run age cannot be determined.
    """
    now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)

    # Unparseable finished_at (present but not valid ISO).
    state_dir = tmp_path / "unparseable"
    state_dir.mkdir(parents=True)
    (state_dir / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "not-a-valid-iso-timestamp",
        "evidence_source_health": {"gh": "ok"},
        "counts": {"nominated": 0, "closed": 0, "unverified": 0},
    }), encoding="utf-8")
    health = bts._autoclose_health(state_dir, now=now)
    assert health["autoclose_degraded"] is True
    assert health["autoclose_reason"] == "indeterminate_age"
    assert health["last_reconcile_age_hours"] is None

    # Missing finished_at entirely.
    state_dir2 = tmp_path / "missing_ts"
    state_dir2.mkdir(parents=True)
    (state_dir2 / "reconcile_summary.json").write_text(json.dumps({
        "evidence_source_health": {"gh": "ok"},
        "counts": {"nominated": 0, "closed": 0, "unverified": 0},
    }), encoding="utf-8")
    health2 = bts._autoclose_health(state_dir2, now=now)
    assert health2["autoclose_degraded"] is True
    assert health2["autoclose_reason"] == "indeterminate_age"
    assert health2["last_reconcile_age_hours"] is None


def test_autoclose_degraded_when_counts_malformed(tmp_path):
    """Non-numeric counts in a valid JSON → degraded with reason=malformed_counts.

    Red on old code: int('not-a-number') raised ValueError outside the try/except
    that only guards json.loads.  A list counts block raised AttributeError.
    """
    now = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)

    # Non-numeric string value inside counts.
    state_dir = tmp_path / "bad_string"
    state_dir.mkdir(parents=True)
    (state_dir / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "2026-08-04T08:00:00Z",
        "evidence_source_health": {"gh": "ok"},
        "counts": {"nominated": "not-a-number", "closed": 0, "unverified": 0},
    }), encoding="utf-8")
    health = bts._autoclose_health(state_dir, now=now)
    assert health["autoclose_degraded"] is True
    assert health["autoclose_reason"] == "malformed_counts"
    assert health["nominated_not_closed"] is None
    # Timestamp info should still be propagated even when counts are malformed.
    assert health["last_reconcile_at"] == "2026-08-04T08:00:00Z"
    assert health["last_reconcile_age_hours"] == 2.0

    # Counts is a list instead of a dict.
    state_dir2 = tmp_path / "bad_list"
    state_dir2.mkdir(parents=True)
    (state_dir2 / "reconcile_summary.json").write_text(json.dumps({
        "finished_at": "2026-08-04T08:00:00Z",
        "evidence_source_health": {"gh": "ok"},
        "counts": [1, 2, 3],
    }), encoding="utf-8")
    health2 = bts._autoclose_health(state_dir2, now=now)
    assert health2["autoclose_degraded"] is True
    assert health2["autoclose_reason"] == "malformed_counts"


# ---------------------------------------------------------------------------
# SessionStart hook command (F5: a quoting bug here breaks every session start)
# ---------------------------------------------------------------------------

def _t0_sessionstart_command() -> str:
    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for entry in settings["hooks"]["SessionStart"]:
        if entry.get("matcher") == "terminals/T0":
            return entry["hooks"][0]["command"]
    raise AssertionError("T0 SessionStart hook not found")


def _t0_sessionstart_wrapper() -> str:
    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for entry in settings["hooks"]["SessionStart"]:
        if entry.get("matcher") == "terminals/T0":
            command = entry["hooks"][0]["command"]
            body = shlex.split(command)[2]
            match = re.search(r"scripts/hooks/([\w._-]+\.sh)", body)
            if not match:
                raise AssertionError(f"T0 SessionStart hook does not delegate to a wrapper: {command}")
            return (_ROOT / "scripts" / "hooks" / match.group(1)).read_text(encoding="utf-8")
    raise AssertionError("T0 SessionStart hook not found")


def test_sessionstart_hook_is_valid_bash_and_de_swallows():
    cmd = _t0_sessionstart_command()
    # 1) the whole command tokenizes (no unbalanced quotes)
    shlex.split(cmd)
    # 2) the inner bash -c body parses (catches quoting/escaping bugs)
    assert cmd.startswith("bash -c ")
    body = shlex.split(cmd)[2]
    rc = subprocess.run(["bash", "-n", "-c", body], capture_output=True, text=True)
    assert rc.returncode == 0, f"hook body is not valid bash: {rc.stderr}"
    # 3) the hook delegates to a repo wrapper script that is itself valid bash.
    wrapper = _t0_sessionstart_wrapper()
    rc = subprocess.run(["bash", "-n", "-c", wrapper], capture_output=True, text=True)
    assert rc.returncode == 0, f"wrapper is not valid bash: {rc.stderr}"
    # 4) the BUILDER's stderr is captured to a log (not /dev/null), the output
    #    lands in the CENTRAL store via the resolved $STATE (not the repo-local
    #    .vnx-data split-brain, OI-859), an explicit interpreter is used, and
    #    the hook never blocks.
    assert 'build_t0_state.py" --output "$STATE/t0_state.json"' in wrapper
    assert '"$ROOT/.vnx-data/state/t0_state.json"' not in wrapper
    assert '2>"$LOG/build_t0_state.err"' in wrapper
    assert "build_t0_state.err" in wrapper
    assert "exit 0" in wrapper
    assert ".venv/bin/python" in wrapper or "python3.12" in wrapper
