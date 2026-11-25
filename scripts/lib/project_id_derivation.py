"""Shared project identity derivation for source and pip initialization."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


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


def derive_project_id(project_dir, explicit: Optional[str] = None, *, marker: Optional[str] = None) -> str:
    """Resolve a valid, collision-safe project_id for a project directory.

    Order: explicit (validated) > caller-read ``.vnx-project-id`` marker > slug of
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
    if marker is not None:
        if not _PROJECT_ID_RE.fullmatch(marker):
            raise ValueError(f"invalid project marker: {marker!r}")
        return marker
    slug = slugify_project_id(project_dir.resolve().name)
    if slug:
        return slug
    digest = hashlib.sha1(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"vnx-{digest}"
