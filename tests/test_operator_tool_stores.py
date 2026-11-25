import os
from pathlib import Path
import subprocess
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]

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
        f"import runpy; m=runpy.run_path({str(ROOT / 'scripts' / tool)!r}); print({read})"],
        capture_output=True, text=True)
    if resolved:
        assert result.returncode == 0, result.stderr
        assert ("d1" if tool.startswith("analysis") else str(data / "unified_reports")) in result.stdout
    else:
        assert result.returncode != 0
        assert "vnx init" in result.stderr and "VNX_PROJECT_ID" in result.stderr
