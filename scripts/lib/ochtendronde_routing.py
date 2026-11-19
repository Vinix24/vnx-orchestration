"""Ochtendronde D2b: classify a morning point as project or fleet, and name the ref a fleet point carries.

ADR-095 decision 5. A point is **fleet** (overkoepelend) when its target lies in the fabric (``FABRIC_PREFIXES``) or
when the same fingerprint stands in two or more projects of the register; otherwise it is **project**. A point whose
class cannot be told (another project's store cannot be read while counting) is **unknown** with a reason: never
silently one or the other. Read-only: nothing here writes into any store.

The fleet point lives in the ledger of ``FLEET_PROJECT`` with ref ``project:<owner>/<original ref>``. The owner is
the lowest project id that holds the fingerprint, so every project computes the same ref and ``point_id`` (ADR-076
decision 3) gives one point. ``operator_queue`` has no ``project:`` ref form; ``routed_ref`` validates it here.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

FLEET_PROJECT = "vnx-dev"
FLEET, PROJECT, UNKNOWN = "fleet", "project", "unknown"
# The fabric, as one tuple. A trailing "/" or "_" is a prefix; anything else is that exact file.
FABRIC_PREFIXES: Tuple[str, ...] = (
    "skills/", "agents/", "scripts/lib/prompts/",
    "scripts/lib/dispatch_cli.py", "scripts/lib/stop_conditions.py", "scripts/pr_merge.py",
    "scripts/review_gate_manager.py", "scripts/lib/review_",
)
# The fingerprint a source already carries; a source without one gives a project point.
FINGERPRINT_KEYS: Tuple[str, ...] = ("proposal_key", "pattern_hash")
DEFAULT_REGISTRY = Path.home() / ".vnx" / "projects.json"  # tests pass their own
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_REF_MAX = 200


class RoutingRefused(ValueError):
    """A routed ref that does not validate, or a register that cannot be read."""


@dataclass(frozen=True)
class Classification:
    kind: str                 # fleet | project | unknown
    reason: str
    owner: Optional[str] = None  # fleet only: the project whose ref names the point


def in_fabric(target: Any) -> bool:
    """True when ``target`` is a relative path inside the fabric. Absolute, ``..`` and non-text are not."""
    if not isinstance(target, str) or not target.strip() or target.startswith("/"):
        return False
    path = posixpath.normpath(target.strip().replace("\\", "/"))
    if path == ".." or path.startswith("../"):
        return False
    return any(path.startswith(p) if p[-1] in "/_" else path == p for p in FABRIC_PREFIXES)


def fingerprint_of(entry: Mapping[str, Any]) -> Optional[str]:
    for key in FINGERPRINT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def classify(project_id: str, ref: str, target: Any, fingerprint: Optional[str],
             holders: Mapping[str, str], unreadable: Mapping[str, str]) -> Classification:
    """The one rule. ``holders`` maps project id to its ref for ``fingerprint`` (other projects, read);
    ``unreadable`` maps a project id to why its store could not be read while counting."""
    if fingerprint is None:
        return Classification(FLEET if in_fabric(target) else PROJECT,
                              "fabric target" if in_fabric(target) else "no fingerprint in the source, no fabric target",
                              project_id if in_fabric(target) else None)
    if unreadable:
        why = "; ".join(f"{p}: {r}" for p, r in sorted(unreadable.items()))
        return Classification(UNKNOWN, f"store not readable while counting the fingerprint ({why})")
    owners = {project_id: ref, **holders}
    if in_fabric(target) or len(owners) >= 2:
        owner = min(owners)
        return Classification(FLEET, "fabric target" if in_fabric(target) else f"fingerprint in {len(owners)} projects", owner)
    return Classification(PROJECT, "fingerprint in one project, no fabric target")


def routed_ref(owner: str, ref: str) -> str:
    """``project:<owner>/<ref>``, validated: the owner is a project id, the ref has no ``..`` and is not absolute."""
    if not isinstance(owner, str) or not _PROJECT_ID.fullmatch(owner):
        raise RoutingRefused("routed ref: owner is not a project id")
    if (not isinstance(ref, str) or not ref.strip() or ref.startswith("/") or ".." in ref.split("/")
            or any(c in ref for c in "\n\r\x00") or len(ref) + len(owner) + 9 > _REF_MAX):
        raise RoutingRefused("routed ref: original ref is empty, absolute, has '..' or is too long")
    return f"project:{owner}/{ref}"


def load_register(path: Optional[Path] = None) -> Dict[str, Path]:
    """project id -> data dir from the register. Raises RoutingRefused when it cannot be read: the caller reports it."""
    target = Path(path) if path is not None else DEFAULT_REGISTRY
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RoutingRefused(f"register {target.name} not readable: {type(exc).__name__}") from exc
    rows = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RoutingRefused(f"register {target.name} has no projects list")
    out: Dict[str, Path] = {}
    for row in rows:
        pid, ddir = (row.get("project_id"), row.get("data_dir")) if isinstance(row, dict) else (None, None)
        if isinstance(pid, str) and _PROJECT_ID.fullmatch(pid) and isinstance(ddir, str) and ddir.strip():
            out[pid] = Path(ddir)
    return out


def _fingerprints_in(path: Path, key: str, idkey: str, prefix: str) -> Dict[str, str]:
    """fingerprint -> ref for one proposal file. Missing file: none. Raises ValueError when it is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} unreadable") from exc
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path.name} has no list {key}")
    out: Dict[str, str] = {}
    for entry in items:
        fp = fingerprint_of(entry) if isinstance(entry, dict) else None
        ident = entry.get(idkey) if isinstance(entry, dict) else None
        if fp and isinstance(ident, (int, str)) and not isinstance(ident, bool):
            out.setdefault(fp, f"{prefix}:{ident}")
    return out


