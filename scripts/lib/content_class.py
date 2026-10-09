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

import codecs
import json
import logging
import math
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

BOUNDARY_ENV = "VNX_CONTENT_BOUNDARY_FILE"
BOUNDARY_VERSION = 1
PROJECT_MARKER = ".vnx-project-id"

# Bounds on one marker read. The path comes from data (a transcript cwd, a dispatch), so the
# read may land on a FIFO, a device or a file whose open never returns.
MARKER_TIMEOUT_ENV = "VNX_MARKER_READ_TIMEOUT_SECONDS"
MARKER_TIMEOUT_DEFAULT = 5.0
MARKER_MAX_BYTES = 4096
# Reader threads that timed out and are still blocked. At this many, no new read is started.
MARKER_MAX_ABANDONED = 8

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
# A marker on the way up could not be read: the class is ``unknown`` and the walk stopped there.
SRC_MARKER_UNREADABLE = "marker_unreadable"
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


@dataclass(frozen=True)
class _MarkerRead:
    """One bounded marker read: an id line, nothing to read here (both empty), or a refusal."""

    line: str = ""
    refusal: str = ""


@dataclass(frozen=True)
class _Marker:
    """End of the walk up: the nearest id, no marker at all, or the directory that stopped it."""

    project_id: Optional[str] = None
    unreadable_dir: Optional[Path] = None
    reason: str = ""


class _Attempt:
    """What a caller and its reader thread share. Guarded by ``_marker_lock``."""

    __slots__ = ("outcome", "finished", "abandoned", "done")

    def __init__(self) -> None:
        self.outcome = _MarkerRead(refusal="reader failed")
        self.finished = False
        self.abandoned = False
        self.done = threading.Event()


_marker_lock = threading.Lock()
# Directories whose marker read timed out, kept for the life of the process. One entry costs one
# timeout to add, so the set grows by at most one path per timeout period.
_stuck_marker_dirs: Set[str] = set()
_abandoned_readers = 0


def marker_timeout() -> float:
    """Seconds one marker read may take, from ``VNX_MARKER_READ_TIMEOUT_SECONDS``.

    Unset, unparseable, not finite or not above zero gives the default of 5 seconds.
    """
    try:
        value = float(os.environ.get(MARKER_TIMEOUT_ENV, "").strip())
    except ValueError:
        return MARKER_TIMEOUT_DEFAULT
    if not math.isfinite(value) or value <= 0:
        return MARKER_TIMEOUT_DEFAULT
    return min(value, threading.TIMEOUT_MAX)


def _first_line(data: bytes) -> _MarkerRead:
    """First line of what was read. Refuses bytes that are not UTF-8 and a line cut by the bound."""
    full = len(data) >= MARKER_MAX_BYTES
    try:
        # A full buffer may end inside a multi-byte character; that tail is not an error.
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not full)
    except UnicodeDecodeError:
        return _MarkerRead(refusal="not UTF-8")
    lines = text.splitlines()
    if not lines:
        return _MarkerRead()
    if full and len(lines[0]) == len(text):
        return _MarkerRead(refusal=f"first line does not end within {MARKER_MAX_BYTES} bytes")
    return _MarkerRead(line=lines[0].strip())


