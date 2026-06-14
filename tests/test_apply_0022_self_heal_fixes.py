"""PR-B fix-forward 2 — apply_0022 self-heal blockers (B-N1/B-N2/B-N3).

Codex re-gate of the #863 self-heal found three REAL blockers in
``apply_0022._self_heal_dispatches_project_id``; this module proves the fixed
behavior, one behavioral test per blocker:

  B-N1 [tenant corruption]: the old ``ALTER TABLE dispatches ADD COLUMN
        project_id TEXT NOT NULL DEFAULT 'vnx-dev'`` stamped EVERY legacy row as
        'vnx-dev'. The fix resolves the VALIDATED tenant (DB-path-anchored,
        fail-closed) and stamps THAT — never a silent 'vnx-dev' default.
  B-N2 [ordering]: the self-heal ran unconditionally BEFORE the version gate, so
        an already-current DB was mutated. The fix gates it on user_version < 22.
  B-N3 [non-atomic]: the self-heal ran in autocommit BEFORE the migration
        savepoint. The fix runs it INSIDE one savepoint with the v22 rebuild, so
        a mid-way failure rolls BOTH back.

ADR-007: dispatches is a central-DB table; project_id is its tenant key and the
v22 rebuild adds the composite UNIQUE(dispatch_id, project_id). Stamping the wrong
tenant is exactly the multi-tenant corruption ADR-007 forbids. See
docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md

Discipline: temp-DB ONLY. No live ~/.vnx-data is ever touched.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
_MIGRATIONS = _REPO_ROOT / "schemas" / "migrations"
_V22_SQL = _MIGRATIONS / "0022_track_layer.sql"

# These exercise the auto_apply lane, where 0022 builds the composite UNIQUE
# itself; the migrate_future_system v22 preflight must not leak in (conftest).
pytestmark = pytest.mark.usefixtures("isolate_v22_composite_preflight")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_apply_0022():
    """Load apply_0022.py as a fresh, isolated module (as auto_apply does)."""
    spec = importlib.util.spec_from_file_location(
        "_apply_0022_selfheal_test", _LIB_DIR / "migrations" / "apply_0022.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_legacy_db(db_path: Path, *, user_version: int = 20, state: str = "completed") -> None:
    """A legacy / pre-project_id runtime_coordination.db: dispatches has NO
    project_id column and PRAGMA user_version is diverged ahead of the schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
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
            """
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state) VALUES ('legacy-1', ?)", (state,)
        )
        conn.execute(f"PRAGMA user_version = {int(user_version)}")
        conn.commit()
    finally:
        conn.close()


def _has_project_id(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return any(r[1] == "project_id" for r in conn.execute("PRAGMA table_info(dispatches)"))
    finally:
        conn.close()


def _row_project_ids(db_path: Path) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT project_id FROM dispatches ORDER BY id")]
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        conn.close()


def _table_exists(db_path: Path, table: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B-N1 — validated tenant, never 'vnx-dev'; fail closed when unresolvable
# ---------------------------------------------------------------------------

def test_b_n1_stamps_db_path_validated_tenant_never_vnx_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy rows are stamped with the DB-path-derived tenant, not 'vnx-dev'."""
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)  # prove it came from the DB path
    # Canonical layout: <root>/.vnx-data/<project_id>/state/runtime_coordination.db
    db_path = tmp_path / ".vnx-data" / "acme-corp" / "state" / "runtime_coordination.db"
    _make_legacy_db(db_path)
    mod = _load_apply_0022()

    applied = mod.apply_migration(db_path, _V22_SQL)

    assert applied is True
    assert _has_project_id(db_path)
    # The whole point of B-N1: the real tenant, NOT the 'vnx-dev' sentinel.
    assert _row_project_ids(db_path) == ["acme-corp"]
    assert "vnx-dev" not in _row_project_ids(db_path)


def test_b_n1_fails_closed_when_tenant_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No DB-path/marker/env identity → raise (fail closed), DB left unchanged."""
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    db_path = tmp_path / "state" / "runtime_coordination.db"  # non-canonical, no marker
    _make_legacy_db(db_path)
    mod = _load_apply_0022()

    with pytest.raises(RuntimeError, match="ADR-007 fail-closed"):
        mod.apply_migration(db_path, _V22_SQL)

    # Fail closed leaves the DB byte-unchanged: no wrong-tenant stamp, no version bump.
    assert not _has_project_id(db_path)
    assert _user_version(db_path) == 20


# ---------------------------------------------------------------------------
# B-N2 — self-heal is version-gated, not run ahead of the version check
# ---------------------------------------------------------------------------

def test_b_n2_self_heal_is_version_gated_when_already_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB already at user_version >= 22 is NOT mutated by the self-heal."""
    monkeypatch.setenv("VNX_PROJECT_ID", "acme-corp")  # resolvable, to isolate the gate
    db_path = tmp_path / "state" / "runtime_coordination.db"
    _make_legacy_db(db_path, user_version=25)  # diverged AHEAD of v22 → "current"
    mod = _load_apply_0022()

    applied = mod.apply_migration(db_path, _V22_SQL)

    # Old code ran the self-heal unconditionally and would have ADDED project_id here.
    assert applied is False
    assert not _has_project_id(db_path)
    assert _user_version(db_path) == 25


# ---------------------------------------------------------------------------
# B-N3 — self-heal + migration are atomic (one savepoint)
# ---------------------------------------------------------------------------

def test_b_n3_self_heal_rolls_back_atomically_on_migration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the v22 rebuild fails, the self-heal's project_id stamp is rolled back too."""
    monkeypatch.setenv("VNX_PROJECT_ID", "acme-corp")
    db_path = tmp_path / "state" / "runtime_coordination.db"
    _make_legacy_db(db_path)
    mod = _load_apply_0022()

    # A migration that creates an observable object, then fails — so we can assert
    # BOTH the migration statements AND the preceding self-heal rolled back together.
    broken_sql = tmp_path / "broken_0022.sql"
    broken_sql.write_text(
        "CREATE TABLE selfheal_atomicity_probe (x INTEGER);\n"
        "INSERT INTO __table_that_does_not_exist__ VALUES (1);\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        mod.apply_migration(db_path, broken_sql)

    # Atomic: the self-heal stamp (project_id) is gone, the probe table is gone,
    # the version is unchanged, and the legacy row survives intact.
    assert not _has_project_id(db_path)
    assert not _table_exists(db_path, "selfheal_atomicity_probe")
    assert _user_version(db_path) == 20
    assert _table_exists(db_path, "dispatches")
