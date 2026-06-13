"""tests/test_fsr_isolation_guard.py — R8.6 / PR-0 acceptance tests.

Verifies the enforced test-isolation guard for migrate_future_system.run()
and build_t0_state():

(a) A deliberately mis-written call (VNX_DATA_DIR_EXPLICIT not set) fails
    with the guard RuntimeError.
(b) A correctly-pinned call (VNX_DATA_DIR_EXPLICIT=1 via autouse fixture)
    passes the guard and proceeds to actual logic.
(c) CI canary: canonical ~/.vnx-data/vnx-dev/state DB file hashes are
    unchanged after invoking migration helpers in an isolated tmp dir.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _get_migrate_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_future_system",
        _SCRIPTS / "migrate_future_system.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_v21_project(tmp_path: Path) -> Path:
    """Minimal project with a v21-style dispatches table (passes preflight)."""
    project_dir = tmp_path / "v21project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)

    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
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


# ---------------------------------------------------------------------------
# (a) Mis-written test: guard fires when VNX_DATA_DIR_EXPLICIT is absent
# ---------------------------------------------------------------------------

class TestIsolationGuardFires:
    """Guard raises RuntimeError when VNX_DATA_DIR_EXPLICIT=1 is not set under pytest."""

    def test_guard_fires_without_isolation_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Simulates a mis-written test: VNX_DATA_DIR_EXPLICIT removed, run() blocked.

        The global autouse fixture normally sets this flag. Removing it with
        monkeypatch simulates what would happen if a test author forgot to
        activate the isolation fixture (or wrote the test before #856/#PR-0).
        """
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        mod = _get_migrate_module()
        project_dir = _make_v21_project(tmp_path)

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            mod.run(project_dir)

    def test_guard_error_message_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Guard error message names the fixture so the developer knows how to fix it."""
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        mod = _get_migrate_module()
        project_dir = _make_v21_project(tmp_path)

        with pytest.raises(RuntimeError) as exc_info:
            mod.run(project_dir)

        msg = str(exc_info.value)
        assert "_fsr_migration_module_isolation" in msg or "VNX_DATA_DIR_EXPLICIT" in msg


# ---------------------------------------------------------------------------
# (b) Correctly-pinned test: guard passes, run() proceeds to actual logic
# ---------------------------------------------------------------------------

class TestIsolationGuardPasses:
    """With VNX_DATA_DIR_EXPLICIT=1 (from autouse fixture), guard lets run() proceed."""

    def test_guard_passes_with_isolation_pin(self, tmp_path: Path) -> None:
        """VNX_DATA_DIR_EXPLICIT=1 is set by the autouse fixture; guard is satisfied."""
        assert os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1", (
            "autouse fixture _fsr_migration_module_isolation must set VNX_DATA_DIR_EXPLICIT=1"
        )

        mod = _get_migrate_module()
        project_dir = _make_v21_project(tmp_path)

        try:
            mod.run(project_dir)
        except RuntimeError as exc:
            assert "TEST ISOLATION GUARD" not in str(exc), (
                f"Isolation guard must not fire when VNX_DATA_DIR_EXPLICIT=1 is set: {exc}"
            )

    def test_explicit_env_override_also_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Explicitly setting VNX_DATA_DIR_EXPLICIT=1 (not via autouse) also passes."""
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "_override"))

        mod = _get_migrate_module()
        project_dir = _make_v21_project(tmp_path)

        try:
            mod.run(project_dir)
        except RuntimeError as exc:
            assert "TEST ISOLATION GUARD" not in str(exc), (
                f"Guard should not fire when VNX_DATA_DIR_EXPLICIT=1 is explicitly set: {exc}"
            )


# ---------------------------------------------------------------------------
# (c) CI canary: canonical DB hash unchanged after migration helpers run
# ---------------------------------------------------------------------------

class TestCanonicalDbHashUnchanged:
    """Prove the canonical ~/.vnx-data live DB is untouched by isolated migration runs."""

    def _canonical_db_files(self) -> list[Path]:
        canonical_dir = Path.home() / ".vnx-data" / "vnx-dev" / "state"
        if not canonical_dir.exists():
            return []
        return sorted(canonical_dir.glob("*.db"))

    def _compute_hashes(self, db_files: list[Path]) -> dict[str, str]:
        return {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in db_files
        }

    def test_canonical_db_hash_unchanged_after_migration_run(
        self, tmp_path: Path
    ) -> None:
        """Canonical DB hashes are identical before and after running migration in isolation.

        If the canonical DB is absent (CI, fresh env), this test is skipped gracefully.
        """
        db_files = self._canonical_db_files()
        if not db_files:
            pytest.skip("Canonical DB absent — CI or fresh environment, skipping canary")

        before = self._compute_hashes(db_files)

        # Run a representative migration helper in isolation.
        # VNX_DATA_DIR_EXPLICIT=1 is already set by the autouse fixture,
        # so the guard is satisfied and run() writes only to tmp_path.
        mod = _get_migrate_module()
        project_dir = _make_v21_project(tmp_path)
        try:
            mod.run(project_dir)
        except Exception:
            pass  # migration may raise for reasons unrelated to canonical DB

        after = self._compute_hashes(db_files)

        assert after == before, (
            "CANARY FAILURE: Canonical DB was mutated during isolated migration run! "
            "The TEST ISOLATION GUARD may have failed to protect the live database. "
            f"Changed files: {[k for k in before if before[k] != after.get(k)]}"
        )
