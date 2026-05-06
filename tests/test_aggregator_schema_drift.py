"""Tests for the schema-drift report (Phase 6 P1, plan §4.3).

Synthesizes a 4-project fixture where one project is intentionally missing a
column from `success_patterns` and one project is missing a whole table
(`dispatch_metadata`). Asserts the drift report flags the diffs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.aggregator.schema_drift_report import compute_drift, main, _project_schema
from scripts.aggregator.build_central_view import ProjectEntry


def _mk_qi(path: Path, tables: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        for tbl, cols in tables.items():
            col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
            con.execute(f"CREATE TABLE {tbl} ({col_defs})")
        con.commit()
    finally:
        con.close()


def _mk_minimal_other_dbs(state: Path) -> None:
    """Create empty rc/dt DBs so the schema walker has something to attach."""
    for name in ("runtime_coordination.db", "dispatch_tracker.db"):
        con = sqlite3.connect(state / name)
        con.execute("CREATE TABLE _placeholder (id INTEGER)")
        con.commit()
        con.close()


@pytest.fixture
def drift_fixture(tmp_path: Path):
    """Build 4 projects:
      vnx-dev: full schema (reference)
      mc: missing `dispatch_metadata` table entirely
      sales-copilot: success_patterns missing the `confidence` column
      seocrawler-v2: full schema
    """
    full_qi = {
        "success_patterns": ["pattern_id", "pattern_name", "project_id", "confidence"],
        "antipatterns": ["antipattern_id", "signal", "project_id"],
        "dispatch_metadata": ["dispatch_id", "project_id"],
    }
    drifted_qi = {
        "success_patterns": ["pattern_id", "pattern_name", "project_id"],  # missing 'confidence'
        "antipatterns": ["antipattern_id", "signal", "project_id"],
        "dispatch_metadata": ["dispatch_id", "project_id"],
    }
    missing_table_qi = {
        "success_patterns": ["pattern_id", "pattern_name", "project_id", "confidence"],
        "antipatterns": ["antipattern_id", "signal", "project_id"],
        # dispatch_metadata MISSING
    }

    layout = {
        "vnx-roadmap-autopilot": ("vnx-dev", full_qi),
        "mission-control": ("mc", missing_table_qi),
        "sales-copilot": ("sales-copilot", drifted_qi),
        "SEOcrawler_v2": ("seocrawler-v2", full_qi),
    }

    specs = []
    for name, (pid, qi_schema) in layout.items():
        proj = tmp_path / name
        state = proj / ".vnx-data" / "state"
        _mk_qi(state / "quality_intelligence.db", qi_schema)
        _mk_minimal_other_dbs(state)
        specs.append({"name": name, "path": str(proj), "project_id": pid})

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": specs}))
    return registry


def test_project_schema_walks_tables(tmp_path: Path):
    proj = tmp_path / "p"
    state = proj / ".vnx-data" / "state"
    _mk_qi(state / "quality_intelligence.db", {"success_patterns": ["a", "b"]})
    _mk_minimal_other_dbs(state)
    entry = ProjectEntry(name="p", path=proj, project_id="p")
    schema = _project_schema(entry)
    assert "success_patterns" in schema["quality_intelligence.db"]
    assert schema["quality_intelligence.db"]["success_patterns"] == ["a", "b"]


def test_compute_drift_flags_missing_table(drift_fixture):
    from scripts.aggregator.build_central_view import load_registry

    projects = load_registry(drift_fixture)
    schemas = {p.project_id: _project_schema(p) for p in projects}
    drift = compute_drift(schemas)

    mc = drift["projects"]["mc"]["quality_intelligence.db"]
    assert "dispatch_metadata" in mc["missing_tables"]


def test_compute_drift_flags_missing_column(drift_fixture):
    from scripts.aggregator.build_central_view import load_registry

    projects = load_registry(drift_fixture)
    schemas = {p.project_id: _project_schema(p) for p in projects}
    drift = compute_drift(schemas)

    sc = drift["projects"]["sales-copilot"]["quality_intelligence.db"]
    assert "success_patterns" in sc["column_diffs"]
    assert "confidence" in sc["column_diffs"]["success_patterns"]["missing"]


def test_compute_drift_clean_for_reference(drift_fixture):
    from scripts.aggregator.build_central_view import load_registry

    projects = load_registry(drift_fixture)
    schemas = {p.project_id: _project_schema(p) for p in projects}
    drift = compute_drift(schemas)

    # The reference project must have no drift against itself.
    ref_pid = drift["reference"]
    ref_per_db = drift["projects"][ref_pid]
    qi = ref_per_db["quality_intelligence.db"]
    assert qi["missing_tables"] == []
    assert qi["extra_tables"] == []
    assert qi["column_diffs"] == {}


def test_cli_json_emits_valid_report(drift_fixture, capsys):
    rc = main(["--registry", str(drift_fixture), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["reference"] == "vnx-dev"
    assert "mc" in payload["projects"]


def test_cli_text_mode(drift_fixture, capsys):
    rc = main(["--registry", str(drift_fixture)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vnx-dev" in out
    assert "mc" in out
    assert "DRIFT" in out
