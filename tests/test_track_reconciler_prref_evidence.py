"""tests/test_track_reconciler_prref_evidence.py — pr_ref+merged evidence path tests.

Verifies the additive pr_ref evidence path added in feat/reconciler-prref-evidence:

- A track with pr_ref + merged evidence derives 'done' with ZERO matching dispatches
- Zero-dispatch tracks without merged-PR evidence defer to declared phase
- Idempotent re-run of the pr_ref path
- Idempotent re-run of the declared-phase fallback
- Determinism: two runs over identical state produce identical derived_status
- Dispatch-based path not regressed (tracks WITH matching dispatches still use it)
- _load_merged_pr_numbers reads from pr_merged.ndjson (NDJSON source)
- _load_merged_pr_numbers reads from ROADMAP.yaml (YAML source)
- _load_merged_pr_numbers returns empty frozenset when no files exist (graceful)
- _parse_pr_number handles '#N', 'N', None, and malformed inputs
"""

from __future__ import annotations

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

import schema_migration
import track_reconciler
from track_reconciler import _load_merged_pr_numbers, _parse_pr_number
import tracks as tracks_lib


PROJECT_ID = "test-ev-proj"


# ---------------------------------------------------------------------------
# DB helpers (same pattern as test_track_reconciler.py)
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path, *, deep: bool = False) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 + 0028 applied.

    deep=True uses tmp_path/.vnx-data/state so that state_dir.parent.parent == tmp_path,
    matching the real project structure and allowing ROADMAP.yaml at tmp_path/ROADMAP.yaml.
    """
    if deep:
        state_dir = tmp_path / ".vnx-data" / "state"
    else:
        state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch',
            entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id TEXT
        )
    """)
    conn.commit()

    for ver, fname in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()
    return state_dir


def _seed_track(state_dir: Path, track_id: str, *, phase: str = "active", pr_ref: str | None = None) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID,
        title=f"Track {track_id}",
        goal_state=f"ship {track_id}",
        phase=phase,
        pr_ref=pr_ref,
    )


def _seed_dispatch(state_dir: Path, dispatch_id: str, track_id: str, *, state: str = "completed") -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id),
    )
    conn.commit()
    conn.close()


def _get_derived(state_dir: Path, track_id: str) -> str | None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row["derived_status"] if row else None


# ---------------------------------------------------------------------------
# _parse_pr_number
# ---------------------------------------------------------------------------

def test_parse_pr_number_hash_prefix():
    assert _parse_pr_number("#756") == 756


def test_parse_pr_number_bare_number():
    assert _parse_pr_number("801") == 801


def test_parse_pr_number_none():
    assert _parse_pr_number(None) is None


def test_parse_pr_number_empty():
    assert _parse_pr_number("") is None


def test_parse_pr_number_malformed():
    assert _parse_pr_number("PR-FUT-1") is None


def test_parse_pr_number_whitespace():
    assert _parse_pr_number("  #42  ") == 42


# ---------------------------------------------------------------------------
# _load_merged_pr_numbers
# ---------------------------------------------------------------------------

def test_load_merged_empty_when_no_files(tmp_path):
    """Returns empty frozenset when no NDJSON or ROADMAP.yaml exist."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    result = _load_merged_pr_numbers(state_dir)
    assert result == frozenset()


def test_load_merged_from_pr_merged_ndjson(tmp_path):
    """Reads pr_number from pr_merged.ndjson."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    ndjson = events_dir / "pr_merged.ndjson"
    ndjson.write_text(
        json.dumps({"event_type": "pr_merged", "pr_number": 756}) + "\n"
        + json.dumps({"event_type": "pr_merged", "pr_number": 759}) + "\n"
        + json.dumps({"event_type": "other", "pr_number": 999}) + "\n",
        encoding="utf-8",
    )
    result = _load_merged_pr_numbers(state_dir)
    assert 756 in result
    assert 759 in result
    assert 999 not in result


def test_load_merged_from_roadmap_yaml(tmp_path):
    """Reads pr_queue[*].status=merged entries from ROADMAP.yaml."""
    # state_dir must be two levels deep so state_dir.parent.parent == tmp_path
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    roadmap = tmp_path / "ROADMAP.yaml"
    roadmap.write_text(
        "features:\n"
        "  - feature_id: feat-a\n"
        "    pr_queue:\n"
        "      - pr_id: '#800'\n"
        "        status: merged\n"
        "      - pr_id: '#801'\n"
        "        status: open\n"
        "  - feature_id: feat-b\n"
        "    pr_queue:\n"
        "      - pr_id: '#802'\n"
        "        status: merged\n",
        encoding="utf-8",
    )
    # repo_root points Source-3 at the test-controlled ROADMAP.yaml deterministically
    # (independent of the CWD git-root), which is the project repo root.
    result = _load_merged_pr_numbers(state_dir, repo_root=tmp_path)
    assert 800 in result
    assert 802 in result
    assert 801 not in result  # status=open, not merged


