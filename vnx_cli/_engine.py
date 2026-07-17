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

import hashlib
import re
import sys
from pathlib import Path
from typing import Optional

# Mirror of vnx_ids.PROJECT_ID_RE / vnx_paths PROJECT_ID_RE. Duplicated here so a
# project_id can be validated before the engine path is bootstrapped; the engine
# regex is the single source of truth and is asserted identical in tests.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# Mirror of the char-class check in templates/vnx_shim.sh.tpl's pin validation
# (`[[ "$pin" =~ ^[A-Za-z0-9._-]+$ ]]`). Keeping this identical means a pin the
# bash shim accepts is also accepted here, and vice versa.
VERSION_PIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_VNX_VERSION_FILENAME = ".vnx-version"


def find_version_pin(start: Optional[Path] = None) -> Optional[str]:
    """Traverse upward from ``start`` (default: cwd) for a ``.vnx-version`` pin.

    Mirrors ``templates/vnx_shim.sh.tpl``'s ``find_version_pin`` so the
    pip-installed CLI and a central install's bash shim agree on which pin
    file governs a given invocation: walk up one directory at a time until a
    ``.vnx-version`` file is found or the filesystem root is reached. Returns
    the first line, stripped, or None if no pin file exists or it's empty.
    """
    directory = (start or Path.cwd()).resolve()
    while True:
        candidate = directory / _VNX_VERSION_FILENAME
        if candidate.is_file():
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except OSError:
                return None
            pin = lines[0].strip() if lines else ""
            return pin or None
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def _pinned_engine_root() -> Optional[Path]:
    """Resolve a ``.vnx-version`` pin to an installed central-install version tree.

    Returns ``~/.vnx-system/versions/<pin>`` when: a pin is found upward from
    cwd; the pin passes the bash shim's char-class check; the resolved path
    stays inside the versions root (defends against a ``..`` pin, which the
    char-class alone permits); and that version is actually installed locally
    (has a ``scripts/`` dir). Returns None in every other case so
    ``engine_root()`` falls back to its existing sibling-of-``vnx_cli``
    resolution — a pin that isn't centrally installed on this machine must not
    break the pip CLI.
    """
    pin = find_version_pin()
    if not pin or not VERSION_PIN_RE.match(pin):
        return None
    versions_root = (Path.home() / ".vnx-system" / "versions").resolve()
    pinned_root = (versions_root / pin).resolve()
    if pinned_root != versions_root and versions_root not in pinned_root.parents:
        return None
    if not (pinned_root / "scripts").is_dir():
        return None
    return pinned_root


def engine_root() -> Path:
    """Return the engine root (the dir holding ``scripts/``, ``schemas/`` ...).

    Honors a ``.vnx-version`` pin first: if one is found upward from cwd and
    resolves to an installed ``~/.vnx-system/versions/<pin>`` tree, that tree
    is the engine root — this is what makes the pin consequential for the
    pip-installed CLI, not just the bash shim (see ``_pinned_engine_root``).

    Otherwise falls back to the unpinned default: in an installed wheel the
    engine ships under the ``vnx_orchestration`` namespace package, so the
    trees live at ``<site-packages>/vnx_orchestration`` — a sibling of the
    ``vnx_cli`` package, not site-packages itself. In a dev checkout the
    engine trees sit at the repo root, also a sibling of ``vnx_cli/``. Probe
    the packaged location first (PR-PIP-REPACKAGE) and fall back to the
    checkout layout; both are validated by the ``scripts/`` presence.
    """
    pinned = _pinned_engine_root()
    if pinned is not None:
        return pinned

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
    ~/.vnx-data/<id> > existing project-local > XDG default).
    """
    ensure_engine_on_path()
    from vnx_paths import resolve_data_root as _resolve_data_root

    return _resolve_data_root(Path(project_dir))


def slugify_project_id(name: str) -> Optional[str]:
    """Derive a valid project_id (``^[a-z][a-z0-9-]{1,31}$``) from a free name.

    Lowercases, maps any run of non ``[a-z0-9]`` to a single ``-``, strips
    leading characters until the first ascii letter, trims trailing ``-`` and
    truncates to 32 chars. Returns None when nothing valid can be derived (e.g.
    a name with no ascii letters) — callers must then refuse to guess a shared
    id (PR-PIP-2 collision-safety) rather than substitute a default.
    """
    lowered = (name or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    # Must start with a letter; drop any leading digits/hyphens.
    collapsed = re.sub(r"^[^a-z]+", "", collapsed)
    collapsed = collapsed.strip("-")[:32].rstrip("-")
    if collapsed and _PROJECT_ID_RE.match(collapsed):
        return collapsed
    return None


PROJECT_FILE_NAME = ".vnx-project-id"


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


def derive_project_id(project_dir, explicit: Optional[str] = None) -> str:
    """Resolve a valid, collision-safe project_id for a project directory.

    Order: explicit (validated) > existing ``.vnx-project-id`` marker > slug of
    the directory basename > a deterministic ``vnx-<8 hex>`` derived from the
    absolute path. The last fallback is *path-unique* (not a shared default), so
    a directory whose name carries no usable letters still gets its own state
    dir instead of colliding with other projects — PR-PIP-2 collision-safety.

    Raises ValueError if ``explicit`` is given but invalid, so a typo surfaces
    instead of being silently slugified.
    """
    project_dir = Path(project_dir)
    if explicit is not None:
        if not _PROJECT_ID_RE.match(explicit):
            raise ValueError(
                f"invalid --project-id {explicit!r}: must match {_PROJECT_ID_RE.pattern}"
            )
        return explicit
    existing = read_marker_project_id(project_dir)
    if existing:
        return existing
    slug = slugify_project_id(project_dir.resolve().name)
    if slug:
        return slug
    digest = hashlib.sha1(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"vnx-{digest}"
