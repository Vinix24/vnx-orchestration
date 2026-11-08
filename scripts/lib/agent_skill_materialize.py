"""Shadow copy of an agent role's skills (ADR-050 contract 5, agents-als-map PR-2a).

Under ``VNX_AGENT_LAYOUT=shadow`` the skills a role declares in
``agents/<role>/config.yaml`` are copied to
``<worktree>/.vnx-agent-skills/<role>/<slug>/`` with a ``manifest.json`` (path,
length, sha256 per file). No harness discovers that directory, git ignores it,
and it goes away with the worktree.

The shadow fails open: any error gives one WARNING and ``status: error`` in the
result (and in ``manifest.json`` once the ignore check has passed). Nothing here
raises to the caller. Under ``off`` nothing happens and no directory is made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import agent_layout
from agent_resolver import _is_safe_agent_name
from skill_prefix import SKILL_TREES, resolve_skill_dir, skill_search_paths

logger = logging.getLogger(__name__)

SHADOW_DIR = ".vnx-agent-skills"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1

NATIVE_DIR = ".claude/skills"
RECORD_DIR = "agent_skill_materializations"
RECORD_SCHEMA = 1
STATUSES = frozenset({"ok", "partial", "error", "skipped"})
REASONS = frozenset({
    "no_role", "no_declared_set", "no_cwd", "no_state_dir", "no_dispatch_id",
    "no_project_id", "not_isolated", "not_ignored", "already_native",
    "source_missing", "copy_failed:<Class>", "not_owned:<slug>", "path_outside_worktree", "symlink_in_destination",
})

DEST_REFUSALS = frozenset({"symlink_in_destination", "path_outside_worktree"})


class ShadowError(Exception):
    """A refusal with a stable reason string; caught inside the module."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def materialize_role_skills(
    worktree: Path | str,
    role: str,
    slugs: Iterable[str],
    source_root: Path | str | None = None,
) -> dict[str, Any]:
    """Copy *slugs* for *role* into the worktree's shadow directory.

    Returns ``{"status": "skipped"|"ok"|"error", ...}``. Never raises.
    ``source_root`` is the tree holding ``skills/`` (default: the worktree).
    """
    if not agent_layout.is_shadow():
        return {"status": "skipped", "reason": "layout_off"}
    wt = Path(worktree)
    try:
        return _materialize(wt, role, list(slugs), Path(source_root) if source_root else wt)
    except ShadowError as exc:
        return _fail(wt, role, exc.reason)
    except Exception as exc:  # fail-open: the shadow never stops a dispatch
        return _fail(wt, role, f"unexpected:{type(exc).__name__}")


def _fail(wt: Path, role: str, reason: str) -> dict[str, Any]:
    logger.warning("agent skill shadow for role %r: %s", role, reason)
    result: dict[str, Any] = {"status": "error", "reason": reason, "role": role}
    if reason != "not_ignored":
        _write_error_manifest(wt, role, result)
    return result


def _write_error_manifest(wt: Path, role: str, result: dict[str, Any]) -> None:
    """Best effort: record the reason, but only for a safe role under an ignored dir."""
    try:
        if not _is_safe_agent_name(role) or not _is_ignored(wt):
            return
        _checked_base(wt)
        root_fd = _open_worktree(wt)
        try:
            role_fd = _open_role_dir(root_fd, role, reset=False)
            try:
                _write_manifest(role_fd, {"schema": MANIFEST_SCHEMA, **result})
            finally:
                os.close(role_fd)
        finally:
            os.close(root_fd)
    except Exception as exc:  # fail-open: the reason is already in the WARNING
        logger.warning("could not write error manifest: %s", type(exc).__name__)


def _checked_base(wt: Path) -> Path:
    """The shadow dir: not a symlink and, once it exists, resolving inside the worktree."""
    base = wt / SHADOW_DIR
    if base.is_symlink():
        raise ShadowError("shadow_dir_symlink")
    if base.exists() and not _inside(Path(os.path.realpath(base)), Path(os.path.realpath(wt))):
        raise ShadowError("shadow_dir_outside_worktree")
    return base