def test_load_merged_combines_sources(tmp_path):
    """Merges PR numbers from both NDJSON and ROADMAP.yaml."""
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    events_dir = tmp_path / ".vnx-data" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "pr_merged.ndjson").write_text(
        json.dumps({"event_type": "pr_merged", "pr_number": 676}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ROADMAP.yaml").write_text(
        "features:\n  - feature_id: f\n    pr_queue:\n      - pr_id: '#757'\n        status: merged\n",
        encoding="utf-8",
    )
    result = _load_merged_pr_numbers(state_dir, repo_root=tmp_path)
    assert 676 in result
    assert 757 in result


def test_load_merged_graceful_on_bad_ndjson(tmp_path):
    """Skips malformed NDJSON lines without raising."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "pr_merged.ndjson").write_text(
        "NOT JSON\n"
        + json.dumps({"event_type": "pr_merged", "pr_number": 500}) + "\n",
        encoding="utf-8",
    )
    result = _load_merged_pr_numbers(state_dir)
    assert 500 in result  # valid line still parsed


# ---------------------------------------------------------------------------
# Core evidence path: zero-dispatch tracks
# ---------------------------------------------------------------------------

def test_done_when_pr_ref_merged_zero_dispatches(tmp_path):
    """The existing pr_ref + merged evidence path still derives 'done'."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-ev-done", pr_ref="#756")

    result = track_reconciler.reconcile_track(
        state_dir, "T-ev-done", PROJECT_ID,
        _merged_pr_numbers=frozenset({756}),
    )
    assert result["derived_status"] == "done"
    assert _get_derived(state_dir, "T-ev-done") == "done"


def test_done_when_pr_ref_merged_with_terminal_dispatches(tmp_path):
    """all-terminal-dispatch path derives 'done' only when ALL PRs in pr_ref are merged."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-ev-term", pr_ref="#908,#909")
    _seed_dispatch(state_dir, "d-term-1", "T-ev-term", state="completed")

    # Both PRs merged → done.
    result = track_reconciler.reconcile_track(
        state_dir, "T-ev-term", PROJECT_ID,
        _merged_pr_numbers=frozenset({908, 909}),
    )
    assert result["derived_status"] == "done"

    # Only one PR merged → in_progress (ALL-merged rule; was 'done' before this change).
    result_partial = track_reconciler.reconcile_track(
        state_dir, "T-ev-term", PROJECT_ID,
        _merged_pr_numbers=frozenset({909}),  # only the 2nd PR of the list merged
    )
    assert result_partial["derived_status"] == "in_progress"


def test_done_when_declared_done_without_evidence(tmp_path):
    """A done track with no dispatch or merged-PR evidence stays done."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase-done", phase="done", pr_ref=None)

    result = track_reconciler.reconcile_track(
        state_dir, "T-phase-done", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    assert result["derived_status"] == "done"
    assert _get_derived(state_dir, "T-phase-done") == "done"


def test_in_progress_when_declared_active_without_evidence(tmp_path):
    """An active track with no dispatch or merged-PR evidence stays in progress."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase-active", phase="active", pr_ref=None)

    result = track_reconciler.reconcile_track(
        state_dir, "T-phase-active", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    assert result["derived_status"] == "in_progress"


def test_queued_when_declared_queued_without_evidence(tmp_path):
    """A queued track with no dispatch or merged-PR evidence stays queued."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase-queued", phase="queued", pr_ref=None)

    result = track_reconciler.reconcile_track(
        state_dir, "T-phase-queued", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    assert result["derived_status"] == "queued"


def test_in_progress_when_pr_ref_not_in_merged_set(tmp_path):
    """An active track with unconfirmed pr_ref and no dispatches stays in progress."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-ev-unmerged", phase="active", pr_ref="#999")

    result = track_reconciler.reconcile_track(
        state_dir, "T-ev-unmerged", PROJECT_ID,
        _merged_pr_numbers=frozenset({756}),  # 999 not in set
    )
    assert result["derived_status"] == "in_progress"


# ---------------------------------------------------------------------------
# Idempotency and determinism
# ---------------------------------------------------------------------------

def test_idempotent_prref_evidence_path(tmp_path):
    """Running reconcile_track twice produces the same result for the pr_ref path."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-ev-idem", pr_ref="#800")

    r1 = track_reconciler.reconcile_track(
        state_dir, "T-ev-idem", PROJECT_ID,
        _merged_pr_numbers=frozenset({800}),
    )
    r2 = track_reconciler.reconcile_track(
        state_dir, "T-ev-idem", PROJECT_ID,
        _merged_pr_numbers=frozenset({800}),
    )
    assert r1["derived_status"] == "done"
    assert r2["derived_status"] == "done"


