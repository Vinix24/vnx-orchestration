"""Project-root resolver for VNX scripts.

Prefers git-based resolution over env vars to prevent cross-project
state pollution. See upstream-fix issue Vinix24/vnx-orchestration#225.
"""
from __future__ import annotations

import os
import subprocess
import warnings
from pathlib import Path


def resolve_project_root(caller_file: str | None = None) -> Path:
    """Resolve project root for the calling script.

    Resolution order:
      1. git rev-parse from caller's physical location (follows symlinks)
      2. git rev-parse from current working directory
      3. VNX_CANONICAL_ROOT env var (DeprecationWarning)
      4. Raise RuntimeError

    Args:
        caller_file: __file__ of calling script (recommended).
                     Used as the starting point for git resolution after
                     symlink-resolving via Path.resolve().
    """
    candidates: list[Path] = []
    if caller_file:
        candidates.append(Path(caller_file).resolve().parent)
    candidates.append(Path.cwd().resolve())

    for start in candidates:
        try:
            out = subprocess.check_output(
                ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if out:
                return Path(out).resolve()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue

    env_root = os.environ.get("VNX_CANONICAL_ROOT")
    if env_root:
        warnings.warn(
            f"VNX_CANONICAL_ROOT env-var used ({env_root}). "
            "Prefer git-based resolution. This fallback will be removed "
            "in vnx-orchestration v0.10.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return Path(env_root).resolve()

    raise RuntimeError(
        "Cannot resolve project root. Not in a git repo and "
        "VNX_CANONICAL_ROOT is not set. "
        "See https://github.com/Vinix24/vnx-orchestration/issues/225"
    )


def resolve_data_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_DATA_DIR: $PROJECT_ROOT/.vnx-data by default.

    Explicit override via VNX_DATA_DIR is honored ONLY when
    VNX_DATA_DIR_EXPLICIT=1. Otherwise the env var is ignored to prevent
    cross-project state pollution from inherited shell environments.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR")
    if explicit_flag and explicit_val:
        return Path(explicit_val).resolve()

    if explicit_val and not explicit_flag:
        warnings.warn(
            f"VNX_DATA_DIR env-var set ({explicit_val}) but "
            "VNX_DATA_DIR_EXPLICIT=1 is required for it to be honored. "
            "Ignoring and using git-resolved project root. "
            "See https://github.com/Vinix24/vnx-orchestration/issues/225",
            DeprecationWarning,
            stacklevel=2,
        )

    root = resolve_project_root(caller_file)
    return root / ".vnx-data"


def resolve_state_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_STATE_DIR: $VNX_DATA_DIR/state by default."""
    data = resolve_data_dir(caller_file)
    return data / "state"


def resolve_dispatch_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_DISPATCH_DIR: $VNX_DATA_DIR/dispatches by default."""
    data = resolve_data_dir(caller_file)
    return data / "dispatches"
