#!/usr/bin/env python3
"""Shared path resolver for VNX Python scripts.

Allows environment overrides while defaulting to dist/runtime-relative paths.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import warnings
from pathlib import Path
from typing import Dict, Optional

# Self-bootstrap: ensure scripts/lib is on sys.path so sibling imports work
# regardless of whether the caller set up the repo root or lib dir.
import sys as _sys
_lib = str(Path(__file__).resolve().parent)
if _lib not in _sys.path:
    _sys.path.insert(0, _lib)

# Single source of truth — do not redefine; import from vnx_ids.
from vnx_ids import PROJECT_ID_RE as _PROJECT_ID_RE

import data_dir_guard

log = logging.getLogger(__name__)


def _resolve_overrides_dir(project_root: Path) -> Optional[Path]:
    """Return project_root/.vnx-overrides if it exists as a directory, else None."""
    candidate = project_root / ".vnx-overrides"
    if candidate.is_dir():
        return candidate
    return None


def _resolve_packaged_vnx_home() -> Optional[Path]:
    """Resolve VNX_HOME for a pip-installed (site-packages) layout.

    In an installed wheel the engine ships under the ``vnx_orchestration``
    namespace package (PR-PIP-REPACKAGE), so this module sits at
    ``<site-packages>/vnx_orchestration/scripts/lib/vnx_paths.py`` with
    ``schemas/``, ``skills/``, etc. as siblings of ``scripts/`` inside
    ``vnx_orchestration/``. Because the walk is relative to this file's own
    location, the same three-parent walk also resolves a legacy top-level wheel
    (``<site-packages>/scripts/lib/vnx_paths.py``); the ``schemas/`` + ``scripts/``
    presence check confirms whichever layout produced the install.

    Returns None for a dev checkout or editable install so the existing
    ``__file__``-walk / git-based resolution stays in control. Detection keys on
    the module living under a ``site-packages``/``dist-packages`` root, which a
    source checkout never does.
    """
    here = Path(__file__).resolve()
    if not any(part in ("site-packages", "dist-packages") for part in here.parts):
        return None
    # scripts/lib/vnx_paths.py -> scripts/lib -> scripts -> engine root
    # (= <site-packages>/vnx_orchestration in a namespaced wheel).
    engine_root = here.parent.parent.parent
    if (engine_root / "schemas").is_dir() and (engine_root / "scripts").is_dir():
        return engine_root
    return None


def _resolve_vnx_home() -> Path:
    vnx_home = os.environ.get("VNX_HOME")
    if vnx_home:
        return Path(vnx_home).expanduser().resolve()

    vnx_bin = os.environ.get("VNX_BIN") or os.environ.get("VNX_EXECUTABLE")
    if vnx_bin:
        return Path(vnx_bin).expanduser().resolve().parent.parent

    # Packaged install: resolve the engine root from the installed layout
    # before falling back to the dev-checkout walk below.
    packaged = _resolve_packaged_vnx_home()
    if packaged is not None:
        return packaged

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


def _vnx_project_root_override(vnx_home: Path) -> Path | None:
    """Return the resolved VNX_PROJECT_ROOT override if it is a usable directory.

    VNX_PROJECT_ROOT is the explicit override exported by the central-install
    shim. It wins over any heuristic but is ignored when it points at VNX_HOME
    (mis-detection) or at a non-directory.
    """
    raw = os.environ.get("VNX_PROJECT_ROOT")
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if candidate.is_dir() and candidate != vnx_home:
        return candidate
    return None


def _is_central_install(vnx_home: Path) -> bool:
    """True when VNX_HOME is a standalone git repo serving as a central install.

    Central install = the VNX code tree is shared (e.g. ~/.vnx-system/versions/<v>)
    and the operator runs from their own project. Detected *only* via the
    ``.vnx-install-mode`` marker file (content ``central``) written by
    install-central.sh.

    The earlier CWD-git-root-mismatch heuristic was removed: a git worktree of
    vnx-orchestration itself produces a CWD git root that differs from VNX_HOME,
    which mis-fired the heuristic and collapsed PROJECT_ROOT onto the parent repo
    (issue #225 / PR-WAVE4-1 CI regression). The marker is unambiguous, so a
    standalone dev checkout or worktree is correctly treated as non-central.
    """
    if _git_toplevel(vnx_home) != vnx_home:
        return False
    marker = vnx_home / ".vnx-install-mode"
    if marker.is_file():
        try:
            return marker.read_text(encoding="utf-8").strip() == "central"
        except OSError:
            return False
    return False


def _default_project_root(vnx_home: Path) -> Path:
    if _is_embedded_layout(vnx_home):
        return vnx_home.parent.parent.resolve()

    # Explicit override exported by the central-install shim (belt-and-suspenders).
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return override

    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        if _is_central_install(vnx_home):
            cwd_git_root = _git_toplevel(Path.cwd())
            resolved = cwd_git_root if cwd_git_root else Path.cwd().resolve()
            # Safety: never collapse PROJECT_ROOT to filesystem root.
            if resolved == Path(resolved.anchor):
                return vnx_home.resolve()
            return resolved
        # Standalone dev checkout: runtime/bootstrap stay local to the repo checkout.
        return vnx_home.resolve()

    return vnx_home.parent.resolve()


def _default_canonical_root(vnx_home: Path) -> Path:
    if _is_embedded_layout(vnx_home):
        return vnx_home.resolve()

    # Explicit override: intelligence follows the project's git root.
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return _git_toplevel(override) or override

    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        if _is_central_install(vnx_home):
            project_root = _default_project_root(vnx_home)
            return _git_toplevel(project_root) or project_root
        return _git_common_root(vnx_home) or vnx_home.resolve()
    return vnx_home.resolve()


def _resolve_project_root(vnx_home: Path) -> Path:
    default_root = _default_project_root(vnx_home)

    # Explicit shim override takes precedence over inherited PROJECT_ROOT, so
    # direct Python callers honor it even when PROJECT_ROOT was not exported (EC-2).
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return override

    project_root_env = os.environ.get("PROJECT_ROOT")
    if project_root_env:
        candidate = Path(project_root_env).expanduser().resolve()
        if candidate == default_root:
            return candidate

    return default_root


def _project_id_from_marker(project_root: Path) -> Optional[str]:
    """Read a validated project_id from the nearest ``.vnx-project-id`` marker.

    Walks up from project_root (and honors the ``VNX_PROJECT_ID`` env-var first)
    looking for ``.vnx-project-id``; returns the validated first line. Unlike the
    full identity chain this needs no operator_id, so a freshly ``vnx init``-ed
    project (which writes only ``.vnx-project-id``) still resolves a project_id
    for state-root purposes. Returns None when no valid id is found.
    """
    env_pid = os.environ.get("VNX_PROJECT_ID")
    if env_pid and _PROJECT_ID_RE.match(env_pid.strip()):
        return env_pid.strip()
    try:
        start = Path(project_root).expanduser().resolve()
    except OSError:
        return None
    for ancestor in [start, *start.parents]:
        marker = ancestor / ".vnx-project-id"
        if not marker.is_file():
            continue
        try:
            first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return None
        if _PROJECT_ID_RE.match(first_line):
            return first_line
        return None
    return None


def _resolve_state_project_id(project_root: Path) -> Optional[str]:
    """Best-effort project_id for state-root resolution (never raises).

    Resolution order:
      1. Canonical identity chain via ``vnx_identity.try_resolve_identity``
         (env > .vnx-project-id file > registry; requires operator+project).
      2. Lenient ``.vnx-project-id`` marker / ``VNX_PROJECT_ID`` env lookup,
         which needs no operator_id — so a fresh ``vnx init`` project resolves.

    Returns None when no validated project_id is available, so
    _resolve_state_root applies its collision-safe project-local fallback
    instead of guessing a shared id.
    """
    try:
        from vnx_identity import try_resolve_identity
        identity = try_resolve_identity(cwd=project_root)
    except Exception:  # pragma: no cover - non-raising contract, belt-and-suspenders
        identity = None
    if identity is not None:
        pid = getattr(identity, "project_id", None)
        if pid and _PROJECT_ID_RE.match(pid):
            return pid
    return _project_id_from_marker(project_root)


def _resolve_state_root(project_id: Optional[str], project_root: Path) -> Path:
    """Resolve the VNX runtime data root (the ``.vnx-data`` equivalent).

    Ordered resolution — first applicable wins:
      1. ``VNX_DATA_DIR_EXPLICIT=1`` + ``VNX_DATA_DIR``  — explicit override
         (worktree isolation, CI, tests rely on this).
      2. ``VNX_DATA_HOME`` + project_id  — ``$VNX_DATA_HOME/<project_id>``.
      3. ``~/.vnx-data/<project_id>`` *if it already exists*  — keep resolving
         existing central installs to their current location.
      4. ``<project_root>/.vnx-data`` *if it already exists*  — keep resolving
         existing dev checkouts / pre-migration installs in place.
      5. XDG default  — ``${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>``
         for a fresh, clean-footprint install.

    The existence-gated legacy branches (3, 4) are checked *before* the XDG
    default so that the existing dev checkouts and central installs keep
    resolving to where their state already lives (per PR-PIP-2: "breek de
    bestaande dev-checkout/central resolutie NIET"). A fresh install has
    neither legacy dir and lands on the XDG user-data-dir.

    Collision-safety: a per-project directory is only ever formed from a
    *resolved* project_id. When project_id is None we never substitute a shared
    default id (which would collide every project into one dir); we fall back to
    the legacy project-local ``<project_root>/.vnx-data`` instead. No guessing.
    """
    # 1. Explicit override — highest precedence.
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR")
    if explicit_flag and explicit_val:
        return Path(explicit_val).expanduser().resolve()

    pid = project_id if (project_id and _PROJECT_ID_RE.match(project_id)) else None
    local = project_root / ".vnx-data"

    # 2. VNX_DATA_HOME — operator-chosen data home, per-project subdir.
    data_home = os.environ.get("VNX_DATA_HOME")
    if data_home and pid:
        return (Path(data_home).expanduser() / pid).resolve()

    # 3. Existing central install — keep resolving to ~/.vnx-data/<id>.
    if pid:
        central = Path.home() / ".vnx-data" / pid
        if central.is_dir():
            return central.resolve()

    # 4. Existing dev checkout / pre-migration install — keep project-local dir.
    if local.is_dir():
        return local.resolve()

    # 5. Fresh install (clean footprint): XDG user-data-dir.
    if pid:
        xdg_base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return (Path(xdg_base).expanduser() / "vnx" / pid).resolve()

    # Collision-safety: no resolvable project_id and no existing layout — never
    # guess a shared id. Stay project-local rather than collide projects.
    return local.resolve()


def resolve_data_root(project_root) -> Path:
    """Public: resolve the VNX runtime data root for an explicit project_root.

    Honors the same ordered resolution as :func:`resolve_paths` (explicit
    override > ``VNX_DATA_HOME`` > existing ``~/.vnx-data/<id>`` > existing
    project-local ``.vnx-data`` > XDG default), but anchored on the *given*
    project_root rather than the env/VNX_HOME-resolved one. The project_id is
    resolved leniently from that root (env, ``.vnx-project-id`` marker, or
    identity chain). Used by the pip console-script commands (``vnx_cli``)
    which operate on a ``--project-dir`` argument instead of the ambient repo.

    Collision-safe: an unresolvable project_id never collapses to a shared
    default; resolution falls back to the project-local ``.vnx-data`` instead.
    """
    project_root = Path(project_root).expanduser().resolve()
    pid = _resolve_state_project_id(project_root)
    data_dir = _resolve_state_root(pid, project_root)
    data_dir_guard.check_data_dir_project_id_guard(data_dir, pid)
    return data_dir


def resolve_paths() -> Dict[str, str]:
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)
    canonical_root = Path(
        os.environ.get("VNX_CANONICAL_ROOT") or _default_canonical_root(vnx_home)
    ).expanduser().resolve()

    _explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    _explicit_val = os.environ.get("VNX_DATA_DIR")
    if _explicit_val and not _explicit_flag:
        warnings.warn(
            f"VNX_DATA_DIR env-var set ({_explicit_val}) but "
            "VNX_DATA_DIR_EXPLICIT=1 is required for it to be honored. "
            "Ignoring and using the resolved state root. "
            "See https://github.com/Vinix24/vnx-orchestration/issues/225",
            DeprecationWarning,
            stacklevel=2,
        )
    _state_project_id = _resolve_state_project_id(project_root)
    vnx_data_dir = _resolve_state_root(_state_project_id, project_root)
    data_dir_guard.check_data_dir_project_id_guard(vnx_data_dir, _state_project_id)

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
        # Resolver order: .vnx-overrides/skills > .claude/skills > VNX_HOME/skills
        overrides_dir = _resolve_overrides_dir(project_root)
        overrides_skills = overrides_dir / "skills" if overrides_dir is not None else None
        if overrides_skills is not None and overrides_skills.is_dir():
            paths["VNX_SKILLS_DIR"] = str(overrides_skills)
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


def project_id_from_state_dir(state_dir: Path) -> str:
    """Best-effort derive a project_id from a state dir path.

    Supports both:
    - central paths: ``~/.vnx-data/<project_id>/state``
    - repo-local paths with a nearby ``.vnx-project-id`` file, such as
      ``<repo>/.vnx-data/state``

    Returns an empty string when no valid project_id can be derived.
    """
    try:
        resolved = Path(state_dir).expanduser().resolve()
    except Exception:
        return ""

    try:
        vnx_data = (Path.home() / ".vnx-data").resolve()
        if resolved.name == "state" and resolved.parent.parent == vnx_data:
            candidate = resolved.parent.name.strip()
            if _PROJECT_ID_RE.match(candidate):
                return candidate
    except OSError as e:
        log.debug("Failed to resolve vnx-data path: %s", e)

    for ancestor in [resolved, *resolved.parents]:
        project_file = ancestor / ".vnx-project-id"
        if not project_file.is_file():
            continue
        try:
            first_line = project_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""
        if _PROJECT_ID_RE.match(first_line):
            return first_line
        return ""

    return ""


def resolve_project_id() -> Optional[str]:
    """Return the resolved project_id for the current VNX context (best-effort, never raises).

    Resolution order: identity chain > VNX_PROJECT_ID env > .vnx-project-id marker > None.
    Used by nightly pipeline and CLI tools that need project_id without a full dispatch.
    ADR-007: project_id is required on all cross-project operations.
    """
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)
    return _resolve_state_project_id(project_root)


def resolve_state_dir(project_root: "Path | None" = None) -> Path:
    """Return the VNX state directory.

    When project_root is supplied, derives the state dir from that root
    (project_root / '.vnx-data' / 'state') without reading any env var.

    When project_root is None, returns VNX_STATE_DIR from resolve_paths().
    """
    if project_root is not None:
        return (Path(project_root) / ".vnx-data" / "state").resolve()
    paths = resolve_paths()
    return Path(paths["VNX_STATE_DIR"])


def resolve_worker_state_dir(terminal_id: str, vnx_data_dir: "Path | None" = None) -> Path:
    """Return ``.vnx-data/workers/<terminal_id>/`` — per-worker isolated state directory.

    Creates the directory on demand (exist_ok=True). When vnx_data_dir is None,
    derives it from resolve_paths()["VNX_DATA_DIR"].

    Raises:
        ValueError: if terminal_id is empty or contains path-traversal characters.
    """
    if not terminal_id or not terminal_id.strip():
        raise ValueError("terminal_id must be non-empty")
    clean = terminal_id.strip()
    if "/" in clean or "\\" in clean or ".." in clean:
        raise ValueError(
            f"terminal_id must not contain path separators or '..': {terminal_id!r}"
        )
    if vnx_data_dir is None:
        vnx_data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
    worker_dir = vnx_data_dir / "workers" / clean
    os.makedirs(worker_dir, exist_ok=True)
    return worker_dir.resolve()


def resolve_central_data_dir(project_id: str) -> Path:
    """Return ``~/.vnx-data/<project_id>/`` — the central per-project data directory.

    Used by Phase 6 P3 dual-write paths and the envelope re-stamper.

    Raises:
        ValueError: if project_id is empty or does not match ^[a-z][a-z0-9-]{1,31}$.
            Rejects dots, slashes, leading dashes, uppercase, and all special chars
            to prevent path-traversal escaping the ~/.vnx-data sandbox.
    """
    if not project_id:
        raise ValueError("project_id must be non-empty")
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"project_id must match ^[a-z][a-z0-9-]{{1,31}}$ "
            f"(no dots, slashes, leading dashes, or special chars): {project_id!r}"
        )
    return Path.home() / ".vnx-data" / project_id


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_skill_name(skill_name: str) -> str:
    """Validate skill_name is a safe bare name with no path traversal.

    Raises:
        ValueError: on empty string, path separators, dots, or any char
                    outside [A-Za-z0-9_-].
    """
    if not skill_name:
        raise ValueError(f"invalid skill name: {skill_name!r}")
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(f"invalid skill name: {skill_name!r}")
    return skill_name


def _confine_skill_path(resolved: Path, skill_root: Path) -> None:
    """Raise ValueError if resolved path escapes skill_root."""
    root_str = str(skill_root.resolve()) + os.sep
    if not str(resolved).startswith(root_str):
        raise ValueError(f"resolved path escapes skill root: {resolved}")


def get_skill_path(skill_name: str, project_root: Optional[Path] = None) -> Path:
    """Return the resolved Path for a named skill directory.

    Resolution order:
    1. project_root/.vnx-overrides/skills/<skill_name>/  (if project_root supplied)
    2. VNX_HOME/skills/<skill_name>/

    Raises:
        ValueError: if skill_name fails validation or resolved path escapes skill root.
        FileNotFoundError: if the skill directory is not found in any location.
    """
    skill_name = _validate_skill_name(skill_name)

    if project_root is not None:
        overrides_dir = _resolve_overrides_dir(Path(project_root))
        if overrides_dir is not None:
            skill_root = overrides_dir / "skills"
            override_skill = skill_root / skill_name
            resolved = override_skill.resolve()
            _confine_skill_path(resolved, skill_root)
            if override_skill.is_dir():
                return resolved

    vnx_home = _resolve_vnx_home()
    skill_root = vnx_home / "skills"
    central_skill = skill_root / skill_name
    resolved = central_skill.resolve()
    _confine_skill_path(resolved, skill_root)
    if central_skill.is_dir():
        return resolved

    raise FileNotFoundError(
        f"Skill {skill_name!r} not found in overrides or central VNX_HOME ({vnx_home})"
    )


if __name__ == "__main__":
    # Print resolved paths for quick diagnostics
    resolved = ensure_env()
    for key in sorted(resolved.keys()):
        print(f"{key}={resolved[key]}")
