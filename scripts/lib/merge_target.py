#!/usr/bin/env python3
"""Which repo the merge door merges into, and what the door proves itself against
(OI-1849).

``scripts/pr_merge.py`` answers two different questions with two different repos,
and until OI-1849 it answered both with the one it happened to run from:

  1. **The target**: the repo of the PROJECT whose PR is being merged. Every
     check that judges the merge (CI run, ADR numbers, branch protection, the
     ``gh pr merge`` itself) belongs to this repo.
  2. **The door**: the fabric code that is doing the judging. Its integrity is
     proven against the fabric's own repo, never the target's.

From a fabric checkout the two are the same repo and nothing changes. From a
central install (``~/.vnx-system/versions/<v>``, a git clone of the fabric with
``.vnx-install-mode`` = ``central``) they are different repos, and the door used
the install's git remote for everything: a consumer merge was judged against
``Vinix24/vnx-orchestration``, and a drift check of that repo passed for a merge
into another one.

The project root comes from ``vnx_paths.resolve_paths`` (``VNX_PROJECT_ROOT`` if
set, else the git toplevel of the cwd for a central install), the resolver every
other VNX script already uses; nothing here resolves a root itself. What this
module adds is the repo name (``gh repo view`` run IN that root, so it is the
same repo ``gh`` resolves for the ``{owner}/{repo}`` placeholders of every call
the door makes there) and the reference the door is compared against.

The door's own integrity is on this module's list (``pr_merge._DOOR_INTEGRITY_PATHS``):
a target resolver that could be edited would point every later check at a
friendlier repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from vnx_paths import resolve_paths

#: Written by ``install-central.sh`` into a version directory. The same marker
#: ``vnx_paths._is_central_install`` and ``bin/vnx`` read.
INSTALL_MODE_MARKER = ".vnx-install-mode"
CENTRAL_INSTALL_MODE = "central"

#: A dev checkout is compared against the tip of the fabric's main.
DOOR_REFERENCE_DEV = "main"

GH_REPO_VIEW_TIMEOUT = 15

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class MergeTargetError(RuntimeError):
    """The merge target, or the door's reference, could not be established.

    Always a refusal upstream: a door that cannot say which repo it is judging
    must not judge.
    """


@dataclass(frozen=True)
class MergeTarget:
    """The project a merge goes into: where its git/gh calls run and which repo that is."""

    project_root: Path
    repo: str


def is_central_install(engine_root: Path) -> bool:
    """True when ``engine_root`` carries the central-install marker."""
    marker = Path(engine_root) / INSTALL_MODE_MARKER
    if not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == CENTRAL_INSTALL_MODE
    except OSError:
        return False


def resolve_target_repo(project_root: Path, *, gh_bin: str = "gh") -> str:
    """``owner/name`` of the repo ``gh`` resolves inside ``project_root``."""
    try:
        proc = subprocess.run(
            [gh_bin, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=str(project_root), capture_output=True, text=True, timeout=GH_REPO_VIEW_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise MergeTargetError(f"gh CLI niet beschikbaar: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MergeTargetError(
            f"gh repo view liep vast na {GH_REPO_VIEW_TIMEOUT}s in {project_root}"
        ) from exc
    if proc.returncode != 0:
        raise MergeTargetError(
            f"gh repo view faalde in {project_root} (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    slug = (proc.stdout or "").strip()
    if not _REPO_SLUG_RE.match(slug):
        raise MergeTargetError(f"gh repo view gaf geen owner/name in {project_root}: {slug!r}")
    return slug


def ensure_project_is_not_the_install(engine_root: Path, project_root: Path) -> None:
    """Refuse a central install as its own target.

    When the project root resolves to the install (a cwd that is not inside any
    project), acting on "the project" would act on the fabric's repo for a run
    the operator meant elsewhere: the door would judge the install's repo for a
    merge into a project, and ``apply_branch_protection.py`` would apply the
    install's YAML to whichever repo the install's remote names. Both call this,
    so the rule is one test and not two.
    """
    if is_central_install(engine_root) and Path(project_root).resolve() == Path(engine_root).resolve():
        raise MergeTargetError(
            f"het doel is de installatie zelf ({Path(project_root).resolve()}), geen project: start dit "
            "vanuit de map van het project, of zet VNX_PROJECT_ROOT"
        )


def _explicit_project_root() -> Path | None:
    """``VNX_PROJECT_ROOT`` resolved, when the operator set it; else ``None``."""
    raw = os.environ.get("VNX_PROJECT_ROOT")
    return Path(raw).expanduser().resolve() if raw else None


def resolve_project_root(engine_root: Path) -> Path:
    """The project a run of ``engine_root``'s code acts on, or a refusal.

    ``vnx_paths`` derives the project root from ``VNX_HOME`` when the
    environment sets it, not from the code that is running. A ``VNX_HOME`` left
    pointing at another checkout (a shell that exported it for the fabric,
    measured in the 2026-09-25 review of #1916) then makes a door run from an
    install judge and merge in THAT checkout's repo, and the install check below
    does not fire because the install is not the project. So a ``VNX_HOME`` that
    is not the running code refuses, unless ``VNX_PROJECT_ROOT`` names the
    project: that is the explicit way to point a door at a project, and it wins
    over ``VNX_HOME`` in ``vnx_paths`` too. Refusing rather than re-resolving
    with ``engine_root`` as the home keeps one resolver: the state dir the door
    writes its records to comes from the same ``resolve_paths``, and a second
    resolution would let the two disagree. The ``vnx`` shim unsets
    ``VNX_HOME`` before it resolves, so it never meets this refusal.
    """
    paths = resolve_paths()
    project_root = Path(paths["PROJECT_ROOT"]).resolve()
    vnx_home = Path(paths["VNX_HOME"]).resolve()
    if vnx_home != Path(engine_root).resolve() and _explicit_project_root() != project_root:
        raise MergeTargetError(
            f"VNX_HOME wijst naar {vnx_home}, maar de draaiende code staat in "
            f"{Path(engine_root).resolve()}: het project zou dan uit een andere checkout komen "
            f"({project_root}). Zet VNX_PROJECT_ROOT op het project, of haal VNX_HOME weg"
        )
    ensure_project_is_not_the_install(engine_root, project_root)
    return project_root


def resolve_merge_target(engine_root: Path, *, gh_bin: str = "gh") -> MergeTarget:
    """The project this merge goes into.

    ``engine_root`` is the root of the running door. The project root comes from
    :func:`resolve_project_root`: ``VNX_HOME`` must be the running door unless
    ``VNX_PROJECT_ROOT`` names the project, and a central install must not be
    its own target.
    """
    project_root = resolve_project_root(engine_root)
    return MergeTarget(project_root=project_root, repo=resolve_target_repo(project_root, gh_bin=gh_bin))


def read_install_version(engine_root: Path) -> str:
    """The version an install claims to be, from its ``VERSION`` file."""
    version_file = Path(engine_root) / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MergeTargetError(f"VERSION van de installatie onleesbaar ({version_file}): {exc}") from exc
    if not version:
        raise MergeTargetError(f"VERSION van de installatie is leeg ({version_file})")
    return version


def door_reference(engine_root: Path) -> Tuple[str, str]:
    """``(git ref, label)`` the running door must be byte-identical to.

    A dev checkout is compared against ``main``: the door only runs from the
    checkout on main. An install is a release, and main has moved on since its
    tag was cut, so comparing it to main would refuse every install that is not
    on the very latest commit. It is compared to the tag it says it is
    (``v<VERSION>``) in the fabric repo it was cloned from: identical to a
    published release, or refused.
    """
    if not is_central_install(engine_root):
        return DOOR_REFERENCE_DEV, DOOR_REFERENCE_DEV
    tag = f"v{read_install_version(engine_root)}"
    return tag, f"release {tag}"
