#!/usr/bin/env python3
"""Engine bootstrap for the pip console-script commands.

The ``vnx`` console entry point (``vnx_cli.main``) ships *with* the full engine:
in an installed wheel ``scripts/`` is a top-level sibling of ``vnx_cli/`` (see
``[tool.setuptools.packages.find]`` in pyproject.toml), and in a dev checkout it
sits at ``<repo>/scripts``. Neither location is importable by default, so this
module puts ``scripts/`` and ``scripts/lib`` on ``sys.path`` and re-exports the
canonical path resolver. Keeping the bootstrap in one place means init, doctor
and status all resolve the state root identically (PR-PIP-2 lockstep).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional


# Validation is usable before engine bootstrap; derivation is shared lazily.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
PROJECT_FILE_NAME = ".vnx-project-id"


def engine_root() -> Path:
    """Return the engine root (the dir holding ``scripts/``, ``schemas/`` ...).

    In an installed wheel the engine ships under the ``vnx_orchestration``
    namespace package, so the trees live at ``<site-packages>/vnx_orchestration``
    — a sibling of the ``vnx_cli`` package, not site-packages itself. In a dev
    checkout the engine trees sit at the repo root, also a sibling of
    ``vnx_cli/``. Probe the packaged location first (PR-PIP-REPACKAGE) and fall
    back to the checkout layout; both are validated by the ``scripts/`` presence.

    NOTE: this resolver intentionally does NOT honor ``.vnx-version`` for engine
    selection. The pip-installed CLI and the pinned engine are API-coupled, so
    swapping the engine root based on a pin can crash commands that call new
    APIs against an old engine (e.g. ``deliver_via_door(deadline_seconds=...)``).
    The pin is instead honored by RE-EXEC'ing the pinned install as a whole
    (its ``vnx_cli`` + its engine) at startup — see
    ``vnx_cli/_reexec.py`` (design-track ``pip-cli-honor-pin-via-reexec``).
    This in-process resolver keeps using its own engine.
    """
    parent = Path(__file__).resolve().parent.parent
    packaged = parent / "vnx_orchestration"
    if (packaged / "scripts").is_dir():
        return packaged
    return parent


def ensure_engine_on_path() -> Path:
    """Put the packaged engine's ``scripts/``, ``scripts/lib``, and
    ``scripts/dream`` on sys.path.

    Idempotent. Returns the engine root so callers can reuse it.
    """
    root = engine_root()
    for sub in (root / "scripts", root / "scripts" / "lib", root / "scripts" / "dream"):
        s = str(sub)
        if sub.is_dir() and s not in sys.path:
            sys.path.insert(0, s)
    return root


def is_packaged_install(root: Optional[Path] = None) -> bool:
    """True when the engine is loaded from an installed (site/dist-packages) tree."""
    root = root or engine_root()
    s = str(root)
    return "site-packages" in s or "dist-packages" in s


def resolve_data_root(project_dir) -> Path:
    """Resolve the VNX runtime data root for ``project_dir`` via vnx_paths.

    Thin wrapper that bootstraps the engine path first, so every CLI command
    shares the ordered resolver (explicit > VNX_DATA_HOME > existing
    ~/.vnx-data/<id> > existing project-local > fresh default).
    """
    ensure_engine_on_path()
    from vnx_paths import resolve_data_root as _resolve_data_root

    return _resolve_data_root(Path(project_dir))


class ProjectStoreError(ValueError):
    """An explicit --project-id cannot be served without reading another project's store."""


