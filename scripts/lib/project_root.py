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


def _run_explicit_data_dir_guard(resolved: Path) -> None:
    """Run the data-dir/project-id guard on an explicitly pinned data-dir.

    Signal only: this never changes the returned path. In the default
    (``warn``) mode it emits ``VNXDataDirMismatchWarning`` when the pinned
    dir does not belong to the active project_id; ``enforce`` raises and
    ``off`` skips (see ``scripts/lib/data_dir_guard.py``).

    Only the explicit branch (VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1) is
    guarded — that is the branch the fleet-wide mitigation tells every
    project to use, and the one that was measured silently accepting a
    foreign project's data root (OI-900). The default branch is
    deliberately NOT guarded: it returns the repo-local
    ``root / ".vnx-data"``, which is legitimately not under
    ``~/.vnx-data/<project_id>``, so guarding it would flag every ordinary
    in-repo resolution in every checkout as a mismatch and flood normal
    use with warnings.

    Deferred import: ``data_dir_guard`` imports ``project_root`` at module
    level, so a module-level import here would create an import cycle.
    ``project_root`` is the lowest-level path module in the repo and must
    stay importable standalone, so a missing guard module fails open.
    """
    try:
        import data_dir_guard
    except ImportError:  # vnx-silent-except: guard is advisory-only; project_root must stay importable when scripts/lib is not on sys.path
        return
    # project_id=None: the guard resolves the ambient id itself and treats
    # an unresolvable id as "cannot verify" (silent), per its contract.
    data_dir_guard.check_data_dir_project_id_guard(resolved, None)


def resolve_data_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_DATA_DIR: $PROJECT_ROOT/.vnx-data by default.

    Explicit override via VNX_DATA_DIR is honored ONLY when
    VNX_DATA_DIR_EXPLICIT=1. Otherwise the env var is ignored to prevent
    cross-project state pollution from inherited shell environments.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR")
    if explicit_flag and explicit_val:
        resolved = Path(explicit_val).resolve()
        # Guard fires here, once per resolution; resolve_state_dir /
        # resolve_dispatch_dir call resolve_data_dir and add no signal of
        # their own, so one resolution produces at most one guard signal.
        _run_explicit_data_dir_guard(resolved)
        return resolved

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


def resolve_central_data_dir(project_id: str) -> Path:
    """Resolve the CENTRAL VNX data directory for a project.

    Returns $HOME/.vnx-data/<project_id> — the same root watched by the
    receipt processor and subprocess_dispatch.py (consistent with ADR-007).
    Distinct from resolve_data_dir() which returns the local
    $PROJECT_ROOT/.vnx-data for the current git worktree.
    """
    return Path.home() / ".vnx-data" / project_id


def resolve_state_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_STATE_DIR: $VNX_DATA_DIR/state by default."""
    data = resolve_data_dir(caller_file)
    return data / "state"


def resolve_dispatch_dir(caller_file: str | None = None) -> Path:
    """Resolve VNX_DISPATCH_DIR: $VNX_DATA_DIR/dispatches by default."""
    data = resolve_data_dir(caller_file)
    return data / "dispatches"


def resolve_project_id(project_dir: str | Path | None = None) -> str:
    """Resolve the current project_id for tenant-scoped operations.

    Resolution order:
      1. VNX_PROJECT_ID env var (explicit override)
      2. .vnx-project-id file in project dir or CWD (canonical per ADR-007)
      3. git remote 'origin' URL → last path component, stripped of .git suffix
      4. Raise RuntimeError — no silent default (ADR-007)

    Raises RuntimeError if project_id cannot be determined.
    """
    env_val = os.environ.get("VNX_PROJECT_ID")
    if env_val:
        return env_val.strip()

    start_dirs: list[Path] = []
    if project_dir is not None:
        start_dirs.append(Path(project_dir).resolve())
    start_dirs.append(Path.cwd().resolve())

    # Priority 2: .vnx-project-id marker file (same contract as vnx_paths.py)
    for start in start_dirs:
        for ancestor in [start, *start.parents]:
            marker = ancestor / ".vnx-project-id"
            if not marker.is_file():
                continue
            try:
                first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
                if first_line:
                    return first_line
            except (OSError, IndexError):
                pass
            break  # found marker but couldn't read it — don't keep walking

    # Priority 3: git remote URL → repo name
    for start in start_dirs:
        try:
            out = subprocess.check_output(
                ["git", "-C", str(start), "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if out:
                name = out.rstrip("/").split("/")[-1]
                if name.endswith(".git"):
                    name = name[:-4]
                if name:
                    return name
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue

    raise RuntimeError(
        "Cannot resolve project_id. Set VNX_PROJECT_ID env var, add a "
        ".vnx-project-id file in the project root, or run from a git "
        "repository with a configured 'origin' remote. "
        "No silent default — explicit project_id required (ADR-007)."
    )
