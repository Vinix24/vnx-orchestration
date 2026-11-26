"""AG-2a1: project discovery, packaged defaults, and unchanged local workers."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib"
DEFAULT = LIB / "defaults" / "vnx_workers.default.yaml"
sys.path.insert(0, str(LIB))
import worker_registry as registry


def test_ag2a1_project_registry_outside_fabric(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    operator = tmp_path / ".vnx" / "vnx_workers.yaml"
    operator.parent.mkdir()
    operator.write_text(
        "schema_version: 1\nworkers:\n"
        "  - {terminal_id: T99, role: orchestrator, provider: codex, "
        "model: custom, pool_id: consumer}\n"
        "pools:\n  - {pool_id: consumer, min_workers: 1, max_workers: 1}\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VNX_CANONICAL_ROOT", str(ROOT))
    assert not LIB.is_relative_to(tmp_path)
    assert [w.terminal_id for w in registry._load_registry().list_workers()] == ["T99"]
    assert registry._find_yaml_file() == operator


def test_ag2a1_consumer_without_validator_rejects_unknown_role(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    operator = tmp_path / ".vnx" / "vnx_workers.yaml"
    operator.parent.mkdir()
    operator.write_text(
        "schema_version: 1\nworkers:\n"
        "  - {terminal_id: T99, role: backned-developer, provider: codex, "
        "pool_id: consumer}\n"
        "pools:\n  - {pool_id: consumer}\n"
    )
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "scripts" / "validate_skill.py").exists()
    with pytest.raises(ValueError, match="Invalid role 'backned-developer'"):
        registry._load_registry()


@pytest.mark.parametrize("module", ["worker_registry", "scripts.lib.worker_registry"])
def test_ag2a1_import_does_not_execute_consumer_validator(tmp_path, module):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = scripts / "validator-executed"
    (scripts / "validate_skill.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('validator-executed').write_text('executed')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c",
         f"import importlib; import {module} as r; importlib.reload(r); "
         "assert r.WORKER_REGISTRY.by_id('T1').role == 'backend-developer'"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": os.pathsep.join([str(LIB), str(ROOT)])},
        capture_output=True, text=True,
    )
    assert not marker.exists(), "Import executed the consumer project's validator"
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["worker_registry", "scripts.lib.worker_registry"])
def test_ag2a1_default_from_package_without_repo_root(tmp_path, module):
    lib = tmp_path / "installed" / "scripts" / "lib"
    (lib / "defaults").mkdir(parents=True)
    for name in ("worker_registry.py", "project_root.py"):
        shutil.copyfile(LIB / name, lib / name)
    shutil.copyfile(DEFAULT, lib / "defaults" / DEFAULT.name)
    project = tmp_path / "consumer"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    result = subprocess.run(
        [sys.executable, "-c",
         f"import {module} as r; from pathlib import Path; "
         "assert r._find_yaml_file() == Path(r.__file__).parent / 'defaults' / "
         "'vnx_workers.default.yaml'; "
         "assert [w.terminal_id for w in r.WORKER_REGISTRY.list_workers()] == "
         "['T0', 'T1', 'T2', 'T3']"],
        cwd=project, env={**os.environ, "PYTHONPATH": os.pathsep.join([str(lib), str(lib.parents[1])])},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_ag2a1_default_is_package_data():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    package_root = ROOT / config["package-dir"]["vnx_orchestration"]
    includes = config["package-data"]["vnx_orchestration"]
    excludes = config["exclude-package-data"]["vnx_orchestration"]
    assert DEFAULT.is_file()
    assert any(DEFAULT in package_root.glob(pattern) for pattern in includes)
    assert not any(DEFAULT in package_root.glob(pattern) for pattern in excludes)
    assert not (ROOT / ".vnx" / DEFAULT.name).exists()


def test_ag2a1_repo_registry_matches_before_snapshot():
    # Recorded from _load_registry(ROOT) on main c27c19f7e, before relocation.
    reg = registry._load_registry(ROOT)
    assert reg.list_workers() == [
        registry.Worker("T0", "orchestrator", "claude", "opus", "default", []),
        registry.Worker("T1", "backend-developer", "claude", "sonnet", "default", []),
        registry.Worker("T2", "backend-developer", "claude", "sonnet", "default", []),
        registry.Worker("T3", "reviewer", "claude", "sonnet", "default", []),
    ]
    assert reg.list_pools() == [registry.Pool("default", 4, 4, "fixed", [])]


@pytest.mark.parametrize("loader", [registry._find_yaml_file, registry._load_registry])
def test_ag2a1_unresolved_project_stays_visible(tmp_path, monkeypatch, loader):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VNX_CANONICAL_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="Cannot resolve project root"):
        loader()


def test_ag2a1_unreadable_operator_is_not_replaced_by_default(tmp_path, monkeypatch):
    operator = tmp_path / ".vnx" / "vnx_workers.yaml"
    operator.parent.mkdir()
    operator.write_text("schema_version: 1\n")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == operator:
            raise PermissionError("registry access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError, match="registry access denied"):
        registry._load_registry(tmp_path)