def _open_and_read(marker: Path) -> _MarkerRead:
    """Open without blocking on a FIFO or device, refuse what is not a regular file, read once."""
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except (FileNotFoundError, NotADirectoryError):
        return _MarkerRead()
    except (OSError, ValueError) as exc:
        return _MarkerRead(refusal=f"open failed: {type(exc).__name__}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _MarkerRead(refusal="not a regular file")
        data = os.read(fd, MARKER_MAX_BYTES)
    except OSError as exc:
        return _MarkerRead(refusal=f"read failed: {type(exc).__name__}")
    finally:
        os.close(fd)
    return _first_line(data)


def _marker_reader(marker: Path, attempt: _Attempt) -> None:
    """Thread body. An unexpected error keeps the "reader failed" refusal and still signs off."""
    global _abandoned_readers
    try:
        attempt.outcome = _open_and_read(marker)
    finally:
        with _marker_lock:
            attempt.finished = True
            if attempt.abandoned:
                _abandoned_readers -= 1
        attempt.done.set()


def _read_marker_line(directory: Path, timeout: float) -> _MarkerRead:
    """First line of the marker in ``directory``, bounded in file type, size and time.

    The open and the read run in a daemon thread, so a call that never returns costs
    ``timeout`` seconds and not the process. A directory that timed out is remembered and
    refused at once from then on. Its thread is abandoned, never killed; while
    ``MARKER_MAX_ABANDONED`` of them are still blocked, every read is refused at once and no
    thread is started. Reads resume when a blocked one returns. Only a missing marker
    (``ENOENT``, ``ENOTDIR``) and a blank first line count as "no marker here"; every other
    failure is a refusal, because a marker that may exist and cannot be read decides nothing.
    """
    global _abandoned_readers
    key = str(directory)
    with _marker_lock:
        if key in _stuck_marker_dirs:
            return _MarkerRead(refusal="timed out earlier in this process, not retried")
        if _abandoned_readers >= MARKER_MAX_ABANDONED:
            return _MarkerRead(
                refusal=f"{_abandoned_readers} marker reads are still blocked, no new read started")
    attempt = _Attempt()
    thread = threading.Thread(target=_marker_reader, args=(directory / PROJECT_MARKER, attempt),
                              daemon=True, name="vnx-marker-read")
    try:
        thread.start()
    except RuntimeError:
        return _MarkerRead(refusal="no reader thread could be started")
    if attempt.done.wait(timeout):
        return attempt.outcome
    with _marker_lock:
        if attempt.finished:
            return attempt.outcome
        attempt.abandoned = True
        _abandoned_readers += 1
        _stuck_marker_dirs.add(key)
    return _MarkerRead(refusal=f"no answer within {timeout:g} seconds")


def _marker_id(path: Path) -> _Marker:
    """Nearest ``.vnx-project-id`` walking up. Reads the file, never ``VNX_PROJECT_ID``.

    An unreadable marker ends the walk at its directory: the id of a parent says nothing about
    a nearer marker that may name another project.
    """
    timeout = marker_timeout()
    current = path
    while True:
        read = _read_marker_line(current, timeout)
        if read.refusal:
            return _Marker(unreadable_dir=current, reason=read.refusal)
        if read.line:
            return _Marker(project_id=read.line)
        if current.parent == current:
            return _Marker()
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
    """Class of the place work comes from. Compares path components, never string prefixes.

    The personal and client roots are compared first: that reads no file and decides the class
    on its own. The marker is read after that, bounded (``_read_marker_line``). Under a root it
    only fills ``project_id``, and an unreadable marker leaves that ``None``. Outside the roots
    an unreadable marker gives ``unknown`` with source ``marker_unreadable``; the walk stops
    there, so the result is never ``fabric`` or ``own`` on the word of a parent directory.
    Either way one WARNING names the directory.
    """
    if path is None or not str(path).strip():
        return Origin(UNKNOWN, None, SRC_NONE)
    resolved = _real(path)

    root: Optional[Tuple[str, str]] = None
    if any(_under(resolved, r) for r in boundary.personal_roots):
        root = (PERSONAL, SRC_PERSONAL_ROOT)
    elif any(_under(resolved, r) for r in boundary.client_roots):
        root = (CLIENT, SRC_CLIENT_ROOT)

    found = _marker_id(resolved)
    marker = found.project_id
    if root is not None:
        if found.unreadable_dir is not None:
            logger.warning("content_class: project marker in %s unreadable (%s); class stays %s "
                           "from the root, project id left empty",
                           found.unreadable_dir, found.reason, root[0])
        return Origin(root[0], marker, root[1])
    if found.unreadable_dir is not None:
        logger.warning("content_class: project marker in %s unreadable (%s); classified %s (%s), "
                       "no parent directory consulted",
                       found.unreadable_dir, found.reason, UNKNOWN, SRC_MARKER_UNREADABLE)
        return Origin(UNKNOWN, None, SRC_MARKER_UNREADABLE)

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
