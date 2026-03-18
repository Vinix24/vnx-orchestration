#!/usr/bin/env python3
"""Shared path resolver for VNX Python scripts.

Allows environment overrides while defaulting to dist/runtime-relative paths.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


def _resolve_vnx_home() -> Path:
    vnx_home = os.environ.get("VNX_HOME")
    if vnx_home:
        return Path(vnx_home).expanduser().resolve()

    vnx_bin = os.environ.get("VNX_BIN") or os.environ.get("VNX_EXECUTABLE")
    if vnx_bin:
        return Path(vnx_bin).expanduser().resolve().parent.parent

    here = Path(__file__).resolve()
    # scripts/lib/vnx_paths.py -> scripts/lib -> scripts -> VNX_HOME
    if here.parent.name == "lib":
        return here.parent.parent.parent
    return here.parent.parent


def _resolve_project_root(vnx_home: Path) -> Path:
    project_root_env = os.environ.get("PROJECT_ROOT")
    if project_root_env:
        candidate = Path(project_root_env).expanduser().resolve()
        # Only trust the env var if vnx_home actually lives under that project.
        # This prevents cross-project pollution when multiple VNX installs coexist
        # (e.g. PROJECT_ROOT from project A leaking into project B's scripts).
        try:
            vnx_home.relative_to(candidate)
            return candidate
        except ValueError:
            pass  # env var points to a different project — ignore it

    # Backward compatibility: VNX_HOME under a legacy hidden directory layout
    if vnx_home.name == "vnx-system" and vnx_home.parent.name == ".claude":
        return vnx_home.parent.parent

    # Default: parent of dist root (.vnx -> project root)
    return vnx_home.parent


def resolve_paths() -> Dict[str, str]:
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)

    vnx_data_dir = Path(os.environ.get("VNX_DATA_DIR") or (project_root / ".vnx-data")).expanduser().resolve()

    paths = {
        "VNX_HOME": str(vnx_home),
        "PROJECT_ROOT": str(project_root),
        "VNX_DATA_DIR": str(vnx_data_dir),
        "VNX_STATE_DIR": str(Path(os.environ.get("VNX_STATE_DIR") or (vnx_data_dir / "state")).expanduser()),
        "VNX_DISPATCH_DIR": str(Path(os.environ.get("VNX_DISPATCH_DIR") or (vnx_data_dir / "dispatches")).expanduser()),
        "VNX_LOGS_DIR": str(Path(os.environ.get("VNX_LOGS_DIR") or (vnx_data_dir / "logs")).expanduser()),
        "VNX_PIDS_DIR": str(Path(os.environ.get("VNX_PIDS_DIR") or (vnx_data_dir / "pids")).expanduser()),
        "VNX_LOCKS_DIR": str(Path(os.environ.get("VNX_LOCKS_DIR") or (vnx_data_dir / "locks")).expanduser()),
        "VNX_REPORTS_DIR": str(Path(os.environ.get("VNX_REPORTS_DIR") or (vnx_data_dir / "unified_reports")).expanduser()),
        "VNX_DB_DIR": str(Path(os.environ.get("VNX_DB_DIR") or (vnx_data_dir / "database")).expanduser()),
    }

    # Git-tracked intelligence directory (portable across worktrees)
    paths["VNX_INTELLIGENCE_DIR"] = str(
        Path(os.environ.get("VNX_INTELLIGENCE_DIR") or (project_root / ".vnx-intelligence")).expanduser()
    )

    if "VNX_SKILLS_DIR" in os.environ:
        paths["VNX_SKILLS_DIR"] = os.environ["VNX_SKILLS_DIR"]
    else:
        claude_skills = project_root / ".claude" / "skills"
        if claude_skills.is_dir():
            paths["VNX_SKILLS_DIR"] = str(claude_skills)
        else:
            paths["VNX_SKILLS_DIR"] = str(vnx_home / "skills")

    return paths


def ensure_env() -> Dict[str, str]:
    """Populate os.environ with any missing VNX path defaults."""
    paths = resolve_paths()
    for key, value in paths.items():
        os.environ.setdefault(key, value)
    return paths


if __name__ == "__main__":
    # Print resolved paths for quick diagnostics
    resolved = ensure_env()
    for key in sorted(resolved.keys()):
        print(f"{key}={resolved[key]}")
