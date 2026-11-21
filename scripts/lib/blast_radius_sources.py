"""blast_radius_sources: what the blast-radius check compares against (ADR-096 decisions 9 and 11).

``live_subjects``: dispatches in ``live_work`` with status live or starting, with their spec paths
(stale is a zombie and does not count). ``open_pr_subjects``: open PRs and their changed files,
cached per ``(project_id, pr_number, head_sha)``. Unknown is a value of its own: a source that
cannot be read is ``unavailable`` with its reason, a subject whose paths cannot be read has
``paths=None`` and a reason in ``Sources.reasons``. Never an empty list standing in for a failure.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_LIB = Path(__file__).resolve().parent
for _p in (str(_LIB), str(_LIB.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomic_io import atomic_write_json
from blast_radius import Subject, _branch_of

CACHE_FILENAME = "blast_radius_pr_cache.json"
CACHE_VERSION = 1
PR_LIST_LIMIT, REASON_PRS_TRUNCATED = 200, "open_prs_truncated"  # a full gh pr list page may be cut off (OI-3055)
Gh = Callable[[List[str], Path], str]  # gh(argv, cwd) -> stdout; raises on failure


@dataclass(frozen=True)
class Sources:
    subjects: Tuple[Subject, ...] = ()
    unavailable: Optional[str] = None  # the whole source could not be read; subjects is empty
    reasons: Dict[str, str] = field(default_factory=dict)  # deliverable key -> why paths is None


def default_cache_path(state_dir: "str | Path") -> Path:
    return Path(state_dir) / CACHE_FILENAME


def _spec_paths(data_dir: Path, did: str) -> Tuple[Optional[Tuple[str, ...]], str]:
    """The write-capable paths of the staged spec, or (None, why they cannot be read)."""
    from dispatch_spec import WRITE_GRANTING_PATH_ACCESS, PathAccess
    from review_vendor import dispatch_spec_paths

    specs, why = dispatch_spec_paths(did, data_dir=data_dir)
    if not specs:
        return None, why
    if len(specs) > 1:
        return None, f"{len(specs)} dispatch-spec.json files for {did}: not guessed"
    try:
        raw = json.loads(specs[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{specs[0]} cannot be read: {type(exc).__name__}: {exc}"
    entries = raw.get("dispatch_paths") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        return None, f"{specs[0]} declares no dispatch_paths"
    paths: List[str] = []
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"]:
            return None, f"{specs[0]} has a malformed dispatch_paths entry: {e!r}"
        try:
            access = PathAccess(e.get("access", PathAccess.READ_WRITE.value))
        except ValueError:
            return None, f"{specs[0]} has an unknown access value: {e.get('access')!r}"
        if access in WRITE_GRANTING_PATH_ACCESS:
            paths.append(e["path"])
    return (tuple(paths), "") if paths else (None, f"{specs[0]} declares no write-capable path")


def live_subjects(state_dir: "str | Path", project_id: Optional[str] = None) -> Sources:
    """Dispatches live or starting as Subjects; unmeasured ones are unknown (paths=None)."""
    state = Path(state_dir)
    try:
        import build_t0_state as bts
        lw = bts._build_live_work(state, project_id if project_id is not None else bts._project_id_for(state), None)
    except Exception as exc:  # vnx-silent-except: returned as unavailable with its reason, never an empty list
        return Sources(unavailable=f"live_work measurement raised: {type(exc).__name__}: {exc}")
    if not isinstance(lw, dict) or lw.get("available") is not True:
        return Sources(unavailable=f"live_work unavailable: {lw.get('reason') if isinstance(lw, dict) else lw!r}")
    subjects: List[Subject] = []
    reasons: Dict[str, str] = {}
    for status in ("live", "starting", "unmeasured"):
        items = lw.get(status)
        if not isinstance(items, list):
            return Sources(unavailable=f"live_work.{status} is {type(items).__name__}, not a list")
        for item in items:
            did = item.get("dispatch_id") if isinstance(item, dict) else None
            if not isinstance(did, str) or not did.strip():
                return Sources(unavailable=f"live_work.{status} has an item without a dispatch_id: {item!r}")
            if status == "unmeasured":  # the lock probe raised: not known to be live, paths unknown
                paths, why = None, f"lock probe unmeasured: {item.get('lock')}"
            else:
                paths, why = _spec_paths(state.parent, did)
            if paths is None:
                reasons[did] = why
            subjects.append(Subject(did, paths, _branch_of(did)))
    return Sources(tuple(subjects), None, reasons)


def _load_cache(cache_path: Path) -> Dict[str, List[str]]:
    """Cached entries; an unreadable or foreign-version file is an empty cache (re-read)."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) and data.get("version") == CACHE_VERSION else None
    return {k: v for k, v in entries.items() if isinstance(v, list) and all(isinstance(p, str) for p in v)} \
        if isinstance(entries, dict) else {}


