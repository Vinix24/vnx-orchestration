"""PR-B fix-forward: build_t0_state auto_apply graceful-degrade (#863).

Root cause: migration 0022 rebuilds ``dispatches`` via
``INSERT INTO dispatches(... project_id ...) SELECT ... project_id ... FROM
dispatches_pre_v22``. On a legacy / pre-project_id DB whose ``PRAGMA
user_version`` diverged ahead of its actual schema (the project_id migrations
never ran against ``dispatches``), that SELECT raised
``no such column: project_id`` and the whole script rolled back. Combined with
PR-E's kanban honesty, a fresh / just-migrated environment (no terminal_state,
no receipts yet) was also folded into ``system_health: degraded`` → ``main()``
exit 1. A legacy DB that merely NEEDS migration was thus treated as degraded.

This module proves the corrected behavior:
  (a) a legacy DB is actually migrated (project_id added — ADR-007 tenant key)
      and ``build_t0_state`` runs CLEAN (exit 0, schema_version 2.1, healthy);
  (b) PR-E honesty is preserved — a genuinely corrupt DB still exits non-zero
      with health degraded/failed;
  (c) a malformed (non-string) receipt timestamp never crashes the build.

ADR-007: every central-DB table requires a composite UNIQUE/PK over project_id.
Migration 0022/0024 stamp ``dispatches`` with project_id and rebuild the track
tables with composite PRIMARY KEY (track_id, project_id); the self-heal here is
what lets that happen on a legacy DB.

Discipline: temp-DB ONLY. Every test pins VNX_DATA_DIR_EXPLICIT=1 + a tmp
VNX_DATA_DIR; the live ~/.vnx-data is never touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
_MIGRATIONS = _REPO_ROOT / "schemas" / "migrations"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402

# The in-process tests below drive the auto_apply lane (build_t0_state → auto_apply
# → apply_0022), which builds the dispatches composite UNIQUE inside migration 0022.
# Keep the migrate_future_system v22 preflight (leaked into the shared pytest process
# via collection-time imports) out of this lane. See conftest for the rationale.
pytestmark = pytest.mark.usefixtures("isolate_v22_composite_preflight")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _pin_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin VNX_DATA_DIR_EXPLICIT=1 + a tmp VNX_DATA_DIR; return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    return state_dir


def _make_legacy_db(state_dir: Path) -> None:
    """Write a faithful legacy / pre-project_id runtime_coordination.db.

    Reproduces the production divergence: ``dispatches`` has NO project_id
    column, the legacy ``runtime_schema_version`` tracker maxes at 14 (the
    0026 predecessor), yet ``PRAGMA user_version`` was bumped ahead to 20.
    Auto_apply must then run 0022/0024/0026 and self-heal project_id.
    """
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE runtime_schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            description TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, state) VALUES ('legacy-1', 'completed')"
    )
    for v in (12, 13, 14):
        conn.execute(
            "INSERT INTO runtime_schema_version (version, description) VALUES (?, ?)",
            (v, f"legacy v{v}"),
        )
    conn.execute("PRAGMA user_version = 20")  # diverged ahead of actual schema
    conn.commit()
    conn.close()


def _dispatches_has_project_id(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return any(r[1] == "project_id" for r in conn.execute("PRAGMA table_info(dispatches)"))
    finally:
        conn.close()


def _subprocess_env(tmp_path: Path) -> dict:
    """Env that isolates the build_t0_state subprocess to a tmp data dir."""
    env = dict(os.environ)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    env["VNX_DATA_DIR"] = str(tmp_path)
    env["VNX_STATE_DIR"] = str(tmp_path / "state")
    env["VNX_PROJECT_ID"] = "vnx-dev"
    return env


# ---------------------------------------------------------------------------
# (a) Legacy DB → actually migrated + clean run (the failing acceptance)
# ---------------------------------------------------------------------------

def test_legacy_db_is_migrated_and_runs_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy / pre-project_id DB migrates in place and build_t0_state is clean."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    _make_legacy_db(state_dir)
    db_path = state_dir / "runtime_coordination.db"
    assert not _dispatches_has_project_id(db_path), "precondition: legacy shape"
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    # Migration actually ran: project_id stamped (ADR-007 tenant key) + version advanced.
    assert _dispatches_has_project_id(db_path), "auto_apply must add dispatches.project_id"
    conn = sqlite3.connect(str(db_path))
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) >= 26
    finally:
        conn.close()

    # Clean run: valid 2.1 output, NOT degraded (legacy = recoverable, not corrupt).
    assert state["schema_version"] == "2.1"
    assert state["system_health"]["status"] == "healthy"
    # Pre-migration legacy row preserved through the dispatches rebuild.
    assert state["canonical_tracks"]["available"] is True


def test_legacy_db_main_exits_zero_subprocess(tmp_path: Path) -> None:
    """End-to-end: the CLI exits 0 with schema 2.1 on a legacy DB (acceptance)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _make_legacy_db(state_dir)
    out = tmp_path / "t0_state.json"

    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_t0_state.py"), "--output", str(out)],
        capture_output=True, text=True, env=_subprocess_env(tmp_path),
    )

    assert result.returncode == 0, (
        f"legacy DB must exit 0; got {result.returncode}\nstderr: {result.stderr[:500]}"
    )
    assert out.exists(), "output file must be written"
    assert json.loads(out.read_text())["schema_version"] == "2.1"


