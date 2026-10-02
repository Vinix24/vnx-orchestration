#!/usr/bin/env python3
"""Which GitHub repo a gate verdict belongs to — derived from the store it was written to.

A verdict is written into a project's store (``<data_dir>/state/review_gates/
results/pr-<n>-<gate>.json``). That store, and nothing about where this code
happens to run, says which project the verdict is about. The chain is:

    results dir -> state dir -> project_id -> registered checkout -> origin

Every link is cwd-independent and ``__file__``-independent on purpose: the
engine's own checkout (a central install, or the dev checkout) is a different
repo from the project's, and a verdict published there lands on the wrong PR.

The checkout is accepted only when its own ``.vnx-project-id`` marker equals the
store's id. The marker is read straight from the file, never through
``vnx_paths._project_id_from_marker``, which honours an ambient
``VNX_PROJECT_ID`` first.

No source of the answer is a fallback: not the cwd, not ``__file__``, not
``vnx_paths.resolve_paths()["PROJECT_ROOT"]``. When the project cannot be
established the caller gets :class:`ForgePublishRefused` and nothing is sent.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from chain_origin_anchor import _owner_repo_from_remote
from forge_check_run import ForgePublishRefused
from merge_target import is_central_install
from vnx_ids import PROJECT_ID_RE

PROJECT_MARKER = ".vnx-project-id"

REGISTER_HINT = (
    "registreer het project in ~/.vnx/projects.json (vnx init in de checkout) of zet "
    "VNX_PROJECT_ROOT op de checkout van dit project"
)


@dataclass(frozen=True)
class ForgeTarget:
    """The project a verdict is published for: its checkout, id and GitHub repo."""

    project_id: str
    project_root: Path
    owner_repo: str


def project_checkout_path(project_id: str) -> Optional[Path]:
    """Resolve the project's checkout path from the operator registry.

    ``~/.vnx/projects.json`` (vnx_identity schema v2) maps ``project_id`` →
    ``path``. This is the cwd-independent link from a central-install runner's
    store (``~/.vnx-data/<project_id>/state``) back to the actual checkout whose
    ``origin`` remote is a real GitHub URL. Returns None when the id is not
    registered or the path is gone.
    """
    if not project_id:
        return None
    try:
        registry_path = Path.home() / ".vnx" / "projects.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(registry, dict):
        return None
    for entry in registry.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("project_id") != project_id:
            continue
        raw_path = entry.get("path")
        if not raw_path:
            continue
        try:
            candidate = Path(raw_path).expanduser()
        except (OSError, ValueError):
            continue
        if candidate.is_dir():
            return candidate
    return None


def checkout_project_id(checkout: Path) -> str:
    """The id in ``<checkout>/.vnx-project-id`` (first line), or "" when absent/invalid."""
    marker = Path(checkout) / PROJECT_MARKER
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    first = (lines[0] if lines else "").strip()
    return first if PROJECT_ID_RE.match(first) else ""


def _store_project_id(state_dir: Path) -> str:
    import vnx_paths

    return vnx_paths.project_id_from_state_dir(state_dir)


def _repo_local_checkout(state_dir: Path) -> Optional[Path]:
    """The checkout that contains a repo-local store (``<checkout>/.vnx-data/state``)."""
    try:
        resolved = Path(state_dir).expanduser().resolve()
    except OSError:
        return None
    if resolved.name != "state" or resolved.parent.name != ".vnx-data":
        return None
    checkout = resolved.parent.parent
    return checkout if checkout.is_dir() else None


def _refuse(why: str) -> ForgePublishRefused:
    return ForgePublishRefused(
        f"{why} — er is niets gepubliceerd en het record is ongewijzigd. Herstel: {REGISTER_HINT}."
    )


def resolve_forge_target(results_dir: Path) -> ForgeTarget:
    """The project a record in ``results_dir`` is about, or :class:`ForgePublishRefused`.

    ``results_dir`` is ``<state>/review_gates/results``; the store is two levels
    up. Refuses when the store yields no project id, no checkout carries that
    id, the checkout is a central install, or its origin is not a GitHub remote.
    """
    results = Path(results_dir)
    state_dir = results.parent.parent
    project_id = _store_project_id(state_dir)
    if not project_id:
        raise _refuse(
            f"het project van de store {state_dir} is niet vast te stellen "
            "(geen ~/.vnx-data/<project_id>/state en geen .vnx-project-id)"
        )

    candidates: list[tuple[str, Path]] = []
    registered = project_checkout_path(project_id)
    if registered is not None:
        candidates.append(("projects.json", registered))
    local = _repo_local_checkout(state_dir)
    if local is not None:
        candidates.append(("repo-lokale store", local))
    explicit = (os.environ.get("VNX_PROJECT_ROOT") or "").strip()
    if explicit:
        candidates.append(("VNX_PROJECT_ROOT", Path(explicit).expanduser()))

    mismatches: list[str] = []
    checkout: Optional[Path] = None
    for source, candidate in candidates:
        found = checkout_project_id(candidate)
        if found == project_id:
            checkout = candidate
            break
        mismatches.append(f"{source}: {candidate} draagt id {found or '<geen>'}")
    if checkout is None:
        detail = "; ".join(mismatches) if mismatches else "geen geregistreerde checkout"
        raise _refuse(
            f"geen checkout gevonden met .vnx-project-id '{project_id}' voor store {state_dir} "
            f"({detail})"
        )

    if is_central_install(checkout):
        raise _refuse(
            f"de checkout {checkout} van project '{project_id}' is een centrale installatie, "
            "geen project"
        )
    owner_repo = _owner_repo_from_remote(checkout)
    if not owner_repo:
        raise _refuse(
            f"de git-remote 'origin' van {checkout} is geen GitHub-remote "
            f"(project '{project_id}')"
        )
    return ForgeTarget(project_id=project_id, project_root=checkout, owner_repo=owner_repo)