def resolve_project_state_dir(
    project_dir,
    project_id: Optional[str] = None,
    project_dir_explicit: bool = False,
) -> Path:
    """State dir for a CLI command, honouring an explicit ``--project-id`` (ADR-007).

    Without ``project_id`` this is the old behaviour: the store of ``project_dir``.
    With one, the store is that project's, resolved through the supported chain
    (``vnx_paths.resolve_project_data_root``: explicit override, ``VNX_DATA_HOME``,
    ``~/.vnx-data/<id>``), never the store of the directory the command runs in.

    Refusals, each naming both projects (``ProjectStoreError``):
      * ``project_dir_explicit`` and that directory belongs to another project;
      * a configured override store (``VNX_DATA_DIR``) that is the central or
        ``VNX_DATA_HOME`` store of another project.
    The default ``--project-dir .`` is never refused: it only tells where the
    command was started.
    """
    root = Path(project_dir).expanduser().resolve()
    if not project_id:
        return resolve_data_root(root) / "state"
    ensure_engine_on_path()
    import vnx_paths

    if not _PROJECT_ID_RE.match(project_id):
        raise ProjectStoreError(
            f"invalid --project-id {project_id!r}: must match {_PROJECT_ID_RE.pattern}"
        )
    # The directory's own identity only (marker, git remote): an ambient
    # VNX_PROJECT_ID must not make a directory "belong" to a project it does not.
    dir_pid = dir_project_id(root)
    if project_dir_explicit and dir_pid and dir_pid != project_id:
        raise ProjectStoreError(
            f"--project-id {project_id!r} does not match --project-dir {str(root)!r}, "
            f"which belongs to project {dir_pid!r}; refusing to pick a store"
        )
    store = vnx_paths.resolve_project_data_root(
        project_id, root if dir_pid == project_id else None
    )
    owner = _override_owner(store)
    if owner and owner != project_id:
        raise ProjectStoreError(
            f"--project-id {project_id!r} but the configured store {str(store)!r} "
            f"belongs to project {owner!r}; refusing to read it"
        )
    return store / "state"


def dir_project_id(root) -> Optional[str]:
    """Project a directory belongs to by its own signals, in the resolver's order.

    Nearest ``.vnx-project-id`` marker, then the operator registry
    (``~/.vnx/projects.json``, read by ``vnx_identity``'s own reader), then the git
    remote. Ambient ``VNX_PROJECT_ID`` is deliberately not consulted. None when
    the directory carries no identity.
    """
    root = Path(root).expanduser().resolve()
    for ancestor in (root, *root.parents):
        marker_pid = read_marker_project_id(ancestor)
        if marker_pid:
            return marker_pid
    ensure_engine_on_path()
    import vnx_identity
    import vnx_paths

    registry = vnx_identity._load_registry(vnx_identity.REGISTRY_PATH)
    if registry is not None:
        entry = vnx_identity._registry_lookup(registry, root)
        registered = entry.get("project_id") if entry else None
        if registered and _PROJECT_ID_RE.match(registered):
            return registered
    return vnx_paths._project_id_from_git_remote(root)


def require_source_project(project_id: str, sources) -> None:
    """Refuse when a repository source belongs to another project than ``project_id``.

    ``sources`` are the directories a command reads from (project dir, repo root,
    roadmap location). A source whose identity cannot be established passes: it
    is unattributable and the operator's explicit ``--project-id`` stands.
    """
    for source in sources:
        owner = dir_project_id(source)
        if owner and owner != project_id:
            raise ProjectStoreError(
                f"--project-id {project_id!r} but the repository source "
                f"{str(Path(source).expanduser().resolve())!r} belongs to project {owner!r}; "
                "refusing to write its content into another project's store"
            )


def _override_owner(store: Path) -> Optional[str]:
    """Project that a configured ``VNX_DATA_DIR`` override names, else None.

    Only a store directly under ``~/.vnx-data`` or ``$VNX_DATA_HOME`` names its
    owner (the directory name is the project_id there). Any other path carries
    no identity and is taken as the operator's choice.
    """
    import os

    if os.environ.get("VNX_DATA_DIR_EXPLICIT") != "1" or not os.environ.get("VNX_DATA_DIR"):
        return None
    parents = {(Path.home() / ".vnx-data").resolve()}
    if os.environ.get("VNX_DATA_HOME"):
        parents.add(Path(os.environ["VNX_DATA_HOME"]).expanduser().resolve())
    if store.parent in parents and _PROJECT_ID_RE.match(store.name):
        return store.name
    return None


def read_marker_project_id(project_dir: Path) -> Optional[str]:
    """Return the validated project_id from ``project_dir/.vnx-project-id``.

    Reads only the first line (the marker may carry orchestrator/agent ids on
    later lines). Returns None when missing or invalid.
    """
    marker = Path(project_dir) / PROJECT_FILE_NAME
    if not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return first if _PROJECT_ID_RE.match(first) else None


def slugify_project_id(name: str) -> Optional[str]:
    """Delegate to the shared derivation rule in the packaged engine."""
    ensure_engine_on_path()
    from project_id_derivation import slugify_project_id as slugify
    return slugify(name)


def derive_project_id(project_dir, explicit: Optional[str] = None) -> str:
    """Resolve explicit > existing marker > basename slug > path hash."""
    ensure_engine_on_path()
    from project_id_derivation import derive_project_id as derive
    marker = read_marker_project_id(project_dir) if explicit is None else None
    return derive(project_dir, explicit=explicit, marker=marker)
