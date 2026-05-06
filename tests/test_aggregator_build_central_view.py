"""Tests for the read-only federation aggregator builder.

Covers:
  - 4-project fixture attach + materialize
  - Legacy rows (NULL project_id) get synthesized project_id from the
    project's slug (Phase 0 backfill behavior, plan §0.1)
  - Read-only mode is honored: writes against the attached source DB
    raise sqlite3.OperationalError
  - --dry-run produces no view DB
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.aggregator.build_central_view import (
    UNIFIED_TABLES,
    attach_readonly,
    load_registry,
    main,
    materialize_views,
    synthesize_project_id,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_quality_intelligence_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE success_patterns (
                pattern_id INTEGER PRIMARY KEY,
                pattern_name TEXT,
                project_id TEXT
            );
            CREATE TABLE antipatterns (
                antipattern_id INTEGER PRIMARY KEY,
                signal TEXT,
                project_id TEXT
            );
            CREATE TABLE dispatch_metadata (
                dispatch_id TEXT PRIMARY KEY,
                project_id TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_runtime_coordination_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE dispatches (
                dispatch_id TEXT PRIMARY KEY,
                state TEXT,
                project_id TEXT
            );
            CREATE TABLE terminal_leases (
                terminal_id TEXT PRIMARY KEY,
                holder TEXT,
                project_id TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_dispatch_tracker_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE dispatch_experiments (id INTEGER PRIMARY KEY)")
        con.commit()
    finally:
        con.close()


def _seed_success_patterns(
    db_path: Path, rows: list[tuple[int, str, str | None]]
) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executemany(
            "INSERT INTO success_patterns (pattern_id, pattern_name, project_id) VALUES (?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _seed_dispatches(db_path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executemany(
            "INSERT INTO dispatches (dispatch_id, state, project_id) VALUES (?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def four_project_fixture(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Build a 4-project fixture with mini DBs.

    Returns `(registry_path, project_specs)` where project_specs lists
    each project's name/path/project_id.
    """
    specs: list[dict] = []
    for name, pid in [
        ("vnx-roadmap-autopilot", "vnx-dev"),
        ("mission-control", "mc"),
        ("sales-copilot", "sales-copilot"),
        ("SEOcrawler_v2", "seocrawler-v2"),
    ]:
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        _make_quality_intelligence_db(state / "quality_intelligence.db")
        _make_runtime_coordination_db(state / "runtime_coordination.db")
        _make_dispatch_tracker_db(state / "dispatch_tracker.db")
        specs.append({"name": name, "path": str(proj), "project_id": pid})

    # Seed:
    # - vnx-dev gets two patterns with explicit project_id
    # - mc gets one pattern with NULL project_id (legacy)
    # - sales-copilot gets one pattern with empty-string project_id (also legacy)
    # - seocrawler-v2 gets one pattern with explicit project_id
    _seed_success_patterns(
        tmp_path / "vnx-roadmap-autopilot/.vnx-data/state/quality_intelligence.db",
        [(1, "p1", "vnx-dev"), (2, "p2", "vnx-dev")],
    )
    _seed_success_patterns(
        tmp_path / "mission-control/.vnx-data/state/quality_intelligence.db",
        [(1, "p1", None)],
    )
    _seed_success_patterns(
        tmp_path / "sales-copilot/.vnx-data/state/quality_intelligence.db",
        [(1, "p1", "")],
    )
    _seed_success_patterns(
        tmp_path / "SEOcrawler_v2/.vnx-data/state/quality_intelligence.db",
        [(1, "p1", "seocrawler-v2")],
    )

    _seed_dispatches(
        tmp_path / "vnx-roadmap-autopilot/.vnx-data/state/runtime_coordination.db",
        [("d1", "completed", "vnx-dev")],
    )
    _seed_dispatches(
        tmp_path / "mission-control/.vnx-data/state/runtime_coordination.db",
        [("d2", "completed", None)],
    )

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))
    return registry, specs


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_synthesize_project_id_basic():
    assert synthesize_project_id("vnx-roadmap-autopilot") == "vnx-roadmap-autopilot"
    assert synthesize_project_id("Mission-Control") == "mission-control"
    assert synthesize_project_id("SEOcrawler_v2") == "seocrawler-v2"


def test_synthesize_project_id_strips_and_truncates():
    assert synthesize_project_id("__weird name!!__") == "weird-name"
    long = "x" * 64
    assert len(synthesize_project_id(long)) == 32


def test_synthesize_project_id_rejects_empty():
    with pytest.raises(ValueError):
        synthesize_project_id("...")


def test_load_registry(four_project_fixture):
    registry, specs = four_project_fixture
    projects = load_registry(registry)
    assert [p.project_id for p in projects] == [s["project_id"] for s in specs]


def test_load_registry_synthesizes_missing_project_id(tmp_path: Path):
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": "mission-control", "path": str(tmp_path)}
                ]
            }
        )
    )
    projects = load_registry(registry)
    assert projects[0].project_id == "mission-control"


# ---------------------------------------------------------------------------
# Read-only attachment guarantee
# ---------------------------------------------------------------------------