def test_idempotent_done_phase_without_evidence(tmp_path):
    """The declared-done fallback remains done across repeated reconciles."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase-done-idem", phase="done", pr_ref=None)

    r1 = track_reconciler.reconcile_track(
        state_dir, "T-phase-done-idem", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    r2 = track_reconciler.reconcile_track(
        state_dir, "T-phase-done-idem", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    assert r1["derived_status"] == "done"
    assert r2["derived_status"] == "done"


def test_determinism_same_state_same_result(tmp_path):
    """Two reconcile calls with identical DB state produce identical derived_status."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-det-1", pr_ref="#750")
    _seed_track(state_dir, "T-det-2", pr_ref="#751")

    merged = frozenset({750, 751})
    r_a1 = track_reconciler.reconcile_track(state_dir, "T-det-1", PROJECT_ID, _merged_pr_numbers=merged)
    r_a2 = track_reconciler.reconcile_track(state_dir, "T-det-1", PROJECT_ID, _merged_pr_numbers=merged)
    r_b1 = track_reconciler.reconcile_track(state_dir, "T-det-2", PROJECT_ID, _merged_pr_numbers=merged)
    r_b2 = track_reconciler.reconcile_track(state_dir, "T-det-2", PROJECT_ID, _merged_pr_numbers=merged)

    assert r_a1["derived_status"] == r_a2["derived_status"] == "done"
    assert r_b1["derived_status"] == r_b2["derived_status"] == "done"


# ---------------------------------------------------------------------------
# No regression: dispatch-based path still works
# ---------------------------------------------------------------------------

def test_dispatch_path_not_regressed_active_dispatch(tmp_path):
    """A track WITH an active dispatch still uses the dispatch path (in_progress)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-reg-active", pr_ref="#900")
    _seed_dispatch(state_dir, "D-reg-1", "T-reg-active", state="running")

    result = track_reconciler.reconcile_track(
        state_dir, "T-reg-active", PROJECT_ID,
        _merged_pr_numbers=frozenset({900}),  # PR in merged set, but dispatch active
    )
    # Dispatch active → in_progress (dispatch path wins when dispatches exist)
    assert result["derived_status"] == "in_progress"


def test_dispatch_path_not_regressed_terminal_no_pr_ref(tmp_path):
    """A track with terminal dispatch and no pr_ref still derives 'done' via dispatch path."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-reg-nopr", pr_ref=None)
    _seed_dispatch(state_dir, "D-reg-2", "T-reg-nopr", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-reg-nopr", PROJECT_ID,
        _merged_pr_numbers=frozenset(),
    )
    assert result["derived_status"] == "done"


def test_dispatch_path_not_regressed_in_progress_pr(tmp_path):
    """A track with terminal dispatch + pr_ref but no merged event stays 'in_progress'."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-reg-inprog", pr_ref="#910")
    _seed_dispatch(state_dir, "D-reg-3", "T-reg-inprog", state="completed")

    # PR 910 NOT in merged set; no coordination_event → in_progress
    result = track_reconciler.reconcile_track(
        state_dir, "T-reg-inprog", PROJECT_ID,
        _merged_pr_numbers=frozenset({756}),  # 910 not present
    )
    assert result["derived_status"] == "in_progress"


# ---------------------------------------------------------------------------
# reconcile_all_tracks with evidence
# ---------------------------------------------------------------------------

def test_reconcile_all_tracks_prref_evidence(tmp_path):
    """reconcile_all_tracks correctly applies pr_ref evidence to all tracks."""
    state_dir = _build_db(tmp_path, deep=True)
    # Track with pr_ref + merged: should derive 'done'
    _seed_track(state_dir, "T-all-a", pr_ref="#756")
    # Active track with pr_ref, not merged: defer to phase.
    _seed_track(state_dir, "T-all-b", pr_ref="#999")
    # Active track with no pr_ref or dispatches: defer to phase.
    _seed_track(state_dir, "T-all-c", pr_ref=None)
    # Track with dispatch (no pr_ref match): dispatch path
    _seed_track(state_dir, "T-all-d", pr_ref=None)
    _seed_dispatch(state_dir, "D-all-d", "T-all-d", state="running")

    # ROADMAP.yaml at the project repo root; repo_root threads it into Source-3
    # deterministically (independent of the CWD git-root).
    (tmp_path / "ROADMAP.yaml").write_text(
        "features:\n  - feature_id: f\n    pr_queue:\n      - pr_id: '#756'\n        status: merged\n",
        encoding="utf-8",
    )

    results = track_reconciler.reconcile_all_tracks(state_dir, PROJECT_ID, repo_root=tmp_path)
    by_id = {r["track_id"]: r for r in results}

    assert by_id["T-all-a"]["derived_status"] == "done"
    assert by_id["T-all-b"]["derived_status"] == "in_progress"
    assert by_id["T-all-c"]["derived_status"] == "in_progress"
    assert by_id["T-all-d"]["derived_status"] == "in_progress"


# ---------------------------------------------------------------------------
# authoritative phase never modified
# ---------------------------------------------------------------------------

def test_phase_never_modified_by_prref_path(tmp_path):
    """The reconciler must not touch tracks.phase even via the pr_ref evidence path."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-phase-guard", phase="done", pr_ref="#756")

    before_phase = "done"
    track_reconciler.reconcile_track(
        state_dir, "T-phase-guard", PROJECT_ID,
        _merged_pr_numbers=frozenset({756}),
    )
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT phase, derived_status FROM tracks WHERE track_id=? AND project_id=?",
        ("T-phase-guard", PROJECT_ID),
    ).fetchone()
    conn.close()
    assert row["phase"] == before_phase
    assert row["derived_status"] == "done"