# ---------------------------------------------------------------------------
# (b) PR-E honesty preserved — genuinely corrupt DB still exits non-zero
# ---------------------------------------------------------------------------

def test_corrupt_db_still_exits_nonzero_subprocess(tmp_path: Path) -> None:
    """A malformed quality_intelligence.db → degraded → main() exits non-zero."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Seed terminal_state + receipts so the empty-env (recoverable) path is NOT the
    # cause; the ONLY degradation signal is the corrupt DB (PR-E R6.1 honesty).
    (state_dir / "terminal_state.json").write_text("{}")
    (state_dir / "t0_receipts.ndjson").write_text("")
    (state_dir / "quality_intelligence.db").write_bytes(b"NOT A VALID SQLITE DATABASE")
    out = tmp_path / "t0_state.json"

    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "build_t0_state.py"), "--output", str(out)],
        capture_output=True, text=True, env=_subprocess_env(tmp_path),
    )

    assert result.returncode != 0, "corrupt DB must NOT exit 0 (PR-E honesty)"
    data = json.loads(out.read_text())
    assert data["system_health"]["status"] in ("degraded", "failed")


def test_corrupt_db_inprocess_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process counterpart: corrupt DB → system_health degraded/failed, no crash."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    (state_dir / "t0_receipts.ndjson").write_text("")
    (state_dir / "quality_intelligence.db").write_bytes(b"NOT A VALID SQLITE DATABASE")
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    state = bts.build_t0_state(state_dir, dispatch_dir)

    assert state["system_health"]["status"] in ("degraded", "failed")


# ---------------------------------------------------------------------------
# Robustness — a non-string receipt timestamp must not crash the build
# ---------------------------------------------------------------------------

def test_build_queues_survives_int_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt carrying an integer timestamp must not crash _build_queues."""
    state_dir = _pin_isolation(tmp_path, monkeypatch)
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    receipt = {"event_type": "task_complete", "timestamp": 1718000000}
    (state_dir / "t0_receipts.ndjson").write_text(json.dumps(receipt) + "\n")

    # Must not raise AttributeError ('int' object has no attribute 'replace').
    result = bts._build_queues(dispatch_dir, state_dir)

    assert result["completed_last_hour"] == 0  # unparseable ts is skipped, not counted


# ---------------------------------------------------------------------------
# Migration-level — apply_0022 self-heals a legacy dispatches table
# ---------------------------------------------------------------------------

def test_apply_0022_self_heals_legacy_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_0022 adds dispatches.project_id before the rebuild on a legacy DB."""
    import importlib.util

    # B-N1: the self-heal resolves a VALIDATED tenant (never a silent 'vnx-dev'
    # default). The tmp DB is not on a canonical path and has no marker, so the
    # tenant comes from VNX_PROJECT_ID.
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _make_legacy_db(state_dir)
    db_path = state_dir / "runtime_coordination.db"

    spec = importlib.util.spec_from_file_location(
        "_apply_0022_test", _LIB_DIR / "migrations" / "apply_0022.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    applied = mod.apply_migration(db_path, _MIGRATIONS / "0022_track_layer.sql")

    assert applied is True
    assert _dispatches_has_project_id(db_path)
    # Idempotent: a second run is a clean skip (already at user_version >= 22).
    assert mod.apply_migration(db_path, _MIGRATIONS / "0022_track_layer.sql") is False
