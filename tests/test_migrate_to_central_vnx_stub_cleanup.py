"""Regression test: a failed --fresh-central apply must not leave a
permanent 0-table quality_intelligence.db / runtime_coordination.db decoy
on disk.

Reproduces the real-world artifact found 2026-08-30: two empty
(0 bytes, 0 tables) quality_intelligence.db files at
``~/.vnx-data/vnx-dev/quality_intelligence.db`` and
``~/.vnx-data/quality_intelligence.db`` — both missing the ``state/``
path segment, 28 minutes apart on 2026-07-30. Root cause traced to
``_run_apply`` in scripts/migrate_to_central_vnx.py: on
``--fresh-central``, it lazy-creates central_qi/central_rc via
``sqlite3.connect(path).close()`` *before* the canonical bootstrap runs,
then only cleans up that stub in three specifically-caught exception
types (BootstrapFailure, sqlite3.Error, FileNotFoundError/ImportError) —
any other termination (an operator's wrong --central-state pointed at a
directory not meant to hold a fresh install, Ctrl-C, an unanticipated
exception) leaves the 0-table stub behind forever. A later reader that
only checks ``.exists()`` then treats that decoy as "no data yet" instead
of "wrong path" (OI: absence-is-loud D5).

Dispatch-ID: 20260830-090500-d5-drie-lege-databases
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402


def _make_minimal_source(state_dir: Path, project_id: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    qi = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    try:
        qi.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY, pattern_type TEXT NOT NULL,
                category TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, pattern_data TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY, dispatch_id TEXT UNIQUE,
                terminal TEXT, track TEXT, role TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        qi.commit()
    finally:
        qi.close()

    rc = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        rc.executescript(
            """
            CREATE TABLE runtime_schema_version (version INTEGER PRIMARY KEY, description TEXT);
            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY, state TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT INTO runtime_schema_version (version, description) VALUES (10, 'fixture');
            """
        )
        rc.commit()
    finally:
        rc.close()


def _build_registry(tmp_path: Path) -> tuple[Path, Path]:
    proj_dir = tmp_path / "proj-a"
    _make_minimal_source(proj_dir / ".vnx-data" / "state", "proj-a")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "schema_version": 1,
        "projects": [{"name": "proj-a", "path": str(proj_dir), "project_id": "proj-a"}],
    }))
    backup_base = tmp_path / "backups"
    backup_base.mkdir()
    return registry, backup_base


def test_failed_fresh_apply_removes_unpopulated_stub(tmp_path, monkeypatch):
    """Bootstrap failure right after stub creation must not leave a 0-table decoy.

    Forces the exact failure window: central_qi/central_rc get lazy-created
    (fresh, empty dir + --fresh-central), then the canonical bootstrap raises
    before populating any tables.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_registry(tmp_path)

    # Central dir intentionally NOT pre-created — mirrors an operator
    # pointing --central-state at a plausible-but-wrong, never-bootstrapped
    # directory (missing the "state/" segment, exactly as found in prod).
    central_state = tmp_path / "central-fresh"

    def _boom(qi_db, rc_db):
        raise sqlite3.Error("simulated bootstrap failure")

    monkeypatch.setattr(M, "_init_central_if_missing", _boom)

    rc = M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--fresh-central",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])

    assert rc == 3, "expected exit 3 (schema migration failed)"

    central_qi = central_state / "quality_intelligence.db"
    central_rc = central_state / "runtime_coordination.db"
    # Before the fix: both files would exist with 0 tables — the exact decoy
    # artifact found in production. After the fix: this run's own stub is
    # cleaned up since the bootstrap it was created for never populated it.
    assert not central_qi.exists(), (
        f"{central_qi} was left behind as an unpopulated stub after a failed fresh apply"
    )
    assert not central_rc.exists(), (
        f"{central_rc} was left behind as an unpopulated stub after a failed fresh apply"
    )


def test_confirm_apply_banner_shows_real_central_state(monkeypatch, capsys):
    """The interactive confirmation banner must print the ACTUAL write
    target, not the module's hardcoded legacy default — otherwise an
    operator confirming 'yes' has nothing to catch a wrong --central-state
    against before it silently creates files there."""
    real_target = Path("/tmp/some-other-central-dir/state")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("yes\n"))

    result = M.confirm_apply(M.CONFIRMATION_PHRASE, real_target, no_prompt=False)

    assert result is True
    out = capsys.readouterr().out
    assert str(real_target) in out, (
        f"confirmation banner did not mention the real central_state {real_target}: {out!r}"
    )
    assert str(M.CENTRAL_DATA_DIR) not in out or str(M.CENTRAL_DATA_DIR) in str(real_target), (
        "confirmation banner printed the hardcoded legacy default instead of the real target"
    )