def _read_files(gh: Gh, root: Path, number: int) -> Tuple[Optional[List[str]], str, str]:
    """(files, head_sha, reason) of one PR; files is None when they cannot be read."""
    try:
        data = json.loads(gh(["pr", "view", str(number), "--json", "files,headRefOid,headRefName"], root))
    except Exception as exc:  # vnx-silent-except: returned as paths=None with the reason, never cached
        return None, "", f"gh pr view {number} failed: {type(exc).__name__}: {exc}"
    files = data.get("files") if isinstance(data, dict) else None
    sha = data.get("headRefOid") if isinstance(data, dict) else None
    if (not isinstance(files, list) or not isinstance(sha, str) or not sha
            or not all(isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"] for f in files)):
        return None, "", f"gh pr view {number} returned no usable files/headRefOid"
    return [f["path"] for f in files], sha, ""


def open_pr_subjects(project_root: "str | Path", project_id: str, cache_path: "str | Path", gh: Gh) -> Sources:
    """Open PRs as Subjects; files come from the cache when the head is unchanged."""
    root, cache_path = Path(project_root), Path(cache_path)
    if not isinstance(project_id, str) or not project_id.strip():
        return Sources(unavailable="no project_id: the PR cache is keyed per project")
    try:
        listing = json.loads(gh(["pr", "list", "--state", "open", "--limit", str(PR_LIST_LIMIT),
                                 "--json", "number,headRefName,headRefOid"], root))
    except Exception as exc:  # vnx-silent-except: returned as unavailable with its reason
        return Sources(unavailable=f"gh pr list failed: {type(exc).__name__}: {exc}")
    if not isinstance(listing, list):
        return Sources(unavailable=f"gh pr list returned {type(listing).__name__}, not a list")
    if len(listing) >= PR_LIST_LIMIT:
        return Sources(unavailable=f"{REASON_PRS_TRUNCATED}: gh pr list returned {len(listing)} rows, the limit")
    cached = _load_cache(cache_path)
    mine = {k: v for k, v in cached.items() if k.startswith(f"{project_id}:")}
    fresh: Dict[str, List[str]] = {}  # this project's entries after the call: other heads drop out
    subjects: List[Subject] = []
    reasons: Dict[str, str] = {}
    for pr in listing:
        number, branch, sha = (pr.get(k) if isinstance(pr, dict) else None for k in ("number", "headRefName", "headRefOid"))
        if isinstance(number, bool) or not isinstance(number, int) or not isinstance(branch, str) or not isinstance(sha, str) or not sha:
            return Sources(unavailable=f"gh pr list returned a malformed entry: {pr!r}")
        did = branch[len("dispatch/"):] if branch.startswith("dispatch/") else ""
        paths = cached.get(f"{project_id}:{number}:{sha}")
        if paths is not None:
            fresh[f"{project_id}:{number}:{sha}"] = paths
        else:
            paths, view_sha, why = _read_files(gh, root, number)
            if paths is None:
                reasons[did or f"pr-{number}"] = why
            else:
                fresh[f"{project_id}:{number}:{view_sha}"] = paths
        subjects.append(Subject(did, tuple(paths) if paths is not None else None, branch))
    if mine != fresh:
        try:
            atomic_write_json(cache_path, {"version": CACHE_VERSION, "entries": {
                **{k: v for k, v in cached.items() if k not in mine}, **fresh}})
        except OSError:  # vnx-silent-except: the cache is an optimisation; a failed write costs one re-read
            pass
    return Sources(tuple(subjects), None, reasons)