def scan_project(data_dir: Path) -> Dict[str, str]:
    """fingerprint -> ref in one project's proposal files. A data dir that is not there cannot be read: ValueError."""
    if not data_dir.is_dir():
        raise ValueError("data dir missing")
    state = data_dir / "state"
    found = _fingerprints_in(state / "pending_archival.json", "pending_archival", "pattern_id", "archival")
    found.update(_fingerprints_in(state / "pending_edits.json", "edits", "id", "edit"))
    return found


class Fingerprints:
    """The fingerprint index of every other project in the register, read once per run, read-only."""

    def __init__(self, project_id: str, register: Mapping[str, Path]) -> None:
        self.holders: Dict[str, Dict[str, str]] = {}   # fingerprint -> {project id: ref}
        self.unreadable: Dict[str, str] = {}
        for pid, ddir in sorted(register.items()):
            if pid == project_id:
                continue
            try:
                for fp, ref in scan_project(ddir).items():
                    self.holders.setdefault(fp, {})[pid] = ref
            except ValueError as exc:
                self.unreadable[pid] = str(exc)

    def classify(self, project_id: str, ref: str, entry: Mapping[str, Any]) -> Classification:
        fp = fingerprint_of(entry)
        return classify(project_id, ref, entry.get("target"), fp, self.holders.get(fp, {}) if fp else {}, self.unreadable)


class Routing:
    """What a reader needs: the register index for one project and the dir of the fleet ledger."""

    def __init__(self, project_id: str, register_path: Optional[Path] = None,
                 fleet_state_dir: Optional[Path] = None, register: Optional[Mapping[str, Path]] = None) -> None:
        self.fleet_state_dir = fleet_state_dir
        self.problems: List[str] = []
        self.index: Optional[Fingerprints] = None
        try:
            self.index = Fingerprints(project_id, register if register is not None else load_register(register_path))
        except RoutingRefused as exc:
            self.problems.append(str(exc))
        self.problems += [f"store of {p} not readable while counting: {r}"
                          for p, r in sorted((self.index.unreadable if self.index else {}).items())]