# ---------------------------------------------------------------------------
# D-RECON (2026-06-26): git-grounded merged-PR source + multi-PR pr_ref
# ---------------------------------------------------------------------------

def test_parse_pr_numbers_handles_comma_list():
    # A track that landed across multiple PRs: '#908,#909' -> {908, 909}.
    assert track_reconciler._parse_pr_numbers("#908,#909") == frozenset({908, 909})
    assert track_reconciler._parse_pr_numbers("908 909") == frozenset({908, 909})
    assert track_reconciler._parse_pr_numbers("#911") == frozenset({911})
    assert track_reconciler._parse_pr_numbers(None) == frozenset()
    assert track_reconciler._parse_pr_numbers("not-a-pr") == frozenset()


def test_gh_source_off_by_default(tmp_path, monkeypatch):
    # Without VNX_RECONCILE_GIT, source 4 must not run (no gh, deterministic).
    monkeypatch.delenv("VNX_RECONCILE_GIT", raising=False)
    called = {"gh": False}
    monkeypatch.setattr(track_reconciler, "_load_merged_prs_from_gh",
                        lambda *a, **k: called.__setitem__("gh", True) or frozenset())
    state = tmp_path / "state"; state.mkdir()
    _load_merged_pr_numbers(state)
    assert called["gh"] is False


def test_gh_source_used_when_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_RECONCILE_GIT", "1")
    monkeypatch.setattr(track_reconciler, "_load_merged_prs_from_gh", lambda *a, **k: frozenset({911, 912}))
    state = tmp_path / "state"; state.mkdir()
    assert {911, 912} <= set(_load_merged_pr_numbers(state))


