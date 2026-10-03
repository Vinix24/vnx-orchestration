"""Content-boundary classifier shared by the analyzer, the dispatch door and the nightly check.

The repo is open source and holds no operator paths. Which folders are restricted lives in
an operator file, ``~/.vnx/content_boundary.json`` (version 1)::

    {
      "version": 1,
      "client_roots": ["~/Desktop/BUSINESS/clients"],
      "personal_roots": ["~/Personal", "~/Desktop/Lifestyle"],
      "client_project_ids": ["pacompany-engine"],
      "provider_exceptions": {"pacompany-engine": ["deepseek"]},
      "canary_token_file": "~/.vnx/content_canary.token",
      "canary_armed_in": ["~/Personal/canary.md"]
    }

Restricted content (class ``client`` or ``personal``) may go to Claude, codex and local models.
It never goes to GLM, DeepSeek, kimi or any litellm/openrouter lane. A project listed in
``provider_exceptions`` widens the allowed providers, but only for content whose most
restrictive class comes from that project id alone.

A missing or unreadable boundary file is an explicit ``unconfigured`` state, never an
exception: callers decide what unconfigured means (the analyzer treats every session as
restricted).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

BOUNDARY_ENV = "VNX_CONTENT_BOUNDARY_FILE"
BOUNDARY_VERSION = 1
PROJECT_MARKER = ".vnx-project-id"

CLIENT = "client"
PERSONAL = "personal"
FABRIC = "fabric"
OWN = "own"
UNKNOWN = "unknown"

DISPATCH_RESTRICTED: FrozenSet[str] = frozenset({CLIENT, PERSONAL})
ANALYZER_RESTRICTED: FrozenSet[str] = frozenset({CLIENT, PERSONAL, UNKNOWN})

# Higher = more restrictive. Personal outranks client (health and private data).
_RANK = {PERSONAL: 5, CLIENT: 4, UNKNOWN: 3, OWN: 2, FABRIC: 1}

# Where the class of an Origin came from.
SRC_PERSONAL_ROOT = "personal_root"
SRC_CLIENT_ROOT = "client_root"
SRC_CLIENT_PROJECT_ID = "client_project_id"
SRC_MARKER = "project_marker"
SRC_REGISTRY = "registry"
SRC_HOME = "home"
SRC_NONE = "none"
SRC_TEXT_PATH = "text_path"
SRC_CANARY = "canary"

_CANARY_RE = re.compile(r"^[A-Za-z0-9]{16,}$")
_GLOB_CHARS = "*?["
# A path in free text ends at whitespace or a quote/bracket/punctuation delimiter.
_PATH_TAIL = r"""([^\s'"`<>()\[\]{},;|]+)"""


@dataclass(frozen=True)
class Origin:
    """Class of a path, text or dispatch, with the project id and the signal that decided it."""

    cls: str
    project_id: Optional[str] = None
    source: str = SRC_NONE


@dataclass(frozen=True)
class Boundary:
    configured: bool
    client_roots: Tuple[Path, ...] = ()
    personal_roots: Tuple[Path, ...] = ()
    client_project_ids: FrozenSet[str] = frozenset()
    provider_exceptions: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    canary_token_file: Optional[Path] = None
    canary_armed_in: Tuple[Path, ...] = ()
    canary_token: Optional[str] = field(default=None, repr=False)
    reason: str = ""
    source_path: Optional[Path] = None


UNCONFIGURED = Boundary(configured=False, reason="not loaded")


def _home() -> Path:
    return Path(os.path.realpath(Path.home()))


def _real(path: object) -> Path:
    """Realpath without requiring existence: a deleted dir resolves through its deepest ancestor."""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def default_boundary_path() -> Path:
    override = os.environ.get(BOUNDARY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vnx" / "content_boundary.json"


def _roots(raw: object) -> Tuple[Path, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_real(item) for item in raw if isinstance(item, str) and item.strip())


def _read_canary_token(token_file: Optional[Path]) -> Optional[str]:
    if token_file is None:
        return None
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if _CANARY_RE.match(token) else None


def load_boundary(path: Optional[os.PathLike] = None) -> Boundary:
    """Read the operator boundary file. Missing, unreadable or unsupported gives ``unconfigured``."""
    boundary_path = Path(path).expanduser() if path else default_boundary_path()
    try:
        data = json.loads(boundary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Boundary(configured=False, reason=f"{type(exc).__name__}: {boundary_path}",
                        source_path=boundary_path)
    if not isinstance(data, dict) or data.get("version") != BOUNDARY_VERSION:
        return Boundary(configured=False, reason=f"unsupported version in {boundary_path}",
                        source_path=boundary_path)

    exceptions: Dict[str, FrozenSet[str]] = {}
    raw_exc = data.get("provider_exceptions")
    if isinstance(raw_exc, dict):
        for pid, providers in raw_exc.items():
            if isinstance(pid, str) and isinstance(providers, list):
                exceptions[pid] = frozenset(
                    p.strip().lower() for p in providers if isinstance(p, str) and p.strip()
                )

    token_raw = data.get("canary_token_file")
    token_file = _real(token_raw) if isinstance(token_raw, str) and token_raw.strip() else None
    armed = data.get("canary_armed_in")
    armed_in = tuple(_real(p) for p in armed if isinstance(p, str) and p.strip()) \
        if isinstance(armed, list) else ()
    ids = data.get("client_project_ids")

    return Boundary(
        configured=True,
        client_roots=_roots(data.get("client_roots")),
        personal_roots=_roots(data.get("personal_roots")),
        client_project_ids=frozenset(i for i in ids if isinstance(i, str)) if isinstance(ids, list)
        else frozenset(),
        provider_exceptions=exceptions,
        canary_token_file=token_file,
        canary_armed_in=armed_in,
        canary_token=_read_canary_token(token_file),
        source_path=boundary_path,
    )


def allowed_exception_providers(project_id: Optional[str],
                                boundary: Optional[Boundary] = None) -> FrozenSet[str]:
    """Providers a project may use despite being restricted. Unknown id or no field: empty set."""
    if not project_id:
        return frozenset()
    b = boundary if boundary is not None else load_boundary()
    return b.provider_exceptions.get(project_id, frozenset())


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _marker_id(path: Path) -> Optional[str]:
    """Id in the nearest ``.vnx-project-id`` walking up. Reads the file, never ``VNX_PROJECT_ID``."""
    current = path
    while True:
        marker = current / PROJECT_MARKER
        try:
            first = marker.read_text(encoding="utf-8").splitlines()
        except OSError:
            first = []
        if first and first[0].strip():
            return first[0].strip()
        if current.parent == current:
            return None
        current = current.parent


def _registry_ids(path: Path, registry_path: Optional[os.PathLike]) -> List[str]:
    """Ids of the registered checkouts at the deepest level that contains ``path``.

    Tolerant like the gate runner's reader. The deepest checkout decides (path components of
    the realpath, never file order), so a nested project outranks a broad one listed before it.
    Several entries on that same path are all returned, in file order. An entry without a usable
    id contributes ``""``. No containing entry: an empty list.
    """
    reg = Path(registry_path).expanduser() if registry_path else Path.home() / ".vnx" / "projects.json"
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    deepest = -1
    ids: List[str] = []
    for entry in data.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw.strip():
            continue
        root = _real(raw)
        if not _under(path, root):
            continue
        depth = len(root.parts)
        pid = entry.get("project_id")
        pid = pid if isinstance(pid, str) and pid else ""
        if depth > deepest:
            deepest, ids = depth, [pid]
        elif depth == deepest:
            ids.append(pid)
    return ids


def classify_path(path: Optional[os.PathLike], boundary: Boundary,
                  registry_path: Optional[os.PathLike] = None) -> Origin:
    """Class of the place work comes from. Compares path components, never string prefixes."""
    if path is None or not str(path).strip():
        return Origin(UNKNOWN, None, SRC_NONE)
    resolved = _real(path)
    marker = _marker_id(resolved)

    if any(_under(resolved, root) for root in boundary.personal_roots):
        return Origin(PERSONAL, marker, SRC_PERSONAL_ROOT)
    if any(_under(resolved, root) for root in boundary.client_roots):
        return Origin(CLIENT, marker, SRC_CLIENT_ROOT)

    if marker:
        if marker in boundary.client_project_ids:
            return Origin(CLIENT, marker, SRC_CLIENT_PROJECT_ID)
        return Origin(FABRIC, marker, SRC_MARKER)

    registered = _registry_ids(resolved, registry_path)
    if registered:
        for pid in registered:
            if pid in boundary.client_project_ids:
                return Origin(CLIENT, pid, SRC_CLIENT_PROJECT_ID)
        return Origin(FABRIC, registered[0] or None, SRC_REGISTRY)

    home = _home()
    if resolved != home and home in resolved.parents:
        return Origin(OWN, None, SRC_HOME)
    return Origin(UNKNOWN, None, SRC_NONE)


def _root_patterns(boundary: Boundary) -> Iterable[Tuple[str, re.Pattern]]:
    home = _home()
    for cls, roots in ((PERSONAL, boundary.personal_roots), (CLIENT, boundary.client_roots)):
        for root in roots:
            forms = {str(root)}
            if home in root.parents:
                forms.add("~/" + str(root.relative_to(home)))
            for form in forms:
                yield cls, re.compile(re.escape(form) + "/" + _PATH_TAIL)


def classify_text(text: Optional[str], boundary: Boundary) -> Optional[Origin]:
    """``client``/``personal`` when the text names a concrete restricted path or holds the canary.

    A concrete path has at least one component after the root that is not a glob. Returns
    None when the text carries no restricted signal.
    """
    if not text:
        return None
    token = boundary.canary_token
    if token and token in text:
        return Origin(PERSONAL, None, SRC_CANARY)
    best: Optional[Origin] = None
    for cls, pattern in _root_patterns(boundary):
        for match in pattern.finditer(text):
            tail = match.group(1)
            if any(c in tail.split("/")[0] for c in _GLOB_CHARS):
                continue
            hit = Origin(cls, None, SRC_TEXT_PATH)
            best = hit if best is None else most_restrictive_origin(best, hit)
    return best


def most_restrictive(classes: Iterable[Optional[str]]) -> str:
    """Most restrictive of the given classes; no input gives ``unknown``."""
    ranked = [c for c in classes if c in _RANK]
    if not ranked:
        return UNKNOWN
    return max(ranked, key=lambda c: _RANK[c])


def most_restrictive_origin(*origins: Optional[Origin]) -> Origin:
    present = [o for o in origins if o is not None]
    if not present:
        return Origin(UNKNOWN)
    return max(present, key=lambda o: _RANK.get(o.cls, 0))


def exception_applies(origin: Origin, provider: str, boundary: Boundary) -> bool:
    """True when ``origin`` is client content only through a project id that grants ``provider``."""
    return (
        origin.cls == CLIENT
        and origin.source == SRC_CLIENT_PROJECT_ID
        and provider.strip().lower() in allowed_exception_providers(origin.project_id, boundary)
    )
