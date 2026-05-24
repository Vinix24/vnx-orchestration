"""Regression tests for the --fresh-central migrator path.

Covers the schema-order bug where runtime_coordination_v10.sql created
CREATE INDEX statements referencing project_id before migration 0010 had
added that column, causing OperationalError "no such column: project_id"
on every fresh --fresh-central run.

Root cause: runtime_coordination_v10.sql contained:
    CREATE INDEX idx_lease_project ON terminal_leases(project_id);
    CREATE INDEX idx_lease_terminal_project ON terminal_leases(terminal_id, project_id);
    CREATE INDEX idx_dispatches_project ON dispatches(project_id);
    CREATE INDEX idx_worker_states_project ON worker_states(project_id);

On a fresh install, init_schema applies v1..v10 sequentially. By the time v10
runs, the base tables (dispatches, terminal_leases, worker_states) already
exist from v1/v9 WITHOUT a project_id column. The v10 CREATE TABLE IF NOT
EXISTS statements are no-ops (tables already present), but the CREATE INDEX
statements still execute — and fail because project_id does not exist yet.
Migration 0010 (which would ADD COLUMN project_id) only runs AFTER
coordination_db.init_schema completes, so the column is never present during
v10 index creation.

Fix: remove the four project_id-referencing CREATE INDEX statements from
runtime_coordination_v10.sql. Those indexes are created by migrations 0010
and 0017 which always run post-bootstrap.

Dispatch-ID: 20260524-204635-migrator-fresh-central-fix
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402


# ---------------------------------------------------------------------------
# Source DB fixture builders (minimal valid schema per project)
# ---------------------------------------------------------------------------


def _make_source_qi_db(path: Path, project_id: str) -> None:
    """Create a minimal quality_intelligence source DB with dispatch_metadata rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS success_patterns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT NOT NULL,
                category     TEXT NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL,
                pattern_data TEXT NOT NULL,
                project_id   TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            CREATE TABLE IF NOT EXISTS dispatch_metadata (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal    TEXT NOT NULL,
                track       TEXT NOT NULL,
                role        TEXT,
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            """
        )
        for i in range(3):
            con.execute(
                "INSERT INTO dispatch_metadata (dispatch_id, terminal, track, role, project_id)"
                " VALUES (?, 'T1', 'A', 'developer', ?)",
                (f"{project_id}-dispatch-{i}", project_id),
            )
        con.commit()
    finally:
        con.close()


def _make_source_rc_db(path: Path, project_id: str) -> None:
    """Create a minimal runtime_coordination source DB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_schema_version (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state       TEXT NOT NULL DEFAULT 'queued',
                project_id  TEXT NOT NULL DEFAULT 'vnx-dev'
            );
            INSERT OR IGNORE INTO runtime_schema_version (version, description)
            VALUES (10, 'test-fixture');
            """
        )
        con.commit()
    finally:
        con.close()


def _build_four_project_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Build 4 minimal source projects and write a registry JSON.

    Returns (registry_path, backup_base).
    """
    specs = []
    project_ids = ["proj-a", "proj-b", "proj-c", "proj-d"]

    for pid in project_ids:
        proj_dir = tmp_path / pid
        state_dir = proj_dir / ".vnx-data" / "state"

        _make_source_qi_db(state_dir / "quality_intelligence.db", pid)
        _make_source_rc_db(state_dir / "runtime_coordination.db", pid)
        specs.append({"name": pid, "path": str(proj_dir), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    return registry, backup_base


def _run_fresh_central(
    tmp_path: Path,
    registry: Path,
    backup_base: Path,
    *,
    extra_args: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Invoke M.main with --fresh-central against an empty central state dir.

    Returns (exit_code, central_qi_path, central_rc_path).
    """
    central_state = tmp_path / "central" / "state"
    # Do NOT pre-create central_state — the migrator must handle a missing dir.

    cmd = [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--fresh-central",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ]
    if extra_args:
        cmd.extend(extra_args)

    rc = M.main(cmd)
    return rc, central_state / "quality_intelligence.db", central_state / "runtime_coordination.db"


# ---------------------------------------------------------------------------
# Happy-path: full fresh-central run
# ---------------------------------------------------------------------------


def test_fresh_central_exits_zero(tmp_path, monkeypatch):
    """--fresh-central apply against a missing central dir must exit 0."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, _, _ = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0, "Expected exit 0 from fresh-central apply"


def test_fresh_central_dispatch_metadata_has_all_project_ids(tmp_path, monkeypatch):
    """Central dispatch_metadata must contain rows for all 4 project_ids after fresh-central."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, qi_db, _ = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0, f"Migration failed with exit code {rc}"
    assert qi_db.exists(), "Central quality_intelligence.db missing after migration"

    con = sqlite3.connect(str(qi_db))
    try:
        project_ids = sorted(
            r[0]
            for r in con.execute(
                "SELECT DISTINCT project_id FROM dispatch_metadata ORDER BY project_id"
            )
        )
    finally:
        con.close()

    assert project_ids == ["proj-a", "proj-b", "proj-c", "proj-d"], (
        f"Expected 4 project_ids in dispatch_metadata, got: {project_ids}"
    )


def test_fresh_central_dispatch_metadata_row_count(tmp_path, monkeypatch):
    """Each of the 4 projects inserts 3 rows; central must have 12 total."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, qi_db, _ = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0
    con = sqlite3.connect(str(qi_db))
    try:
        total = con.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
    finally:
        con.close()

    assert total == 12, f"Expected 12 rows in central dispatch_metadata (4 projects × 3), got {total}"


def test_fresh_central_dispatch_metadata_table_exists_post_bootstrap(tmp_path, monkeypatch):
    """dispatch_metadata must exist in the central QI DB after the bootstrap phase.

    This directly tests the regression: before the fix, the bootstrap would
    fail with 'no such column: project_id' during coordination_db.init_schema
    (v10 CREATE INDEX on project_id that did not exist yet). After the fix,
    bootstrap completes and dispatch_metadata is present.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, qi_db, _ = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0, "Fresh-central migration must not fail with schema-order error"

    con = sqlite3.connect(str(qi_db))
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_metadata'"
        ).fetchone()
    finally:
        con.close()

    assert row is not None, "dispatch_metadata table absent from central QI DB post-bootstrap"


def test_fresh_central_project_id_column_in_dispatch_metadata(tmp_path, monkeypatch):
    """dispatch_metadata must have a project_id column after migration 0010 runs."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, qi_db, _ = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0
    con = sqlite3.connect(str(qi_db))
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info(dispatch_metadata)")]
    finally:
        con.close()

    assert "project_id" in columns, (
        f"project_id column missing from dispatch_metadata after migration 0010; "
        f"columns present: {columns}"
    )


def test_fresh_central_central_rc_has_dispatches_table(tmp_path, monkeypatch):
    """Central runtime_coordination.db must have dispatches table after bootstrap."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, _, rc_db = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0
    assert rc_db.exists(), "Central runtime_coordination.db missing after migration"

    con = sqlite3.connect(str(rc_db))
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatches'"
        ).fetchone()
    finally:
        con.close()

    assert row is not None, "dispatches table absent from central RC DB post-bootstrap"


def test_fresh_central_rc_project_id_column_in_dispatches(tmp_path, monkeypatch):
    """dispatches must have project_id column in central RC DB after migration 0010."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc, _, rc_db = _run_fresh_central(tmp_path, registry, backup_base)

    assert rc == 0
    con = sqlite3.connect(str(rc_db))
    try:
        columns = [r[1] for r in con.execute("PRAGMA table_info(dispatches)")]
    finally:
        con.close()

    assert "project_id" in columns, (
        f"project_id column missing from dispatches in central RC DB; "
        f"columns present: {columns}"
    )


# ---------------------------------------------------------------------------
# Guard: without --fresh-central on an empty central, migrator must refuse
# ---------------------------------------------------------------------------


def test_missing_fresh_central_flag_refuses_empty_dir(tmp_path, monkeypatch):
    """Omitting --fresh-central on a missing central state dir must return exit 1.

    The operator-acknowledgement gate prevents accidental first-deploy wipes.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)
    central_state = tmp_path / "no-central-yet" / "state"
    # Deliberately NOT pre-creating central_state so it is genuinely empty/missing.

    rc = M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        # --fresh-central intentionally OMITTED
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])

    assert rc == 1, (
        "Expected exit 1 when --fresh-central is omitted on an empty central dir"
    )


# ---------------------------------------------------------------------------
# Idempotency: fresh-central run followed by a second apply is a no-op
# ---------------------------------------------------------------------------


def test_fresh_central_idempotent_second_apply(tmp_path, monkeypatch):
    """A second --apply on an already-bootstrapped central must be a clean no-op.

    After the first fresh-central run, the sentinel tables exist and the
    central is no longer fresh. The second run goes through the normal
    (non-fresh) path; it must still exit 0 and not duplicate rows.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_four_project_fixture(tmp_path)

    rc1, qi_db, _ = _run_fresh_central(tmp_path, registry, backup_base)
    assert rc1 == 0, "First fresh-central apply must succeed"

    con = sqlite3.connect(str(qi_db))
    try:
        count_after_run1 = con.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
    finally:
        con.close()

    # Second run: central is no longer fresh, so --fresh-central is not needed.
    central_state = tmp_path / "central" / "state"
    rc2 = M.main([
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ])
    assert rc2 == 0, "Second apply on already-bootstrapped central must exit 0"

    con = sqlite3.connect(str(qi_db))
    try:
        count_after_run2 = con.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
    finally:
        con.close()

    assert count_after_run1 == count_after_run2, (
        f"Row count changed between run 1 ({count_after_run1}) "
        f"and run 2 ({count_after_run2}); second run is not idempotent"
    )


# ---------------------------------------------------------------------------
# Schema-order regression: v10 indexes must not reference project_id directly
# ---------------------------------------------------------------------------


def test_v10_schema_no_project_id_indexes_before_migration_0010(tmp_path):
    """runtime_coordination_v10.sql must not CREATE INDEX on project_id columns.

    This is the direct regression guard. Verifies that the schema file does
    not contain active (non-comment) CREATE INDEX statements referencing
    project_id columns that don't exist during init_schema phase.

    Before the fix, the file contained four such statements:
        CREATE INDEX IF NOT EXISTS idx_lease_project ON terminal_leases(project_id);
        CREATE INDEX IF NOT EXISTS idx_lease_terminal_project ON terminal_leases(terminal_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_dispatches_project ON dispatches(project_id);
        CREATE INDEX IF NOT EXISTS idx_worker_states_project ON worker_states(project_id);

    These caused 'no such column: project_id' on fresh installs. After the fix,
    the index names only appear in comments; the SQL statements are absent.
    """
    import re

    v10_path = ROOT / "schemas" / "runtime_coordination_v10.sql"
    assert v10_path.exists(), f"v10 schema file not found at {v10_path}"

    # Strip comments before scanning for CREATE INDEX on project_id.
    # Comments in the file use -- (single-line only; no block comments in these files).
    non_comment_lines = [
        line for line in v10_path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    non_comment_sql = "\n".join(non_comment_lines)

    # Any CREATE INDEX whose column list contains 'project_id' is forbidden.
    # Pattern: CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON table(...project_id...)
    bad_indexes = re.findall(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+[^\n]+\(\s*[^)]*\bproject_id\b[^)]*\)",
        non_comment_sql,
        re.IGNORECASE,
    )
    assert not bad_indexes, (
        f"runtime_coordination_v10.sql contains active CREATE INDEX statements "
        f"referencing project_id. These cause 'no such column: project_id' on "
        f"fresh installs because project_id is added by migration 0010 AFTER "
        f"coordination_db.init_schema completes. Offending statements: {bad_indexes}"
    )


def test_v10_schema_is_valid_sqlite_on_fresh_db(tmp_path):
    """Apply v1 + v10 sequentially on a fresh DB; must not raise OperationalError.

    Before the fix: applying v10 after v1 would fail with
    'no such column: project_id' during CREATE INDEX execution.
    After the fix: both files apply cleanly.
    """
    rc_db = tmp_path / "runtime_coordination.db"
    v1_sql = (ROOT / "schemas" / "runtime_coordination.sql").read_text()
    v10_sql = (ROOT / "schemas" / "runtime_coordination_v10.sql").read_text()

    con = sqlite3.connect(str(rc_db))
    try:
        con.executescript(v1_sql)
        # This was the failing line before the fix.
        con.executescript(v10_sql)
    except sqlite3.OperationalError as exc:
        pytest.fail(
            f"runtime_coordination_v10.sql failed on a fresh DB after v1: {exc}. "
            f"This is the schema-order bug. Check for CREATE INDEX on project_id "
            f"columns that do not yet exist."
        )
    finally:
        con.close()
