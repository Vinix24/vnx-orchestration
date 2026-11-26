"""AG-2a1: project discovery, packaged defaults, and unchanged local workers."""

from fnmatch import fnmatchcase
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


def _matches_package_pattern(relative, pattern, *, exclude=False):
    # Setuptools includes glob by component (* stays within /, ** recurses); excludes use full-path fnmatch.
    if exclude:
        return fnmatchcase(relative.as_posix(), pattern)

    def match(parts, patterns):
        if not patterns:
            return not parts
        head, *tail = patterns
        if head == "**":
            return match(parts, tail) or bool(
                parts and not parts[0].startswith(".") and match(parts[1:], patterns)
            )
        return bool(
            parts
            and (not parts[0].startswith(".") or head.startswith("."))
            and fnmatchcase(parts[0], head)
            and match(parts[1:], tail)
        )

    return match(relative.parts, pattern.split("/"))


def test_ag2a1_default_is_package_data():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    package_root = ROOT / config["package-dir"]["vnx_orchestration"]
    includes = config["package-data"]["vnx_orchestration"]
    excludes = config["exclude-package-data"]["vnx_orchestration"]
    relative = DEFAULT.relative_to(package_root)
    assert DEFAULT.is_file()
    assert any(_matches_package_pattern(relative, pattern) for pattern in includes)
    assert not any(_matches_package_pattern(relative, pattern, exclude=True) for pattern in excludes)
    assert not (ROOT / ".vnx" / DEFAULT.name).exists()


@pytest.mark.parametrize(("relative", "included", "excluded"), [
    ("scripts/lib/defaults/vnx_workers.default.yaml", True, False),
    ("scripts/maintenance/export_toolkit_plugin.sh", True, True),
    ("scripts/lib/schema_prep.py", True, True),
    ("README.md", False, False),
])
def test_ag2a1_package_data_pattern_membership(relative, included, excluded):
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    path = Path(relative)
    includes = config["package-data"]["vnx_orchestration"]
    excludes = config["exclude-package-data"]["vnx_orchestration"]
    assert any(_matches_package_pattern(path, p) for p in includes) is included
    assert any(_matches_package_pattern(path, p, exclude=True) for p in excludes) is excluded


@pytest.mark.parametrize(("pattern", "relative", "expected"), [
    ("scripts/**/*", "scripts/vnx_guard.py", True),
    ("scripts/**/*", "scripts/lib/defaults/vnx_workers.default.yaml", True),
    ("scripts/**/*", "other/scripts/vnx_guard.py", False),
    ("scripts/**/*", "scripts/.hidden/config.yaml", False),
    ("scripts/**/*", "scripts/lib/.hidden.yaml", False),
    ("**/*.pyc", "worker_registry.pyc", True),
    ("**/*.pyc", "scripts/lib/__pycache__/worker_registry.cpython-311.pyc", True),
    ("**/*.pyc", "scripts/lib/worker_registry.py", False),
    ("**/__pycache__/**", "__pycache__/worker_registry.pyc", True),
    ("**/__pycache__/**", "scripts/lib/__pycache__/worker_registry.pyc", True),
    ("**/__pycache__/**", "scripts/lib/worker_registry.py", False),
    ("scripts/*_prep*", "scripts/schema_prep.py", True),
    ("scripts/*_prep*", "scripts/lib/schema_prep.py", False),
    (".vnx-data/**", ".vnx-data/worktrees/dispatch/scripts/lib/worker_registry.py", True),
    ("scripts/maintenance/export_toolkit_plugin.sh", "scripts/maintenance/export_toolkit_plugin.sh.bak", False),
])
def test_ag2a1_package_glob_semantics(pattern, relative, expected):
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    patterns = config["package-data"]["vnx_orchestration"] + config["exclude-package-data"]["vnx_orchestration"]
    assert pattern in patterns, "Exercise the real packaging patterns"
    assert _matches_package_pattern(Path(relative), pattern) is expected


@pytest.mark.parametrize(("relative", "excluded"), [
    ("scripts/schema_prep.py", True),
    ("scripts/lib/schema_prep.py", True),
    ("scripts/lib/defaults/vnx_workers.default.yaml", False),
    ("other/scripts/schema_prep.py", False),
])
def test_ag2a1_exclusion_matches_setuptools(relative, excluded):
    from setuptools import Distribution
    from setuptools.command.build_py import build_py

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]
    pattern = "scripts/*_prep*"
    assert pattern in config["exclude-package-data"]["vnx_orchestration"]
    command = build_py(Distribution())
    command.exclude_package_data = {"vnx_orchestration": [pattern]}
    files = [str(ROOT / relative)]
    assert command.exclude_data_files("vnx_orchestration", str(ROOT), files) == ([] if excluded else files)
    assert _matches_package_pattern(Path(relative), pattern, exclude=True) is excluded


def test_ag2a1_package_data_ignores_vanished_worktree(monkeypatch):
    original_glob = Path.glob
    vanished = ROOT / ".vnx-data/worktrees/dispatch-test-dispatch-deepseek/scripts/lib"

    def glob(path, pattern):
        if pattern.startswith("**/"):
            raise FileNotFoundError(2, "No such file or directory", str(vanished))
        return original_glob(path, pattern)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "glob", glob)
        test_ag2a1_default_is_package_data()


def test_ag2a1_package_data_never_walks(monkeypatch):
    def forbid_walk(*args, **kwargs):
        pytest.fail("Package-data membership must not enumerate directories")

    with monkeypatch.context() as patch:
        for method in ("glob", "rglob", "iterdir"):
            patch.setattr(Path, method, forbid_walk)
        patch.setattr(os, "scandir", forbid_walk)
        test_ag2a1_default_is_package_data()


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
