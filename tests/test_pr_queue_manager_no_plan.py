"""tests/test_pr_queue_manager_no_plan.py — A6: pr_queue_manager without the plan parser.

A6 of the retire-feature-plan track removes the FEATURE_PLAN.md parser from
``scripts/pr_queue_manager.py``. ADR-047 makes the tracks DB the only plan
source, so these tests pin the three invariants that survive the removal:

1. PR readiness comes from ``track_dependencies`` in the tracks DB (the track of
   the PR's dispatch), not from a plan-filled queue entry;
2. the batch entry point is gone — ``init-feature`` is an unknown command;
3. the module imports and builds a manager in a project root with no
   FEATURE_PLAN.md at all.

Missing evidence (no tracks DB, no such track) is fail-closed: not ready, never
a silent pass.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for _p in (str(_ROOT / "tests"), str(_SCRIPTS), str(_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conftest import pin_vnx_data_dir

import schema_migration
import tracks as tracks_lib
from pr_queue_manager import PRQueueManager


PROJECT_ID = "test-proj-a6"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """A throwaway data dir + project root with no FEATURE_PLAN.md anywhere.

    ``pin_vnx_data_dir`` pins VNX_DATA_DIR *and* every subsystem dir derived
    from it, so PRQueueManager's real-store guard never sees the central store.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pin_vnx_data_dir(monkeypatch, data_dir)

    project_root = tmp_path / "project"
    project_root.mkdir()
    vnx_home = tmp_path / "vnx-home"
    vnx_home.mkdir()

    monkeypatch.setenv("VNX_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_HOME", str(vnx_home))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_ID)

    return {
        "data_dir": data_dir,
        "state_dir": Path(os.environ["VNX_STATE_DIR"]),
        "project_root": project_root,
    }


def _build_tracks_db(state_dir: Path) -> None:
    """Create a v24-era runtime_coordination.db (dispatches + tracks + deps)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
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
    for version, filename in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, version, (_MIGRATIONS / filename).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()


def _seed_two_tracks(state_dir: Path) -> None:
    """Track A (unfinished) and track B, where B depends on A with a hard edge."""
    tracks_lib.create_track(
        state_dir, "TRACK-A", PROJECT_ID,
        title="Track A", goal_state="ship the first track end to end",
        phase="active",
    )
    tracks_lib.create_track(
        state_dir, "TRACK-B", PROJECT_ID,
        title="Track B", goal_state="ship the second track end to end",
        phase="active",
    )
    tracks_lib.add_dependency(
        state_dir, "TRACK-B", PROJECT_ID, "TRACK-A", PROJECT_ID,
        kind="hard", derivation_source="manual",
    )


def _add_pr(manager: PRQueueManager, pr_id: str, track_id: str) -> None:
    manager.add_pr({
        "id": pr_id,
        "title": f"{pr_id} work",
        "size": 100,
        "dependencies": [],
        "skill": "@backend-developer",
        "track": track_id,
    })


# ---------------------------------------------------------------------------
# 1. readiness comes from track_dependencies
# ---------------------------------------------------------------------------


def test_pr_not_ready_while_its_track_dependency_is_unfinished(isolated_env):
    """B depends on unfinished A → the PR on B is not ready, and says why."""
    state_dir = isolated_env["state_dir"]
    _build_tracks_db(state_dir)
    _seed_two_tracks(state_dir)

    manager = PRQueueManager()
    _add_pr(manager, "PR-B", "TRACK-B")

    ready, reason = manager.check_pr_dependencies("PR-B")

    assert ready is False, "an unfinished hard dependency must block"
    assert "TRACK-A" in reason, f"reason must name the blocking track: {reason}"


def test_pr_ready_once_its_track_dependency_is_done(isolated_env):
    """Same PR becomes ready when track A reaches phase 'done'."""
    state_dir = isolated_env["state_dir"]
    _build_tracks_db(state_dir)
    _seed_two_tracks(state_dir)

    manager = PRQueueManager()
    _add_pr(manager, "PR-B", "TRACK-B")

    tracks_lib.transition_phase(
        state_dir, "TRACK-A", PROJECT_ID, "done",
        actor="T0", reason="track A shipped",
    )

    ready, reason = manager.check_pr_dependencies("PR-B")

    assert ready is True, f"a done dependency must not block: {reason}"


# ---------------------------------------------------------------------------
# 2. init-feature is gone
# ---------------------------------------------------------------------------


def test_init_feature_is_an_unknown_command(isolated_env):
    """The batch entry point is removed: the CLI rejects it as unknown.

    Exit code 10 is this script's ``unknown_command`` code
    (``PR_QUEUE_RESULT_EXIT_CODE_MAP``), asserted by
    tests/test_result_contract_as06.py.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / "pr_queue_manager.py"), "init-feature", "FEATURE_PLAN.md"],
        cwd=str(isolated_env["project_root"]),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 10, (result.returncode, result.stdout, result.stderr)
    assert "Unknown command: init-feature" in result.stdout


# ---------------------------------------------------------------------------
# 3. no FEATURE_PLAN.md needed to import or construct the manager
# ---------------------------------------------------------------------------


def test_imports_without_a_feature_plan(isolated_env):
    """A project root with no FEATURE_PLAN.md is a normal, working root."""
    project_root = isolated_env["project_root"]
    assert not (project_root / "FEATURE_PLAN.md").exists()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [_SCRIPTS.as_posix(), _LIB.as_posix(), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c",
         "import pr_queue_manager\n"
         "manager = pr_queue_manager.PRQueueManager()\n"
         "print('no-plan-ok', manager.get_next_pr())\n"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "no-plan-ok None" in result.stdout


# ---------------------------------------------------------------------------
# fail-closed evidence
# ---------------------------------------------------------------------------


def test_missing_tracks_db_is_not_ready(isolated_env):
    """No tracks DB → not ready, with the reason naming the missing evidence."""
    manager = PRQueueManager()
    _add_pr(manager, "PR-NO-DB", "TRACK-X")

    ready, reason = manager.check_pr_dependencies("PR-NO-DB")

    assert ready is False, "missing dependency evidence must never pass"
    assert "tracks DB" in reason, f"reason must name the missing store: {reason}"
