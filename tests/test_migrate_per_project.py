"""Tests for the --project flag (PR-WAVE2A-2): per-project migrator mode.

Covers all 5 acceptance criteria from the Wave 2a blueprint:
  AC1 - --project <id>: only that project migrated; others skipped
  AC2 - --project unknown-id: exit 2 + error with valid IDs in message
  AC3 - --project + --fresh-central: bootstrap runs on first apply;
         second per-project run skips bootstrap (central not empty)
  AC4 - single-project apply in tmp-dir; central DB contains only
         that project's rows
  AC5 - fresh-central + single project: dispatch_metadata table
         exists + correct project rows present

Dispatch-ID: 20260525-075557-wave2a-2-per-project-mode
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders (mirrors fresh_central test pattern)
# ---------------------------------------------------------------------------


def _make_source_qi_db(path: Path, project_id: str, row_count: int = 3) -> None:
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
        for i in range(row_count):
            con.execute(
                "INSERT INTO dispatch_metadata "
                "(dispatch_id, terminal, track, role, project_id)"
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


def _build_fixture(
    tmp_path: Path,
    project_ids: list[str],
    rows_per_project: int = 3,
) -> tuple[Path, Path]:
    """Build source projects with minimal DBs and write a registry JSON.

    Returns (registry_path, backup_base).
    """
    specs = []
    for pid in project_ids:
        proj_dir = tmp_path / pid
        state_dir = proj_dir / ".vnx-data" / "state"
        _make_source_qi_db(state_dir / "quality_intelligence.db", pid, rows_per_project)
        _make_source_rc_db(state_dir / "runtime_coordination.db", pid)
        specs.append({"name": pid, "path": str(proj_dir), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))

    backup_base = tmp_path / "backups"
    backup_base.mkdir()

    return registry, backup_base


def _run_migrator(
    tmp_path: Path,
    registry: Path,
    backup_base: Path,
    *,
    project: str | None = None,
    fresh_central: bool = False,
    central_state: Path | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Invoke M.main with apply mode.

    Returns (exit_code, central_qi_path, central_rc_path).
    """
    if central_state is None:
        central_state = tmp_path / "central" / "state"

    cmd = [
        "--apply",
        "--confirm", M.CONFIRMATION_PHRASE,
        "--no-prompt",
        "--registry", str(registry),
        "--backup-base", str(backup_base),
        "--central-state", str(central_state),
    ]
    if fresh_central:
        cmd.append("--fresh-central")
    if project is not None:
        cmd.extend(["--project", project])
    if extra_args:
        cmd.extend(extra_args)

    rc = M.main(cmd)
    return rc, central_state / "quality_intelligence.db", central_state / "runtime_coordination.db"


def _get_project_ids_in_central(qi_db: Path) -> list[str]:
    """Return sorted list of distinct project_ids in dispatch_metadata."""
    con = sqlite3.connect(str(qi_db))
    try:
        rows = con.execute(
            "SELECT DISTINCT project_id FROM dispatch_metadata ORDER BY project_id"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _get_row_count_for_project(qi_db: Path, project_id: str) -> int:
    con = sqlite3.connect(str(qi_db))
    try:
        return con.execute(
            "SELECT COUNT(*) FROM dispatch_metadata WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# AC1 + AC4: --project filter — only targeted project rows in central DB
# ---------------------------------------------------------------------------


def test_project_filter_only_imports_targeted_project(tmp_path, monkeypatch):
    """AC1 + AC4: --project proj-a imports ONLY proj-a; proj-b through proj-d absent.

    Also validates AC4: central DB contains exclusively the targeted project's rows.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["proj-a", "proj-b", "proj-c", "proj-d"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)

    rc, qi_db, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="proj-a",
        fresh_central=True,
    )

    assert rc == 0, f"Expected exit 0; got {rc}"
    assert qi_db.exists(), "Central quality_intelligence.db missing after migration"

    present_ids = _get_project_ids_in_central(qi_db)
    assert present_ids == ["proj-a"], (
        f"Central dispatch_metadata must contain only proj-a; got: {present_ids}"
    )


def test_project_filter_row_count_only_targeted(tmp_path, monkeypatch):
    """AC4: central dispatch_metadata has exactly the targeted project's rows.

    Fixture inserts 3 rows per project. With --project proj-a, only 3 rows
    must appear in central (not 12 from all 4 projects).
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["proj-a", "proj-b", "proj-c", "proj-d"]
    registry, backup_base = _build_fixture(tmp_path, project_ids, rows_per_project=3)

    rc, qi_db, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="proj-a",
        fresh_central=True,
    )

    assert rc == 0

    count = _get_row_count_for_project(qi_db, "proj-a")
    assert count == 3, f"Expected 3 rows for proj-a, got {count}"

    # Other projects must have zero rows
    for other in ["proj-b", "proj-c", "proj-d"]:
        other_count = _get_row_count_for_project(qi_db, other)
        assert other_count == 0, (
            f"Expected 0 rows for {other} (not in --project filter), got {other_count}"
        )


def test_project_filter_exits_zero(tmp_path, monkeypatch):
    """--project <valid_id> --fresh-central must exit 0."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_fixture(tmp_path, ["alpha", "beta", "gamma"])

    rc, _, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="alpha",
        fresh_central=True,
    )

    assert rc == 0, f"Expected exit 0 for valid --project; got {rc}"


# ---------------------------------------------------------------------------
# AC2: unknown project_id → exit 2 + valid IDs in log
# ---------------------------------------------------------------------------


def test_unknown_project_id_exits_2(tmp_path, monkeypatch):
    """AC2: --project unknown-xyz must exit 2."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["vnx-orchestration", "seocrawler-v2", "sales-copilot"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)

    rc, _, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="unknown-xyz",
        fresh_central=True,
    )

    assert rc == 2, f"Expected exit 2 for unknown --project; got {rc}"


