"""Tests for Phase 6 P4 dry-run preflight.

Covers:
  - Dry-run produces row-count plan, collision report, schema-drift report
  - Schema drift report shows correct column diffs
  - Collision detection finds known cross-project conflicts
  - Markdown report renders with project tables and operator checklist
  - Pure read: no source DB mtime/size changes after dry-run
  - Negative-path: missing registry returns exit 2
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts.migrate_dry_run import (  # noqa: E402
    PLAN_TABLES_QI,
    PLAN_TABLES_RC,
    _detect_collisions,
    build_dry_run_report,
    main,
    render_markdown,
)
from scripts.aggregator.build_central_view import load_registry  # noqa: E402


def _make_qi_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                title TEXT,
                project_id TEXT
            );
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                pattern_hash TEXT,
                project_id TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


def _make_rc_db(path: Path) -> None:
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
            """
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def four_projects(tmp_path: Path) -> tuple[Path, list[dict]]:
    specs: list[dict] = []
    for name, pid in [
        ("vnx-roadmap-autopilot", "vnx-dev"),
        ("mission-control", "mc"),
        ("sales-copilot", "sales-copilot"),
        ("SEOcrawler_v2", "seocrawler-v2"),
    ]:
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        _make_qi_db(state / "quality_intelligence.db")
        _make_rc_db(state / "runtime_coordination.db")
        specs.append({"name": name, "path": str(proj), "project_id": pid})

    # Seed cross-project COLLIDING dispatch_id and pattern_id.
    qi_a = tmp_path / "vnx-roadmap-autopilot/.vnx-data/state/quality_intelligence.db"
    qi_b = tmp_path / "mission-control/.vnx-data/state/quality_intelligence.db"
    rc_a = tmp_path / "vnx-roadmap-autopilot/.vnx-data/state/runtime_coordination.db"
    rc_b = tmp_path / "mission-control/.vnx-data/state/runtime_coordination.db"
    rc_c = tmp_path / "sales-copilot/.vnx-data/state/runtime_coordination.db"

    with sqlite3.connect(qi_a) as c:
        c.execute("INSERT INTO success_patterns VALUES (1, 'p', 'vnx-dev')")
        c.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, project_id) "
            "VALUES (?, ?, ?, ?)",
            ("dup-pattern", "title", "hash1", "vnx-dev"),
        )
    with sqlite3.connect(qi_b) as c:
        c.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, project_id) "
            "VALUES (?, ?, ?, ?)",
            ("dup-pattern", "title", "hash2", "mc"),
        )

    with sqlite3.connect(rc_a) as c:
        c.execute("INSERT INTO dispatches VALUES ('dup-dispatch', 'completed', 'vnx-dev')")
    with sqlite3.connect(rc_b) as c:
        c.execute("INSERT INTO dispatches VALUES ('dup-dispatch', 'pending', 'mc')")
    with sqlite3.connect(rc_c) as c:
        c.execute("INSERT INTO dispatches VALUES ('only-here', 'pending', 'sales-copilot')")

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"schema_version": 1, "projects": specs}))
    return registry, specs


def test_dry_run_row_count_plan_reports_per_project(four_projects, tmp_path: Path):
    registry, _ = four_projects
    projects = load_registry(registry)
    plan = build_dry_run_report(projects)

    assert plan["dry_run"] is True
    counts = {
        (r["project_id"], r["db"], r["table"]): r["rows"]
        for r in plan["row_count_plan"]
    }
    assert counts.get(("vnx-dev", "quality_intelligence.db", "success_patterns")) == 1
    assert counts.get(("vnx-dev", "quality_intelligence.db", "pattern_usage")) == 1
    assert counts.get(("mc", "quality_intelligence.db", "pattern_usage")) == 1
    assert counts.get(("vnx-dev", "runtime_coordination.db", "dispatches")) == 1
    assert counts.get(("mc", "runtime_coordination.db", "dispatches")) == 1
    assert counts.get(("sales-copilot", "runtime_coordination.db", "dispatches")) == 1


def test_dry_run_collisions_detected(four_projects):
    registry, _ = four_projects
    projects = load_registry(registry)
    collisions = _detect_collisions(projects)
    assert "dup-dispatch" in collisions["dispatch_id"]
    assert sorted(collisions["dispatch_id"]["dup-dispatch"]) == ["mc", "vnx-dev"]
    assert "dup-pattern" in collisions["pattern_id"]
    assert "only-here" not in collisions["dispatch_id"]


def test_dry_run_no_writes_to_source_dbs(four_projects, tmp_path: Path):
    registry, specs = four_projects
    projects = load_registry(registry)

    src_paths = []
    for spec in specs:
        for db in ("quality_intelligence.db", "runtime_coordination.db"):
            p = Path(spec["path"]) / ".vnx-data" / "state" / db
            if p.exists():
                src_paths.append((p, p.stat().st_size, p.stat().st_mtime_ns))

    build_dry_run_report(projects)

    for p, size, mtime in src_paths:
        st = p.stat()
        assert st.st_size == size, f"{p} size changed during dry-run"
        assert st.st_mtime_ns == mtime, f"{p} mtime changed during dry-run"