def test_gh_cache_first_no_network_on_fresh_cache(tmp_path, monkeypatch):
    # A fresh cache file is honoured without shelling out to gh.
    import time as _t
    state = tmp_path / "state"; state.mkdir()
    (state / "pr_merged_cache.json").write_text(
        json.dumps({"ts": _t.time(), "numbers": [911, 912]}), encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("gh must NOT be called when the cache is fresh")
    monkeypatch.setattr(track_reconciler.subprocess, "run", _boom)
    assert track_reconciler._load_merged_prs_from_gh(state) == frozenset({911, 912})


def test_gh_silent_on_failure(tmp_path, monkeypatch):
    # gh absent/failing -> empty set, never raises (offline-safe contract).
    state = tmp_path / "state"; state.mkdir()
    monkeypatch.setattr(track_reconciler.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gh")))
    assert track_reconciler._load_merged_prs_from_gh(state) == frozenset()


# ---------------------------------------------------------------------------
# D1: ALL-merged multi-PR derivation + declared-done stability
# ---------------------------------------------------------------------------

def test_single_pr_merged_done_no_dispatch(tmp_path):
    """Single PR in pr_ref — merged → done (unchanged single-PR behaviour, no dispatch)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-single-nd", pr_ref="#800")

    result = track_reconciler.reconcile_track(
        state_dir, "T-single-nd", PROJECT_ID,
        _merged_pr_numbers=frozenset({800}),
    )
    assert result["derived_status"] == "done"


def test_single_pr_merged_done_terminal_dispatch(tmp_path):
    """Single PR in pr_ref — merged → done (unchanged single-PR behaviour, terminal dispatch)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-single-td", pr_ref="#800")
    _seed_dispatch(state_dir, "D-single-td-1", "T-single-td", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-single-td", PROJECT_ID,
        _merged_pr_numbers=frozenset({800}),
    )
    assert result["derived_status"] == "done"


def test_multi_pr_partial_merge_no_dispatch_not_done(tmp_path):
    """Multi-PR '#908,#909', only #908 merged, no dispatches → NOT done (ALL-merged rule)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-multi-nd", pr_ref="#908,#909")

    result = track_reconciler.reconcile_track(
        state_dir, "T-multi-nd", PROJECT_ID,
        _merged_pr_numbers=frozenset({908}),  # only first PR merged
    )
    assert result["derived_status"] != "done"


def test_multi_pr_partial_merge_terminal_dispatch_not_done(tmp_path):
    """Multi-PR '#908,#909', only #908 merged, terminal dispatch, no event → NOT done."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-multi-td", pr_ref="#908,#909")
    _seed_dispatch(state_dir, "D-multi-td-1", "T-multi-td", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-multi-td", PROJECT_ID,
        _merged_pr_numbers=frozenset({908}),  # only first PR merged
    )
    # Was 'done' before this change (ANY-merged); now 'in_progress' (ALL-merged rule).
    assert result["derived_status"] == "in_progress"


def test_multi_pr_all_merged_done_no_dispatch(tmp_path):
    """Multi-PR '#908,#909', both merged, no dispatch → done."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-multi-all-nd", pr_ref="#908,#909")

    result = track_reconciler.reconcile_track(
        state_dir, "T-multi-all-nd", PROJECT_ID,
        _merged_pr_numbers=frozenset({908, 909}),
    )
    assert result["derived_status"] == "done"


def test_multi_pr_all_merged_done_terminal_dispatch(tmp_path):
    """Multi-PR '#908,#909', both merged, terminal dispatch → done."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-multi-all-td", pr_ref="#908,#909")
    _seed_dispatch(state_dir, "D-multi-all-td-1", "T-multi-all-td", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-multi-all-td", PROJECT_ID,
        _merged_pr_numbers=frozenset({908, 909}),
    )
    assert result["derived_status"] == "done"


def test_declared_done_terminal_partial_merge_no_event_done(tmp_path):
    """Declared-done + terminal dispatch + partial multi-PR merge + no pr_merged event → done.

    This is the phantom-drift regression introduced by the ALL-merged change: without the
    declared-done short-circuit in the all-terminal branch, this would return 'in_progress'.
    """
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-decl-stab", phase="done", pr_ref="#908,#909")
    _seed_dispatch(state_dir, "D-decl-stab-1", "T-decl-stab", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-decl-stab", PROJECT_ID,
        _merged_pr_numbers=frozenset({908}),  # partial merge, no pr_merged event
    )
    assert result["derived_status"] == "done"


def test_declared_done_no_dispatch_partial_merge_done(tmp_path):
    """Declared-done, no dispatches, partial multi-PR merge → done (existing fallback)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-decl-nd", phase="done", pr_ref="#908,#909")

    result = track_reconciler.reconcile_track(
        state_dir, "T-decl-nd", PROJECT_ID,
        _merged_pr_numbers=frozenset({908}),  # partial merge
    )
    assert result["derived_status"] == "done"


def test_unparseable_pr_ref_not_done_no_dispatch(tmp_path):
    """Unparseable pr_ref produces an empty set → does not derive done via the pr_ref path."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-unparse-nd", phase="active", pr_ref="PR-FUT-99")

    result = track_reconciler.reconcile_track(
        state_dir, "T-unparse-nd", PROJECT_ID,
        _merged_pr_numbers=frozenset({1, 2, 3}),
    )
    # Empty parse result cannot derive done via pr_ref path.
    assert result["derived_status"] != "done"


def test_unparseable_pr_ref_not_done_terminal_dispatch(tmp_path):
    """Unparseable pr_ref + terminal dispatch → in_progress (no pr_ref evidence path)."""
    state_dir = _build_db(tmp_path)
    _seed_track(state_dir, "T-unparse-td", phase="active", pr_ref="PR-FUT-99")
    _seed_dispatch(state_dir, "D-unparse-td-1", "T-unparse-td", state="completed")

    result = track_reconciler.reconcile_track(
        state_dir, "T-unparse-td", PROJECT_ID,
        _merged_pr_numbers=frozenset({1, 2, 3}),
    )
    assert result["derived_status"] == "in_progress"