def test_attach_readonly_blocks_writes(tmp_path: Path):
    db = tmp_path / "src.db"
    con0 = sqlite3.connect(db)
    con0.execute("CREATE TABLE t (id INTEGER)")
    con0.execute("INSERT INTO t VALUES (1)")
    con0.commit()
    con0.close()

    view = sqlite3.connect(":memory:")
    try:
        attach_readonly(view, "src", db)
        # Reads succeed.
        assert view.execute("SELECT id FROM src.t").fetchone() == (1,)
        # Writes raise OperationalError because of mode=ro.
        with pytest.raises(sqlite3.OperationalError):
            view.execute("INSERT INTO src.t VALUES (2)")
    finally:
        view.close()


# ---------------------------------------------------------------------------
# Materialize behavior
# ---------------------------------------------------------------------------


def test_materialize_attaches_all_four_projects(four_project_fixture, tmp_path: Path):
    registry, _ = four_project_fixture
    projects = load_registry(registry)
    view_db = tmp_path / "agg" / "data.db"

    plan = materialize_views(view_db, projects, dry_run=False)
    assert plan["dry_run"] is False
    assert view_db.exists()

    con = sqlite3.connect(view_db)
    try:
        seen = {
            row[0]
            for row in con.execute(
                "SELECT DISTINCT project_id FROM success_patterns_unified"
            )
        }
    finally:
        con.close()
    assert seen == {"vnx-dev", "mc", "sales-copilot", "seocrawler-v2"}


def test_materialize_synthesizes_project_id_for_legacy_rows(four_project_fixture, tmp_path: Path):
    registry, _ = four_project_fixture
    projects = load_registry(registry)
    view_db = tmp_path / "agg" / "data.db"
    materialize_views(view_db, projects, dry_run=False)

    con = sqlite3.connect(view_db)
    try:
        # The mc row had NULL project_id; the sales-copilot row had "".
        # Both should be backfilled to their project's slug in the unified view.
        rows = con.execute(
            "SELECT project_id FROM success_patterns_unified ORDER BY project_id"
        ).fetchall()
    finally:
        con.close()
    pids = sorted(r[0] for r in rows)
    assert "mc" in pids
    assert "sales-copilot" in pids
    # No NULL or empty project_ids leak into the unified table.
    assert all(p not in (None, "") for p in pids)


def test_materialize_idempotent(four_project_fixture, tmp_path: Path):
    registry, _ = four_project_fixture
    projects = load_registry(registry)
    view_db = tmp_path / "agg" / "data.db"

    materialize_views(view_db, projects, dry_run=False)
    con = sqlite3.connect(view_db)
    try:
        first = con.execute(
            "SELECT COUNT(*) FROM success_patterns_unified"
        ).fetchone()[0]
    finally:
        con.close()

    materialize_views(view_db, projects, dry_run=False)
    con = sqlite3.connect(view_db)
    try:
        second = con.execute(
            "SELECT COUNT(*) FROM success_patterns_unified"
        ).fetchone()[0]
    finally:
        con.close()

    assert first == second


def test_materialize_does_not_mutate_source_dbs(four_project_fixture, tmp_path: Path):
    registry, specs = four_project_fixture
    projects = load_registry(registry)
    view_db = tmp_path / "agg" / "data.db"

    src_paths = []
    for spec in specs:
        for db in (
            "quality_intelligence.db",
            "runtime_coordination.db",
            "dispatch_tracker.db",
        ):
            p = Path(spec["path"]) / ".vnx-data" / "state" / db
            src_paths.append((p, p.stat().st_size, p.stat().st_mtime_ns))

    materialize_views(view_db, projects, dry_run=False)

    for p, size, mtime in src_paths:
        st = p.stat()
        assert st.st_size == size, f"{p} size changed"
        assert st.st_mtime_ns == mtime, f"{p} mtime changed"


def test_dry_run_writes_nothing(four_project_fixture, tmp_path: Path):
    registry, _ = four_project_fixture
    projects = load_registry(registry)
    view_db = tmp_path / "agg" / "data.db"

    plan = materialize_views(view_db, projects, dry_run=True)
    assert plan["dry_run"] is True
    # The view DB must not exist after a dry-run.
    assert not view_db.exists()
    # And neither should the directory have been created.
    assert not view_db.parent.exists()


def test_unified_tables_constants_consistent():
    # Each entry is (db_name, table, has_project_id) and the db_name must be one
    # of the three source DBs we attach.
    valid_dbs = {"quality_intelligence.db", "runtime_coordination.db", "dispatch_tracker.db"}
    for db_name, _table, _has_pid in UNIFIED_TABLES:
        assert db_name in valid_dbs


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_dry_run_exits_zero_and_prints_plan(four_project_fixture, tmp_path: Path, capsys):
    registry, _ = four_project_fixture
    view_db = tmp_path / "agg" / "data.db"
    rc = main(["--dry-run", "--registry", str(registry), "--view-db", str(view_db), "--json"])
    assert rc == 0
    captured = capsys.readouterr().out
    plan = json.loads(captured)
    assert plan["dry_run"] is True
    assert not view_db.exists()
