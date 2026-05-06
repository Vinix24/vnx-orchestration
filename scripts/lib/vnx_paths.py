#!/usr/bin/env python3
"""Shared path resolver for VNX Python scripts.

Allows environment overrides while defaulting to dist/runtime-relative paths.
"""

from __future__ import annotations

import os
import subprocess
import warnings
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


def _is_embedded_layout(vnx_home: Path) -> bool:
    return vnx_home.name == "vnx-system" and vnx_home.parent.name == ".claude"


def _git_toplevel(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not output:
        return None
    return Path(output).expanduser().resolve()


def _git_common_root(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not output:
        return None
    common_dir = Path(output).expanduser().resolve()
    return common_dir.parent if common_dir.name == ".git" else common_dir


def _default_project_root(vnx_home: Path) -> Path:
    if _is_embedded_layout(vnx_home):
        return vnx_home.parent.parent.resolve()

    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        # Standalone repo/worktree layout: runtime/bootstrap stay local to the repo checkout.
        return vnx_home.resolve()

    return vnx_home.parent.resolve()


def _default_canonical_root(vnx_home: Path) -> Path:
    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        return _git_common_root(vnx_home) or vnx_home.resolve()
    return vnx_home.resolve()


def _resolve_project_root(vnx_home: Path) -> Path:
    default_root = _default_project_root(vnx_home)
    project_root_env = os.environ.get("PROJECT_ROOT")
    if project_root_env:
        candidate = Path(project_root_env).expanduser().resolve()
        if candidate == default_root:
            return candidate

    return default_root


def resolve_paths() -> Dict[str, str]:
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)
    canonical_root = Path(
        os.environ.get("VNX_CANONICAL_ROOT") or _default_canonical_root(vnx_home)
    ).expanduser().resolve()

    _explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    _explicit_val = os.environ.get("VNX_DATA_DIR")
    if _explicit_flag and _explicit_val:
        vnx_data_dir = Path(_explicit_val).expanduser().resolve()
    else:
        if _explicit_val and not _explicit_flag:
            warnings.warn(
                f"VNX_DATA_DIR env-var set ({_explicit_val}) but "
                "VNX_DATA_DIR_EXPLICIT=1 is required for it to be honored. "
                "Ignoring and using VNX_HOME-resolved project root. "
                "See https://github.com/Vinix24/vnx-orchestration/issues/225",
                DeprecationWarning,
                stacklevel=2,
            )
        vnx_data_dir = (project_root / ".vnx-data").resolve()

    paths = {
        "VNX_HOME": str(vnx_home),
        "PROJECT_ROOT": str(project_root),
        "VNX_CANONICAL_ROOT": str(canonical_root),
        "VNX_DATA_DIR": str(vnx_data_dir),
        "VNX_STATE_DIR": str(Path(os.environ.get("VNX_STATE_DIR") or (vnx_data_dir / "state")).expanduser()),
        "VNX_DISPATCH_DIR": str(Path(os.environ.get("VNX_DISPATCH_DIR") or (vnx_data_dir / "dispatches")).expanduser()),
        "VNX_LOGS_DIR": str(Path(os.environ.get("VNX_LOGS_DIR") or (vnx_data_dir / "logs")).expanduser()),
        "VNX_PIDS_DIR": str(Path(os.environ.get("VNX_PIDS_DIR") or (vnx_data_dir / "pids")).expanduser()),
        "VNX_LOCKS_DIR": str(Path(os.environ.get("VNX_LOCKS_DIR") or (vnx_data_dir / "locks")).expanduser()),
        "VNX_SOCKETS_DIR": str(Path(os.environ.get("VNX_SOCKETS_DIR") or (vnx_data_dir / "sockets")).expanduser()),
        "VNX_REPORTS_DIR": str(Path(os.environ.get("VNX_REPORTS_DIR") or (vnx_data_dir / "unified_reports")).expanduser()),
        "VNX_DB_DIR": str(Path(os.environ.get("VNX_DB_DIR") or (vnx_data_dir / "database")).expanduser()),
    }

    reports_dir = Path(paths["VNX_REPORTS_DIR"])
    paths["VNX_HEADLESS_REPORTS_DIR"] = str(
        Path(os.environ.get("VNX_HEADLESS_REPORTS_DIR") or (reports_dir / "headless")).expanduser()
    )

    # Git-tracked intelligence directory (portable across worktrees)
    paths["VNX_INTELLIGENCE_DIR"] = str(
        Path(os.environ.get("VNX_INTELLIGENCE_DIR") or (canonical_root / ".vnx-intelligence")).expanduser().resolve()
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


def resolve_central_data_dir(project_id: str) -> Path:
    """Return ~/.vnx-data/<project_id> — the per-project namespace in the central store.

    Phase 6 P3: dual-write paths land here in addition to the per-project path.
    Per-project remains source-of-truth until w6-p5 reader cutover.

    Raises ValueError when project_id violates the VNX id format.
    """
    import re as _re
    _ID_RE = _re.compile(r"^[a-z][a-z0-9-]{1,31}$")
    if not _ID_RE.match(project_id):
        raise ValueError(
            f"invalid project_id {project_id!r}: must match {_ID_RE.pattern}"
        )
    return Path.home() / ".vnx-data" / project_id


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