def test_unknown_project_id_logs_valid_ids(tmp_path, monkeypatch, caplog):
    """AC2: error log must mention valid project_ids from registry."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["vnx-orchestration", "seocrawler-v2", "sales-copilot"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)

    with caplog.at_level(logging.ERROR, logger="vnx.migrate.apply"):
        rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(tmp_path / "central" / "state"),
            "--project", "does-not-exist",
        ])

    assert rc == 2
    # At least one error record must mention the invalid id and valid ids
    combined = " ".join(r.message for r in caplog.records if r.levelname == "ERROR")
    assert "does-not-exist" in combined, (
        "Error log must mention the unknown project_id"
    )
    assert "vnx-orchestration" in combined, (
        "Error log must mention valid project_ids from registry"
    )


# ---------------------------------------------------------------------------
# AC3: --project + --fresh-central interaction
# ---------------------------------------------------------------------------


def test_fresh_central_plus_project_first_run_bootstraps(tmp_path, monkeypatch):
    """AC3 + AC5: first per-project run with --fresh-central bootstraps central DB.

    dispatch_metadata table must exist and contain the targeted project's rows.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["vnx-orchestration", "seocrawler-v2", "sales-copilot", "mission-control"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)

    rc, qi_db, rc_db = _run_migrator(
        tmp_path, registry, backup_base,
        project="vnx-orchestration",
        fresh_central=True,
    )

    assert rc == 0, f"First per-project --fresh-central apply failed with exit {rc}"
    assert qi_db.exists(), "Central QI DB must exist after first per-project apply"

    # dispatch_metadata table must exist (AC5)
    con = sqlite3.connect(str(qi_db))
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_metadata'"
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "dispatch_metadata table absent from central QI DB post-bootstrap"

    # Only vnx-orchestration rows present (AC5)
    present_ids = _get_project_ids_in_central(qi_db)
    assert present_ids == ["vnx-orchestration"], (
        f"Expected only vnx-orchestration rows after first per-project apply; got {present_ids}"
    )


def test_fresh_central_plus_project_second_run_incremental(tmp_path, monkeypatch):
    """AC3: second per-project run (no --fresh-central) adds incrementally.

    Central is populated after the first run, so the second run does NOT
    require --fresh-central and must exit 0.
    """
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["vnx-orchestration", "seocrawler-v2", "sales-copilot"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)
    central_state = tmp_path / "central" / "state"

    # First run: fresh-central + only vnx-orchestration
    rc1, qi_db, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="vnx-orchestration",
        fresh_central=True,
        central_state=central_state,
    )
    assert rc1 == 0, f"First per-project apply failed with exit {rc1}"

    # Second run: incremental, no --fresh-central, different project
    rc2, qi_db2, _ = _run_migrator(
        tmp_path, registry, backup_base,
        project="seocrawler-v2",
        fresh_central=False,   # explicitly NOT passing --fresh-central
        central_state=central_state,
    )
    assert rc2 == 0, (
        f"Second per-project apply (no --fresh-central) failed with exit {rc2}; "
        "central was already bootstrapped so --fresh-central should not be required"
    )

    # Both project rows must now be present
    present_ids = _get_project_ids_in_central(qi_db2)
    assert "vnx-orchestration" in present_ids, "vnx-orchestration rows must survive second run"
    assert "seocrawler-v2" in present_ids, "seocrawler-v2 rows must be present after second run"


def test_fresh_central_plus_project_rc_table_bootstrapped(tmp_path, monkeypatch):
    """AC5: central runtime_coordination.db must have dispatches table after bootstrap."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    registry, backup_base = _build_fixture(
        tmp_path, ["vnx-orchestration", "seocrawler-v2"]
    )

    rc, _, rc_db = _run_migrator(
        tmp_path, registry, backup_base,
        project="vnx-orchestration",
        fresh_central=True,
    )

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


# ---------------------------------------------------------------------------
# AC1: skipping INFO log for excluded projects
# ---------------------------------------------------------------------------


def test_project_filter_logs_skipped_projects(tmp_path, monkeypatch, caplog):
    """AC1: excluded projects must produce INFO 'skipping project X' log lines."""
    monkeypatch.setattr(M, "ABORT_FLAG", tmp_path / ".vnx-aggregator" / "ABORT")
    project_ids = ["proj-a", "proj-b", "proj-c"]
    registry, backup_base = _build_fixture(tmp_path, project_ids)

    with caplog.at_level(logging.INFO, logger="vnx.migrate.apply"):
        rc = M.main([
            "--apply",
            "--confirm", M.CONFIRMATION_PHRASE,
            "--no-prompt",
            "--fresh-central",
            "--registry", str(registry),
            "--backup-base", str(backup_base),
            "--central-state", str(tmp_path / "central" / "state"),
            "--project", "proj-a",
        ])

    assert rc == 0

    info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
    combined = " ".join(info_messages)

    # proj-b and proj-c should appear in a "skipping" info message
    assert "proj-b" in combined, (
        "Expected INFO log mentioning skipped project proj-b"
    )
    assert "proj-c" in combined, (
        "Expected INFO log mentioning skipped project proj-c"
    )
