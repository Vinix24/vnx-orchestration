"""Tests for scripts/lib/migration_effectiveness_probe.py
(framework-status-audit-and-cockpit PR-7).

Builds GENUINE migration-walked DBs via ``migrate_future_system`` (the same
builder pattern as ``tests/test_fsr_version_reconciliation.py``) rather than
hand-rolling schema fixtures, so the probe is exercised against the real
migration surface, not a guess at its shape.

Dispatch-ID: 20260712-185712-cockpit-pr7
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import migrate_future_system as mfs  # noqa: E402
import schema_migration  # noqa: E402
from effectiveness_probe import EFFECTIVENESS_PROBES  # noqa: E402
from migration_effectiveness_probe import (  # noqa: E402
    COORDINATION_DB_FILENAME,
    MigrationEffectivenessProbe,
)

_V21_DISPATCHES = """
CREATE TABLE dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
    terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_after TEXT, metadata_json TEXT DEFAULT '{}',
    UNIQUE(dispatch_id, project_id)
)
"""

_APPLY_CHAIN = (
    (22, "apply_migration"), (24, "apply_migration_v24"), (27, "apply_migration_v27"),
    (28, "apply_migration_v28"), (29, "apply_migration_v29"), (30, "apply_migration_v30"),
    (31, "apply_migration_v31"),
)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    state = proj / ".vnx-data" / "state"
    state.mkdir(parents=True)
    db = state / COORDINATION_DB_FILENAME
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_V21_DISPATCHES)
    conn.commit()
    conn.close()
    return proj


def _state_dir(proj: Path) -> Path:
    return proj / ".vnx-data" / "state"


def _build_db_at(tmp_path: Path, target: int) -> Path:
    proj = _make_project(tmp_path)
    db = _state_dir(proj) / COORDINATION_DB_FILENAME
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    for ver, fname in _APPLY_CHAIN:
        if ver > target:
            break
        getattr(mfs, fname)(conn, proj)
        conn.commit()
    assert schema_migration.get_user_version(conn) == target
    conn.close()
    return proj


def test_registered_under_migration_mechanisms():
    assert EFFECTIVENESS_PROBES["migration-mechanisms"] is MigrationEffectivenessProbe


def test_unknown_when_db_absent(tmp_path):
    result = MigrationEffectivenessProbe(state_dir=tmp_path / "state").run()
    assert result.status == "unknown"
    assert result.detail["db_exists"] is False


def test_unknown_when_claimed_version_predates_manifest_floor(tmp_path):
    """A user_version of 0 (no migration ever walked) has no applicable manifest
    entry to validate against — nothing to measure, not a failure."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute("CREATE TABLE placeholder (x INTEGER)")
    conn.commit()
    conn.close()

    result = MigrationEffectivenessProbe(state_dir=state_dir).run()

    assert result.status == "unknown"
    assert result.detail["effective_version"] is None


def test_genuine_terminal_version_db_is_ok(tmp_path):
    proj = _build_db_at(tmp_path, 31)

    result = MigrationEffectivenessProbe(state_dir=_state_dir(proj)).run()

    assert result.status == "ok"
    assert result.detail["claimed_version"] == 31
    assert result.detail["violation_count"] == 0


def test_genuine_earlier_version_db_is_degraded_not_produces_crap(tmp_path):
    """A DB honestly at an earlier, internally-consistent version is behind, not
    broken — the manifest holds for its claimed (lower) version."""
    proj = _build_db_at(tmp_path, 27)

    result = MigrationEffectivenessProbe(state_dir=_state_dir(proj)).run()

    assert result.status == "degraded"
    assert result.detail["claimed_version"] == 27
    assert result.detail["violation_count"] == 0


def test_lying_user_version_is_produces_crap(tmp_path):
    """A DB physically at v27 that claims v31 fails the v31 invariant manifest —
    genuine drift/corruption, not a lag."""
    proj = _build_db_at(tmp_path, 27)
    db = _state_dir(proj) / COORDINATION_DB_FILENAME
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 31")
    conn.commit()
    conn.close()

    result = MigrationEffectivenessProbe(state_dir=_state_dir(proj)).run()

    assert result.status == "produces_crap"
    assert result.detail["violation_count"] > 0


def test_default_construction_resolves_real_state_dir_without_crashing():
    result = MigrationEffectivenessProbe().run()
    assert result.status in {"ok", "degraded", "produces_crap", "unknown"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
