"""tests/test_migrate_fk_order_real_data.py — FK ordering regression for PR-FUT-1 fix1.

Verifies that migrate_future_system.run() does not abort when the DB already
contains dispatches with non-null track values that were set before the tracks
table was populated (real v21 data scenario).

Option A (dispatch 20260528-fut-1-fix1-codex-r1):
- 0022 creates track tables + rebuilds dispatches WITHOUT FK
- tracks are seeded
- orphaned refs are nullified
- 0023 rebuilds dispatches WITH FK (safe — all track values now valid or NULL)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIB = _PROJECT_ROOT / "scripts" / "lib"
_SCRIPTS = _PROJECT_ROOT / "scripts"
_SCHEMAS = _PROJECT_ROOT / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


FIXTURE_ROADMAP = """\
# VNX Master Roadmap — FK order fixture

## 1. Feature-tracks

| Track | Goal | Doel-state |
|---|---|---|
| **track-01** | **15-juni Subscription Escape** | Alle Claude-workers default via tmux-leaseless lane. |
| **track-02** | **Public 1.0 Launch** | `pip install vnx-orchestration` werkend. |
| **track-03** | **Observability Activation** | Command-centre live, per-provider token-capture. |
| **track-04** | **Routing Hardening** | `provider_constraints.yaml` enforced. |
| **track-05** | **Governance Self-Monitoring** | Health-heartbeat-laag actief. |
| **track-06** | **Dimitri Adoption** | Track-layer + Context-composer-assembly. |

## 2. Phase — ACTIVE

(content elided)
"""


def _init_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    claudedocs = project_dir / "claudedocs"
    claudedocs.mkdir()

    (claudedocs / "VNX-MASTER-ROADMAP-2026-05-28.md").write_text(FIXTURE_ROADMAP, encoding="utf-8")

    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE coordination_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            event_type  TEXT,
            entity_type TEXT,
            entity_id   TEXT,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT,
            reason      TEXT,
            metadata_json TEXT,
            occurred_at TEXT,
            project_id  TEXT
        )
    """)
    conn.commit()
    conn.close()
    return project_dir


def _get_migrate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "migrate_future_system",
        _SCRIPTS / "migrate_future_system.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFKOrderRealData:
    def test_orphan_track_does_not_crash_migration(self, tmp_path):
        """Migration must complete without error when orphaned track ref exists."""
        project_dir = _init_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, track) "
            "VALUES ('disp-orphan', 'queued', 'track-99-orphan')"
        )
        conn.commit()
        conn.close()

        mod = _get_migrate_module()
        mod.run(project_dir)  # must not raise

    def test_orphan_track_nullified(self, tmp_path):
        """Option A behavior: orphaned track ref is nullified before FK enforcement."""
        project_dir = _init_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, track) "
            "VALUES ('disp-orphan', 'queued', 'track-99-orphan')"
        )
        conn.commit()
        conn.close()

        mod = _get_migrate_module()
        mod.run(project_dir)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = 'disp-orphan'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] is None, f"Expected track=NULL after nullification, got {row[0]!r}"

    def test_valid_track_preserved(self, tmp_path):
        """Dispatches with a valid PR prefix are tagged and FK is preserved."""
        project_dir = _init_project(tmp_path)
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state, pr_ref) "
            "VALUES ('disp-hyg-valid', 'queued', 'PR-HYG-1')"
        )
        conn.commit()
        conn.close()

        mod = _get_migrate_module()
        mod.run(project_dir)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT track FROM dispatches WHERE dispatch_id = 'disp-hyg-valid'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "track-02", f"Expected track-02 from PR-HYG- mapping, got {row[0]!r}"

    def test_schema_version_reaches_23(self, tmp_path):
        """Full migration run ends at user_version=23."""
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == 23

    def test_fk_enforced_after_migration(self, tmp_path):
        """After migration, inserting an invalid track FK raises IntegrityError."""
        project_dir = _init_project(tmp_path)
        mod = _get_migrate_module()
        mod.run(project_dir)

        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, state, track) "
                "VALUES ('disp-bad-fk', 'proposed', 'track-nonexistent')"
            )
            conn.commit()
        conn.close()
