"""OI-2819: the worker-scope hook resolves a pinned interpreter, not the bare python3."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = REPO_ROOT / "scripts" / "hooks"
HOOK_SH = HOOK_DIR / "pretooluse_worker_scope_enforce.sh"
LIB_SH = HOOK_DIR / "lib_hook_python.sh"
PINNED = Path("/opt/homebrew/opt/python@3.12/bin/python3.12")


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "VNX_PYTHON"}
    env.update(extra)
    return env


def _resolve(root: Path, env: dict) -> str:
    script = f'. "{LIB_SH}"; vnx_hook_python "{root}"'
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _shim_path(tmp_path: Path) -> tuple[str, Path]:
    marker = tmp_path / "shim_called"
    _exe(tmp_path / "shim" / "python3", f'echo called >> "{marker}"\nexit 1\n')
    return f"{tmp_path / 'shim'}:{os.environ['PATH']}", marker


def test_wrapper_does_not_call_bare_python3_when_vnx_python_is_set(tmp_path):
    path, marker = _shim_path(tmp_path)
    env = _env(PATH=path, VNX_PYTHON=sys.executable, VNX_ENFORCE_WORKER_PERMISSIONS="0")
    out = subprocess.run(["bash", str(HOOK_SH)], input="{}", env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    assert not marker.exists()


def test_wrapper_uses_a_pinned_interpreter_without_vnx_python(tmp_path):
    if not (PINNED.exists() or (REPO_ROOT / ".venv" / "bin" / "python").exists()):
        pytest.skip("no pinned interpreter on this machine")
    path, marker = _shim_path(tmp_path)
    env = _env(PATH=path, VNX_ENFORCE_WORKER_PERMISSIONS="0")
    subprocess.run(["bash", str(HOOK_SH)], input="{}", env=env, capture_output=True, text=True, timeout=30)
    assert not marker.exists()


def test_vnx_python_wins_over_venv(tmp_path):
    venv = _exe(tmp_path / "repo" / ".venv" / "bin" / "python", "echo venv\n")
    pin = _exe(tmp_path / "pin" / "python", "echo pin\n")
    assert _resolve(tmp_path / "repo", _env(VNX_PYTHON=str(pin))) == str(pin)
    assert _resolve(tmp_path / "repo", _env()) == str(venv)


def test_venv_wins_over_pinned_path(tmp_path):
    venv = _exe(tmp_path / "repo" / ".venv" / "bin" / "python", "echo venv\n")
    assert _resolve(tmp_path / "repo", _env()) == str(venv)


def test_non_executable_vnx_python_falls_through(tmp_path):
    venv = _exe(tmp_path / "repo" / ".venv" / "bin" / "python", "echo venv\n")
    plain = tmp_path / "plain"
    plain.write_text("x")
    assert _resolve(tmp_path / "repo", _env(VNX_PYTHON=str(plain))) == str(venv)
    assert _resolve(tmp_path / "repo", _env(VNX_PYTHON=str(tmp_path / "missing"))) == str(venv)


def test_falls_through_to_pinned_then_bare(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    expected = str(PINNED) if os.access(PINNED, os.X_OK) else "python3"
    assert _resolve(root, _env(VNX_PYTHON="")) == expected
