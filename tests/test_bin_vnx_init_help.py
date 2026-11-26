"""OI-3087: the real Bash init CLI must never bootstrap on a help request."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["python", "legacy"])
def init_cli(tmp_path, request):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "store"
    executable = REPO / "bin" / "vnx"
    if request.param == "legacy":
        # Run the shipped Bash CLI without a Python entrypoint or resolver.
        executable = tmp_path / "engine" / "bin" / "vnx"
        executable.parent.mkdir(parents=True)
        shutil.copy2(REPO / "bin" / "vnx", executable)
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("VNX_") and k not in {"PROJECT_ROOT", "PYTHONPATH"}
    }
    env.update(
        HOME=str(home), VNX_PROJECT_ROOT=str(project),
        VNX_DATA_DIR=str(data), VNX_DATA_DIR_EXPLICIT="1",
        VNX_INTELLIGENCE_DIR=str(tmp_path / "intelligence"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return executable, project, data, env


def _run(init_cli, *args):
    executable, project, _, env = init_cli
    return subprocess.run(
        [str(executable), "init", *args], cwd=project, env=env,
        capture_output=True, text=True, timeout=120,
    )


def _snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.stat().st_mode, p.stat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None,
        ) for p in root.rglob("*")
    }


@pytest.mark.parametrize("args", [
    ("--help",), ("-h",), ("--starter", "--help"),
    ("--operator", "-h"), ("--help", "--operator"),
])
def test_help_preserves_starter_mode(tmp_path, init_cli, args):
    data = init_cli[2]
    data.mkdir()
    mode = data / "mode.json"
    original = b'{"schema_version": 1, "mode": "starter", "set_at": "2026-11-26T00:00:00Z"}\n'
    mode.write_bytes(original)
    before = _snapshot(tmp_path)

    result = _run(init_cli, *args)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "usage:" in result.stdout.lower()
    assert "init" in result.stdout
    assert mode.read_bytes() == original
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_does_not_create_state(tmp_path, init_cli, flag):
    before = _snapshot(tmp_path)

    result = _run(init_cli, flag)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "usage:" in result.stdout.lower()
    assert not (init_cli[2] / "mode.json").exists()
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("init_cli", ["python"], indirect=True)
@pytest.mark.parametrize("args,expected", [
    (("--starter",), "starter"), (("--operator",), "operator"), ((), "operator"),
])
def test_normal_init_writes_mode(init_cli, args, expected):
    result = _run(init_cli, *args)

    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads((init_cli[2] / "mode.json").read_text())["mode"] == expected


@pytest.mark.parametrize("init_cli", ["legacy"], indirect=True)
def test_help_propagates_python_failure_without_writes(tmp_path, init_cli):
    # A real but incomplete installation: init exists, its required libs do not.
    script = init_cli[0].parents[1] / "scripts" / "vnx_init.py"
    script.parent.mkdir()
    shutil.copy2(REPO / "scripts" / "vnx_init.py", script)
    before = _snapshot(tmp_path)

    result = _run(init_cli, "--help")

    assert result.returncode == 1
    assert "ModuleNotFoundError" in result.stderr
    assert _snapshot(tmp_path) == before
