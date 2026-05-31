#!/usr/bin/env python3
"""vnx version — print VNX version, build info, and resolved paths."""

import os
import platform
import subprocess
import sys
from pathlib import Path


def _read_version_file() -> str:
    # Single source of truth: vnx_cli.__version__ (package metadata in an
    # installed wheel, root VERSION file in a dev checkout).
    from vnx_cli import __version__
    return __version__


def _git_commit(repo_dir: Path) -> str:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return output or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _resolve_vnx_home() -> str:
    env_val = os.environ.get("VNX_HOME")
    if env_val:
        return env_val

    try:
        # Route through the shared engine bootstrap so the packaged
        # (<site-packages>/vnx_orchestration) and dev-checkout layouts resolve
        # identically (PR-PIP-REPACKAGE). Do not recompute scripts/lib inline.
        from vnx_cli import _engine
        _engine.ensure_engine_on_path()
        from vnx_paths import resolve_paths  # type: ignore[import]
        return resolve_paths().get("VNX_HOME", "unresolved")
    except Exception:
        return "unresolved"


def _read_pin(project_dir: Path) -> str:
    pin_file = project_dir / ".vnx-version"
    if pin_file.is_file():
        pin = pin_file.read_text(encoding="utf-8").strip()
        if pin:
            return f"{pin} (project)"
    return "current"


def vnx_version(args) -> int:
    version = _read_version_file()

    # Resolve project root from module location for git
    repo_dir = Path(__file__).resolve().parent.parent.parent
    commit = _git_commit(repo_dir)

    vnx_home = _resolve_vnx_home()

    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    pin = _read_pin(project_dir)

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    plat = platform.system().lower()

    print(f"VNX {version}")
    print(f"Commit: {commit}")
    print(f"VNX_HOME: {vnx_home}")
    print(f"Pin: {pin}")
    print(f"Python: {py_ver} {plat}")

    return 0
