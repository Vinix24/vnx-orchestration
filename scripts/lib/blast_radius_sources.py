"""blast_radius_sources — what the blast-radius check compares against (ADR-096 decisions 9 and 11).

Two sources, each returned as ``Subject`` values for ``blast_radius.check``:

* ``live_subjects``: dispatches in ``live_work`` with status ``live`` or ``starting`` and the
  ``dispatch_paths`` of their staged spec. ``stale`` is a zombie and does not count.
* ``open_pr_subjects``: the project's open PRs and their changed files, cached per
  ``(project_id, pr_number, head_sha)``.

Unknown is a value of its own. A source that cannot be read comes back as ``unavailable`` with the
reason, never as an empty list; a subject whose paths cannot be read has ``paths=None`` and a
reason in ``Sources.reasons``. Nothing here reads configuration or decides to block: the door does.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_LIB = Path(__file__).resolve().parent
for _p in (str(_LIB), str(_LIB.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomic_io import atomic_write_json
from blast_radius import Subject, _branch_of

CACHE_FILENAME = "blast_radius_pr_cache.json"
CACHE_VERSION = 1
LIVE_STATUSES = ("live", "starting")
_BRANCH_PREFIX = "dispatch/"

# gh(argv, cwd) -> stdout; raises on a non-zero exit or when gh cannot start.
Gh = Callable[[List[str], Path], str]


@dataclass(frozen=True)
class Sources:
    subjects: Tuple[Subject, ...] = ()
    unavailable: Optional[str] = None  # set: the whole source could not be read; subjects is empty
    reasons: Dict[str, str] = field(default_factory=dict)  # deliverable key -> why paths is None


def default_cache_path(state_dir: "str | Path") -> Path:
    return Path(state_dir) / CACHE_FILENAME


def _unavailable(reason: str) -> Sources:
    return Sources(unavailable=reason)


# --- live work ---------------------------------------------------------------------------------
def _spec_paths(data_dir: Path, dispatch_id: str) -> Tuple[Optional[Tuple[str, ...]], str]:
    """The write-capable paths of the staged spec, or (None, why they cannot be read)."""
    from dispatch_spec import PathAccess, WRITE_GRANTING_PATH_ACCESS
    from review_vendor import dispatch_spec_paths

    specs, why = dispatch_spec_paths(dispatch_id, data_dir=data_dir)
    if not specs:
        return None, why
    if len(specs) > 1:
        return None, f"{len(specs)} dispatch-spec.json files for {dispatch_id}: not guessed"
    try:
        raw = json.loads(specs[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{specs[0]} cannot be read: {type(exc).__name__}: {exc}"
    entries = raw.get("dispatch_paths") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        return None, f"{specs[0]} declares no dispatch_paths"
    paths: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            return None, f"{specs[0]} has a malformed dispatch_paths entry: {entry!r}"
        try:
            access = PathAccess(entry.get("access", PathAccess.READ_WRITE.value))
        except ValueError:
            return None, f"{specs[0]} has an unknown access value: {entry.get('access')!r}"
        if access in WRITE_GRANTING_PATH_ACCESS:
            paths.append(entry["path"])
    if not paths:
        return None, f"{specs[0]} declares no write-capable dispatch_paths"
    return tuple(paths), ""


def live_subjects(state_dir: "str | Path", project_id: Optional[str] = None) -> Sources:
    """Dispatches with status live or starting, as Subjects with the paths of their spec."""
    state = Path(state_dir)
    try:
        import build_t0_state as bts
        pid = project_id if project_id is not None else bts._project_id_for(state)
        live_work = bts._build_live_work(state, pid, None)
    except Exception as exc:  # vnx-silent-except: returned as unavailable with its reason, never an empty list
        return _unavailable(f"live_work measurement raised: {type(exc).__name__}: {exc}")
    if not isinstance(live_work, dict) or live_work.get("available") is not True:
        reason = live_work.get("reason") if isinstance(live_work, dict) else None
        return _unavailable(f"live_work unavailable: {reason or 'no reason given'}")

    subjects: List[Subject] = []
    reasons: Dict[str, str] = {}
    for status in LIVE_STATUSES:
        items = live_work.get(status)
        if not isinstance(items, list):
            return _unavailable(f"live_work.{status} is {type(items).__name__}, not a list")
        for item in items:
            did = item.get("dispatch_id") if isinstance(item, dict) else None
            if not isinstance(did, str) or not did.strip():
                return _unavailable(f"live_work.{status} has an item without a dispatch_id: {item!r}")
            paths, why = _spec_paths(state.parent, did)
            if paths is None:
                reasons[did] = why
            subjects.append(Subject(did, paths, _branch_of(did)))
    # unmeasured: the lock probe raised, so whether it is live is unknown; its paths are unknown too.
    unmeasured = live_work.get("unmeasured")
    if not isinstance(unmeasured, list):
        return _unavailable(f"live_work.unmeasured is {type(unmeasured).__name__}, not a list")
    for item in unmeasured:
        did = item.get("dispatch_id") if isinstance(item, dict) else None
        if not isinstance(did, str) or not did.strip():
            return _unavailable(f"live_work.unmeasured has an item without a dispatch_id: {item!r}")
        reasons[did] = f"lock probe unmeasured: {item.get('lock')}"
        subjects.append(Subject(did, None, _branch_of(did)))
    return Sources(tuple(subjects), None, reasons)


# --- open PRs ----------------------------------------------------------------------------------
def _key(project_id: str, number: int, sha: str) -> str:
    return f"{project_id}:{number}:{sha}"


def _load_cache(cache_path: Path) -> Dict[str, List[str]]:
    """The cached entries; an unreadable or foreign-version file is an empty cache (re-read, not trusted)."""
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items()
            if isinstance(k, str) and isinstance(v, list) and all(isinstance(p, str) for p in v)}


def _store_cache(cache_path: Path, entries: Dict[str, List[str]]) -> None:
    try:
        atomic_write_json(Path(cache_path), {"version": CACHE_VERSION, "entries": entries})
    except OSError:  # vnx-silent-except: the cache is an optimisation; a failed write costs one re-read, no verdict
        return


def _deliverable_of(branch: str) -> str:
    return branch[len(_BRANCH_PREFIX):] if branch.startswith(_BRANCH_PREFIX) else ""


def _read_files(gh: Gh, project_root: Path, number: int) -> Tuple[Optional[List[str]], str, str]:
    """(files, head_sha, reason) of one PR; files is None when they cannot be read."""
    try:
        out = gh(["pr", "view", str(number), "--json", "files,headRefOid,headRefName"], project_root)
        data = json.loads(out)
    except Exception as exc:  # vnx-silent-except: returned as paths=None with the reason, never cached
        return None, "", f"gh pr view {number} failed: {type(exc).__name__}: {exc}"
    files = data.get("files") if isinstance(data, dict) else None
    sha = data.get("headRefOid") if isinstance(data, dict) else None
    if not isinstance(files, list) or not isinstance(sha, str) or not sha:
        return None, "", f"gh pr view {number} returned no usable files/headRefOid"
    paths: List[str] = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not f["path"]:
            return None, "", f"gh pr view {number} returned a malformed file entry: {f!r}"
        paths.append(f["path"])
    return paths, sha, ""


def open_pr_subjects(project_root: "str | Path", project_id: str, cache_path: "str | Path",
                     gh: Gh) -> Sources:
    """The project's open PRs as Subjects, files from the cache when the head is unchanged."""
    root = Path(project_root)
    if not isinstance(project_id, str) or not project_id.strip():
        return _unavailable("no project_id: the PR cache is keyed per project")
    try:
        listing = json.loads(gh(["pr", "list", "--state", "open", "--limit", "200",
                                 "--json", "number,headRefName,headRefOid"], root))
    except Exception as exc:  # vnx-silent-except: returned as unavailable with its reason
        return _unavailable(f"gh pr list failed: {type(exc).__name__}: {exc}")
    if not isinstance(listing, list):
        return _unavailable(f"gh pr list returned {type(listing).__name__}, not a list")

    cached = _load_cache(Path(cache_path))
    fresh: Dict[str, List[str]] = {}  # this project's entries after the call; other heads drop out
    others = {k: v for k, v in cached.items() if not k.startswith(f"{project_id}:")}
    subjects: List[Subject] = []
    reasons: Dict[str, str] = {}
    for pr in listing:
        number = pr.get("number") if isinstance(pr, dict) else None
        branch = pr.get("headRefName") if isinstance(pr, dict) else None
        sha = pr.get("headRefOid") if isinstance(pr, dict) else None
        if (isinstance(number, bool) or not isinstance(number, int)
                or not isinstance(branch, str) or not isinstance(sha, str) or not sha):
            return _unavailable(f"gh pr list returned a malformed entry: {pr!r}")
        did = _deliverable_of(branch)
        label = did or f"pr-{number}"
        paths = cached.get(_key(project_id, number, sha))
        if paths is not None:
            fresh[_key(project_id, number, sha)] = paths
        else:
            paths, view_sha, why = _read_files(gh, root, number)
            if paths is None:
                reasons[label] = why
            else:
                fresh[_key(project_id, number, view_sha)] = paths
        subjects.append(Subject(did, tuple(paths) if paths is not None else None, branch))
    if {k: v for k, v in cached.items() if k.startswith(f"{project_id}:")} != fresh:
        _store_cache(Path(cache_path), {**others, **fresh})
    return Sources(tuple(subjects), None, reasons)