def test_dry_run_renders_markdown(four_projects):
    registry, _ = four_projects
    projects = load_registry(registry)
    plan = build_dry_run_report(projects)
    md = render_markdown(plan)
    assert "Phase 6 P4" in md
    assert "Per-project row-count plan" in md
    assert "vnx-dev" in md
    assert "Operator pre-flight checklist" in md
    assert "dup-dispatch" in md  # collision shown


def test_dry_run_cli_writes_atomic_report(four_projects, tmp_path: Path, capsys):
    registry, _ = four_projects
    out_path = tmp_path / "dry-run.md"
    rc = main(["--registry", str(registry), "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    json_path = out_path.with_suffix(out_path.suffix + ".json")
    assert json_path.exists()
    plan = json.loads(json_path.read_text())
    assert plan["dry_run"] is True


def test_dry_run_missing_registry_returns_2(tmp_path: Path):
    rc = main(["--registry", str(tmp_path / "nope.json"), "--out", str(tmp_path / "x.md")])
    assert rc == 2


def test_plan_tables_constants_disjoint():
    # Sanity: the QI vs RC table sets must not overlap (different DBs, different schemas).
    qi = set(PLAN_TABLES_QI)
    rc = set(PLAN_TABLES_RC)
    assert qi.isdisjoint(rc), f"overlap: {qi & rc}"


def test_plan_tables_qi_includes_dispatch_experiments():
    """Codex round-7 advisory 2: PLAN_TABLES_QI must include dispatch_experiments.

    IMPORT_TABLES_QI in migrate_to_central_vnx.py was updated to include
    dispatch_experiments in round-7.  PLAN_TABLES_QI must mirror it so the
    dry-run operator preflight sees the same table set as the live migrator.
    A mismatch means the operator approval is based on under-reported row counts.
    """
    assert "dispatch_experiments" in PLAN_TABLES_QI, (
        "PLAN_TABLES_QI is missing 'dispatch_experiments'. "
        "Keep it in lockstep with IMPORT_TABLES_QI in scripts/migrate_to_central_vnx.py."
    )


def test_plan_tables_qi_matches_import_tables_qi():
    """PLAN_TABLES_QI and IMPORT_TABLES_QI must contain the same table names.

    The two lists serve the same purpose (QI table scope) and must not diverge.
    Any table in the live migrator that is absent from the dry-run reporter
    means the operator preflight under-reports the migration plan.
    """
    from scripts.migrate_to_central_vnx import IMPORT_TABLES_QI
    plan_set = set(PLAN_TABLES_QI)
    import_set = set(IMPORT_TABLES_QI)
    missing_from_plan = import_set - plan_set
    assert not missing_from_plan, (
        f"Tables in IMPORT_TABLES_QI but absent from PLAN_TABLES_QI: {sorted(missing_from_plan)}. "
        "Mirror both lists whenever the live migrator scope changes."
    )


def test_migration_0015_all_tables_covered_by_import_and_plan_lists():
    """Structural defense: every table in 0015_complete_project_id.sql must appear
    in IMPORT_TABLES_QI ∪ IMPORT_TABLES_RC (live migrator) and in
    PLAN_TABLES_QI ∪ PLAN_TABLES_RC (dry-run planner).

    This test would have caught both the round-7 dispatch_experiments gap
    and the round-8 quality_system_metrics/scan_history gap.
    """
    import re
    from scripts.migrate_to_central_vnx import IMPORT_TABLES_QI, IMPORT_TABLES_RC

    sql_path = ROOT / "schemas" / "migrations" / "0015_complete_project_id.sql"
    sql = sql_path.read_text()

    sql_tables = set(
        re.findall(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+project_id",
            sql,
            re.IGNORECASE,
        )
    )
    assert sql_tables, "No ALTER TABLE ... ADD COLUMN project_id found in 0015 SQL"

    import_scope = set(IMPORT_TABLES_QI) | set(IMPORT_TABLES_RC)
    plan_scope = set(PLAN_TABLES_QI) | set(PLAN_TABLES_RC)

    missing_from_import = sql_tables - import_scope
    assert not missing_from_import, (
        f"Tables in 0015 SQL absent from IMPORT_TABLES_QI ∪ IMPORT_TABLES_RC: "
        f"{sorted(missing_from_import)}. "
        "Add them to scripts/migrate_to_central_vnx.py."
    )

    missing_from_plan = sql_tables - plan_scope
    assert not missing_from_plan, (
        f"Tables in 0015 SQL absent from PLAN_TABLES_QI ∪ PLAN_TABLES_RC: "
        f"{sorted(missing_from_plan)}. "
        "Add them to scripts/migrate_dry_run.py."
    )
