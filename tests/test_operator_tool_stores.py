import os
from pathlib import Path
import subprocess
import sys
import pytest

from operator_paths import operator_data_dir
import project_scope

@pytest.fixture(params=[True, False])
def operator_project(tmp_path, monkeypatch, request):
    for key in tuple(os.environ):
        if key.startswith("VNX_") or key in ("PROJECT_ROOT", "PYTHONPATH"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "other"
    project.mkdir()
    if request.param:
        (project / ".vnx-project-id").write_text("other\n")
    monkeypatch.chdir(project)
    return project, tmp_path / ".vnx-data" / "other", request.param
@pytest.mark.parametrize("tool,read", [
    ("analysis/worker_scope_enforcement_measure.py", "m['load_specs']()"),
    ("benchmarks/run_lane_bench.py", "str(m['operator_data_dir']() / 'unified_reports' if 'operator_data_dir' in m else m['REPORT_DIR_CENTRAL'])"),
    ("benchmark/field-tests/runners/lane_adapter.py", "str((m['report_dirs']() if 'report_dirs' in m else m['REPORT_DIR_CANDIDATES'])[1])"),
])
def test_operator_store(operator_project, tool, read):
    project, data, resolved = operator_project
    pending = data / "dispatches/pending/d1"
    pending.mkdir(parents=True)
    (pending / "dispatch-spec.json").write_text('{"dispatch_id": "d1"}')
    result = subprocess.run([sys.executable, "-c",
        f"import runpy; m=runpy.run_path({str(Path(__file__).resolve().parents[1] / 'scripts' / tool)!r}); print({read})"],
        capture_output=True, text=True)
    if resolved:
        assert result.returncode == 0, result.stderr
        assert ("d1" if tool.startswith("analysis") else str(data / "unified_reports")) in result.stdout
    else:
        assert result.returncode != 0
        assert "vnx init" in result.stderr and "VNX_PROJECT_ID" in result.stderr


@pytest.mark.parametrize("legacy_kind", ["database", "symlink"])
def test_operator_identity_ignores_legacy_database(operator_project, legacy_kind):
    import sqlite3

    project, data, resolved = operator_project
    legacy = project / ".vnx-data" / "state" / "runtime_coordination.db"
    legacy.parent.mkdir(parents=True)
    target = legacy
    if legacy_kind == "symlink":
        target = data.parent / "foreign-project" / "state" / legacy.name
        target.parent.mkdir(parents=True)
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE project_config (project_id TEXT)")
        connection.execute("INSERT INTO project_config VALUES ('foreign-project')")
    if legacy_kind == "symlink":
        legacy.symlink_to(target)

    if resolved:
        assert operator_data_dir() == data
    else:
        with pytest.raises(project_scope.TenantUnresolved) as error:
            operator_data_dir()
        assert "vnx init" in str(error.value)
        assert "VNX_PROJECT_ID" in str(error.value)


def test_operator_identity_uses_canonical_store_layout(operator_project, monkeypatch):
    _, data, _ = operator_project
    state = data / "state"
    state.mkdir(parents=True)
    monkeypatch.chdir(state)
    assert operator_data_dir() == data


def test_operator_explicit_identity_wins(operator_project, monkeypatch):
    _, data, _ = operator_project
    monkeypatch.setenv("VNX_PROJECT_ID", "env-project")
    assert operator_data_dir("chosen-project") == data.parent / "chosen-project"


@pytest.mark.parametrize("project_id", ["..", "/absolute", "Bad_ID"])
def test_operator_invalid_identity_refuses(operator_project, project_id):
    with pytest.raises(project_scope.TenantUnresolved, match="explicit project_id invalid"):
        operator_data_dir(project_id)


def test_operator_environment_identity_conflict_refuses(operator_project, monkeypatch):
    _, data, resolved = operator_project
    monkeypatch.setenv("VNX_PROJECT_ID", "env-project")
    if resolved:
        with pytest.raises(project_scope.TenantUnresolved, match="project_id conflict"):
            operator_data_dir()
    else:
        assert operator_data_dir() == data.parent / "env-project"