def _is_ignored(wt: Path) -> bool:
    """True only when git positively reports the shadow dir as ignored (rc 0)."""
    proc = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-q", f"{SHADOW_DIR}/x"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _materialize(wt: Path, role: str, slugs: list[str], source_root: Path) -> dict[str, Any]:
    if not _is_ignored(wt):
        raise ShadowError("not_ignored")
    if not _is_safe_agent_name(role):
        raise ShadowError(f"role_refused:{role}")
    sources = [(slug, _vet_slug(source_root, slug)) for slug in slugs]
    _checked_base(wt)
    root_fd = _open_worktree(wt)
    try:
        role_fd = _open_role_dir(root_fd, role, reset=True)
        try:
            entries = [_copy_skill(role_fd, source_root, slug, src) for slug, src in sources]
            manifest = {"schema": MANIFEST_SCHEMA, "status": "ok", "role": role, "skills": entries}
            _write_manifest(role_fd, manifest)
        finally:
            os.close(role_fd)
    finally:
        os.close(root_fd)
    return {"status": "ok", "role": role, "dest": str(wt / SHADOW_DIR / role), "skills": entries}


def _vet_slug(source_root: Path, slug: str) -> Path:
    """Return the real skill dir for *slug*, or refuse it."""
    if not isinstance(slug, str) or not skill_search_paths(source_root, slug):
        raise ShadowError(f"slug_refused:{slug}")
    src = resolve_skill_dir(source_root, slug)
    if src is None:
        raise ShadowError(f"slug_unknown:{slug}")
    real_src = Path(os.path.realpath(src))
    if not any(_inside(real_src, Path(os.path.realpath(source_root / t))) for t in SKILL_TREES):
        raise ShadowError(f"slug_outside_skill_trees:{slug}")
    return real_src


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_worktree(wt: Path) -> int:
    """The one open by absolute path: the worktree root the caller passed in (trusted)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(os.path.realpath(wt), flags)


def _open_role_dir(root_fd: int, role: str, reset: bool) -> int:
    """Open ``<SHADOW_DIR>/<role>`` below *root_fd*, one component at a time; *reset* empties it first."""
    base_fd = _descend(root_fd, (SHADOW_DIR,))
    try:
        try:
            mode = os.stat(role, dir_fd=base_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and stat.S_ISLNK(mode):
            raise ShadowError("role_dir_symlink")
        if mode is not None and reset:
            if not stat.S_ISDIR(mode):
                os.unlink(role, dir_fd=base_fd)
            else:
                _rmtree_below(base_fd, role)
        return _descend(base_fd, (role,))
    finally:
        os.close(base_fd)


def _rmtree_below(parent_fd: int, name: str) -> None:
    """Remove directory *name* below *parent_fd*; links are unlinked, never followed."""
    for _top, dirs, files, dir_fd in os.fwalk(name, topdown=False, dir_fd=parent_fd, follow_symlinks=False):
        for entry in files:
            os.unlink(entry, dir_fd=dir_fd)
        for entry in dirs:
            if stat.S_ISLNK(os.stat(entry, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                os.unlink(entry, dir_fd=dir_fd)
            else:
                os.rmdir(entry, dir_fd=dir_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _read_below_worktree(wt: Path, parts: tuple[str, ...]) -> bytes:
    """Read the file at *parts* below the worktree root; every component is opened without following a link."""
    fd = _open_worktree(wt)
    try:
        for part in parts[:-1]:
            nxt = _open_dir_nofollow(fd, part)
            os.close(fd)
            fd = nxt
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            raise ShadowError("symlink_in_destination") from exc
        with os.fdopen(file_fd, "rb") as handle:
            return handle.read()
    finally:
        os.close(fd)


def _write_manifest(role_fd: int, manifest: dict[str, Any]) -> None:
    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    _write_nofollow(role_fd, Path(MANIFEST_NAME), data, 0o644)


def _skill_files(src: Path) -> list[Path]:
    """Regular files under *src* (sorted); a symlink must stay inside *src*."""
    files: list[Path] = []
    for dirpath, _dirs, names in os.walk(src):
        for name in names:
            path = Path(dirpath) / name
            if not _inside(Path(os.path.realpath(path)), src):
                raise ShadowError(f"symlink_outside_skill:{path.relative_to(src).as_posix()}")
            if path.is_file():
                files.append(path)
    return sorted(files)


def _open_dir_nofollow(parent_fd: int, name: str) -> int:
    """Open the single-component directory *name* below *parent_fd* without following a link."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ShadowError("symlink_in_destination") from exc


def _descend(root_fd: int, parts: tuple[str, ...]) -> int:
    """Create-and-enter each directory of *parts* below *root_fd*; a link or file fails the write."""
    fd = os.dup(root_fd)
    for part in parts:
        try:
            os.mkdir(part, 0o755, dir_fd=fd)
        except FileExistsError:
            pass  # vnx-silent-except: an existing entry is re-opened with O_NOFOLLOW just below
        try:
            nxt = _open_dir_nofollow(fd, part)
        finally:
            os.close(fd)
        fd = nxt
    return fd


def _write_nofollow(root_fd: int, rel: Path, data: bytes, mode: int) -> None:
    """Write *data* at *rel* below *root_fd*; no component of the path is ever followed."""
    dir_fd = _descend(root_fd, rel.parts[:-1])
    tmp = f".{rel.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            raise ShadowError("symlink_in_destination") from exc
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
        final = os.stat(rel.name, dir_fd=dir_fd, follow_symlinks=False) if _lexists(rel.name, dir_fd) else None
        if final is not None and not stat.S_ISREG(final.st_mode):
            raise ShadowError("symlink_in_destination")
        os.replace(tmp, rel.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        if _lexists(tmp, dir_fd):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    finally:
        os.close(dir_fd)


def _lexists(name: str, dir_fd: int) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _copy_skill(parent_fd: int, source_root: Path, slug: str, src: Path) -> dict[str, Any]:
    """Copy *src* to ``<slug>/`` below the directory *parent_fd*; nothing is opened by a multi-component path."""
    files = []
    root_fd = _descend(parent_fd, (slug,))
    try:
        for path in _skill_files(src):
            rel = path.relative_to(src)
            data = path.read_bytes()
            _write_nofollow(root_fd, rel, data, path.stat().st_mode & 0o777)
            files.append({"path": rel.as_posix(), "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    finally:
        os.close(root_fd)
    real_root = Path(os.path.realpath(source_root))
    return {"slug": slug, "source_dir": src.relative_to(real_root).as_posix(), "files": files}


def _refuse_dest_tree(wt: Path, slug: str, src: Path) -> None:
    """Refuse before any write when the destination tree holds a link, a wrong-typed entry or leaves *wt*."""
    real_wt = Path(os.path.realpath(wt))
    dest = wt / NATIVE_DIR / slug
    chain = [wt / ".claude", wt / NATIVE_DIR, dest]
    for _dirpath, dirs, names in os.walk(dest):
        chain += [Path(_dirpath) / n for n in dirs + names]
    for rel in (p.relative_to(src) for p in _skill_files(src)):
        chain += [dest.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts) + 1)]
    for entry in chain:
        try:
            mode = os.lstat(entry).st_mode
        except FileNotFoundError:
            continue
        except NotADirectoryError as exc:
            raise ShadowError("symlink_in_destination") from exc
        if stat.S_ISLNK(mode):
            raise ShadowError("symlink_in_destination")
        if not _inside(Path(os.path.realpath(entry)), real_wt):
            raise ShadowError("path_outside_worktree")
    _refuse_wrong_types(dest, src)


def _refuse_wrong_types(dest: Path, src: Path) -> None:
    """A file where a directory belongs, or a directory where a file belongs, refuses the skill."""
    for path in _skill_files(src):
        rel = path.relative_to(src)
        for i in range(1, len(rel.parts)):
            parent = dest.joinpath(*rel.parts[:i])
            if os.path.lexists(parent) and not stat.S_ISDIR(os.lstat(parent).st_mode):
                raise ShadowError("symlink_in_destination")
        target = dest / rel
        if os.path.lexists(target) and not stat.S_ISREG(os.lstat(target).st_mode):
            raise ShadowError("symlink_in_destination")


def materialize_skill_set(
    role: str | None,
    *,
    worktree: Path | str | None,
    provider: str,
    mode: str,
    dispatch_id: str | None,
    state_dir: Path | str | None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Make *role*'s declared skill set visible to *provider* (ADR-059 decision 13).

    ``shadow`` delegates to :func:`materialize_role_skills`. ``on`` with ``claude``
    copies to ``<worktree>/.claude/skills/<slug>/``, writes one record per attempt
    and returns ``status`` in :data:`STATUSES`. Anything else is ``skipped`` without
    a write (``off``, or a provider other than claude). Never reads the environment
    and never raises: the dispatch runs on.
    """
    if mode == "shadow":
        return _shadow_delegate(worktree, role)
    if mode != "on":
        return {"status": "skipped", "reason": "layout_off"}
    if provider != "claude":
        return {"status": "skipped", "reason": "provider_unsupported"}
    ids = {"role": role, "provider": provider, "dispatch_id": dispatch_id, "project_id": project_id,
           "worktree": str(worktree) if worktree else None}
    try:
        result = _native_claude(role, worktree, dispatch_id, state_dir, project_id)
    except Exception as exc:  # fail-open: materialisation never stops a dispatch
        result = _outcome("error", f"copy_failed:{type(exc).__name__}")
    if result["status"] != "ok":
        logger.warning("agent skill materialisation for role %r: %s", role, result.get("reason"))
    result["record"] = _write_record(state_dir, ids, result)
    return result


def _shadow_delegate(worktree: Path | str | None, role: str | None) -> dict[str, Any]:
    if not role or worktree is None:
        return {"status": "skipped", "reason": "no_role" if not role else "no_cwd"}
    from agent_resolver import resolve_agent

    config = resolve_agent(role, worktree, engine_root=Path(__file__).resolve().parents[2])
    if config is None:
        logger.warning("agent skill shadow for role %r: no config", role)
        return {"status": "skipped", "reason": "no_role"}
    if not config.skills:
        return {"status": "skipped", "reason": "no_declared_set"}
    return materialize_role_skills(worktree, role, config.skills)


def _outcome(status: str, reason: str | None = None, skills: list | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "skills": skills or []}


def _native_claude(role, worktree, dispatch_id, state_dir, project_id) -> dict[str, Any]:
    for value, reason in ((role, "no_role"), (worktree, "no_cwd"), (state_dir, "no_state_dir"),
                          (dispatch_id, "no_dispatch_id"), (project_id, "no_project_id")):
        if not value:
            return _outcome("skipped", reason)
    wt = Path(worktree)
    if not _worktree_is_isolated(wt):
        return _outcome("skipped", "path_outside_worktree")
    from agent_resolver import resolve_agent

    config = resolve_agent(role, wt, engine_root=Path(__file__).resolve().parents[2])
    if config is None:
        return _outcome("skipped", "no_role")
    if not config.skills:
        return _outcome("skipped", "no_declared_set")
    return _copy_native(wt, config.skills)


def _worktree_is_isolated(wt: Path) -> bool:
    from dispatch_worktree_isolation import resolve_consumer_project_root

    real = wt.resolve()
    if real == resolve_consumer_project_root():
        return False
    return not any(_inside(real, (Path.home() / d).resolve()) for d in (".claude", ".codex"))


def _copy_native(wt: Path, slugs: list[str]) -> dict[str, Any]:
    base = wt / NATIVE_DIR
    if base.exists() and not _inside(Path(os.path.realpath(base)), Path(os.path.realpath(wt))):
        return _outcome("skipped", "path_outside_worktree")
    pending = [s for s in slugs if skill_search_paths(wt, s) and not os.path.lexists(base / s / "SKILL.md")]
    refusal = _first_dest_refusal(wt, pending)
    if refusal:
        return _outcome("skipped", refusal)
    if any(not _dest_ignored(wt, s) for s in pending):
        return _outcome("skipped", "not_ignored")
    entries: list[dict[str, Any]] = []
    failure: str | None = None
    for slug in slugs:
        try:
            entries.append(_native_entry(wt, base, slug, slug in pending))
        except ShadowError as exc:
            if exc.reason in DEST_REFUSALS:
                return _outcome("skipped", exc.reason)
            failure = failure or ("source_missing" if exc.reason.startswith("slug_") else exc.reason)
        except Exception as exc:  # fail-open per skill: the next slug still gets its copy
            failure = failure or f"copy_failed:{type(exc).__name__}"
    copied = any(e["ownership"] == "materialized" for e in entries)
    if failure:
        return _outcome("partial" if entries else "error", failure, entries)
    return _outcome("ok", None if copied else "already_native", entries)


def _first_dest_refusal(wt: Path, pending: list[str]) -> str | None:
    for slug in pending:
        try:
            _refuse_dest_tree(wt, slug, _vet_slug(wt, slug))
        except ShadowError as exc:
            if exc.reason in DEST_REFUSALS:
                return exc.reason
            # vnx-silent-except: an unknown slug is reported per skill by _native_entry
    return None


def _dest_ignored(wt: Path, slug: str) -> bool:
    proc = subprocess.run(["git", "-C", str(wt), "check-ignore", "-q", f"{NATIVE_DIR}/{slug}/SKILL.md"],
                          capture_output=True, check=False)
    return proc.returncode == 0


def _native_entry(wt: Path, base: Path, slug: str, to_copy: bool) -> dict[str, Any]:
    if to_copy:
        src = _vet_slug(wt, slug)
        root_fd = _open_worktree(wt)
        try:
            base_fd = _descend(root_fd, tuple(Path(NATIVE_DIR).parts))
            try:
                _copy_skill(base_fd, wt, slug, src)
            finally:
                os.close(base_fd)
        finally:
            os.close(root_fd)
    elif not skill_search_paths(wt, slug):
        raise ShadowError(f"slug_refused:{slug}")
    data = _read_below_worktree(wt, (*Path(NATIVE_DIR).parts, slug, "SKILL.md"))
    return {"slug": slug, "dest": f"{NATIVE_DIR}/{slug}", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "ownership": "materialized" if to_copy else "native"}


def _write_record(state_dir, ids: dict[str, Any], result: dict[str, Any]) -> str | None:
    """One file per attempt, ``n`` chosen with ``O_CREAT|O_EXCL``; best effort."""
    dispatch_id = ids["dispatch_id"]
    if not state_dir or not dispatch_id or dispatch_id != Path(str(dispatch_id)).name:
        return None
    skills = result["skills"]
    body = {"schema": RECORD_SCHEMA, "status": result["status"], "reason": result["reason"], **ids,
            "session_id": None, "captured_at": datetime.now(timezone.utc).isoformat(), "skills": skills,
            "manifest_sha256": hashlib.sha256(json.dumps(skills, sort_keys=True).encode()).hexdigest()}
    try:
        folder = Path(state_dir) / RECORD_DIR / dispatch_id
        folder.mkdir(parents=True, exist_ok=True)
        attempt = 1
        while True:
            path = folder / f"{ids['provider']}.attempt-{attempt}.json"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                attempt += 1
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # vnx-atomic-write: O_EXCL create is the exclusivity
            json.dump({**body, "attempt": attempt}, handle, indent=2)
        return str(path)
    except Exception as exc:  # fail-open: the reason is already in the WARNING
        logger.warning("could not write materialisation record: %s", type(exc).__name__)
        return None
