"""Shadow copy of an agent role's skills (ADR-050 contract 5, agents-als-map PR-2a).

Under ``VNX_AGENT_LAYOUT=shadow`` the skills a role declares in
``agents/<role>/config.yaml`` are copied to
``<worktree>/.vnx-agent-skills/<role>/<slug>/`` with a ``manifest.json`` (path,
length, sha256 per file). No harness discovers that directory, git ignores it,
and it goes away with the worktree.

The shadow fails open: any error gives one WARNING and ``status: error`` in the
result (and in ``manifest.json`` once the role folder is claimed). Nothing here
raises to the caller. Under ``off`` nothing happens and no directory is made.

Every removal is bound to one directory fd (skilldisc R4 r3). A directory is entered
only when this run made it (one ``mkdir``, then opened and found empty) or validated it
through the fd it still holds. It is emptied through that fd (:func:`_empty_fd`: every
subdirectory opened with ``O_NOFOLLOW`` relative to its parent fd, a link unlinked and
never followed), and only the empty entry is removed by name, by an ``rmdir`` right
after its ``st_dev``/``st_ino`` matched the held fd (:func:`_rmdir_if_same`). A file is
unlinked by name only while it is the inode this run wrote or identified
(:func:`_unlink_if_same`). No entry is ever replaced.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import subprocess
import uuid
from dataclasses import dataclass
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
NATIVE_PARTS = tuple(Path(NATIVE_DIR).parts)
CODEX_PARTS = (".agents", "skills")
DESTS = {"claude": NATIVE_PARTS, "codex": CODEX_PARTS}
MARKER_NAME = ".vnx-managed.json"
MARKER_SCHEMA = 1
SKILL_FILE = Path("SKILL.md")
RECORD_DIR = "agent_skill_materializations"
RECORD_SCHEMA = 1
STATUSES = frozenset({"ok", "partial", "error", "skipped"})
REASONS = frozenset({
    "no_role", "no_declared_set", "no_cwd", "no_state_dir", "no_dispatch_id",
    "no_project_id", "not_isolated", "not_ignored", "already_native",
    "source_missing", "copy_failed:<Class>", "not_owned:<slug>", "path_outside_worktree", "symlink_in_destination",
})

DEST_REFUSALS = frozenset({"symlink_in_destination", "path_outside_worktree"})

# A skill file is written to ``.<name>.vnx-partial-<uuid4 hex>`` first and only then
# linked to its final name. A killed run can leave such a name behind; nothing reads
# it, counts it or removes it (ADR-059 decision 6), and ``.gitignore`` ignores it.
PARTIAL_MARK = ".vnx-partial-"
NO_LINK_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP})

# Every directory below a held fd is opened with these: a link is never followed, a FIFO never blocks the open.
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
NOT_A_DIR_ERRNOS = frozenset({errno.ENOTDIR, errno.ELOOP})
DIR_IN_THE_WAY_ERRNOS = frozenset({errno.EPERM, errno.EISDIR})
NOT_EMPTY_ERRNOS = frozenset({errno.ENOTEMPTY, errno.EEXIST})


class ShadowError(Exception):
    """A refusal with a stable reason string; caught inside the module."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LinkUnsupported(OSError):
    """The filesystem refuses the hard link that publishes a skill file; the slug is ``copy_failed:LinkUnsupported``."""


class IncompleteWrite(OSError):
    """The private copy holds another length than the source after the write; it is never published."""


@dataclass(frozen=True)
class _Source:
    """A skill read once, before any destination is touched: a copy writes exactly these files and bytes."""

    root: Path
    files: tuple[tuple[Path, bytes, int], ...]

    @property
    def skill_md(self) -> bytes:
        return next(data for rel, data, _mode in self.files if rel == SKILL_FILE)


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
        return _fail(role, exc.reason)
    except Exception as exc:  # fail-open: the shadow never stops a dispatch
        return _fail(role, f"unexpected:{type(exc).__name__}")


def _fail(role: str, reason: str, role_fd: int | None = None) -> dict[str, Any]:
    """One WARNING and the error result; with *role_fd* the reason also goes into that folder's manifest.

    *role_fd* is the role folder this run claimed and still holds. An error before the
    claim writes no manifest: whatever stands at the role name then is not this run's.
    """
    logger.warning("agent skill shadow for role %r: %s", role, reason)
    result: dict[str, Any] = {"status": "error", "reason": reason, "role": role}
    if role_fd is not None:
        try:
            _write_manifest(role_fd, {"schema": MANIFEST_SCHEMA, **result})
        except Exception as exc:  # fail-open: the reason is already in the WARNING
            logger.warning("could not write error manifest: %s", type(exc).__name__)
    return result


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
    _checked_base(wt)
    root_fd = _open_worktree(wt)
    try:
        role_fd = _claim_role_dir(root_fd, role)
    finally:
        os.close(root_fd)
    try:
        return _fill_role_dir(role_fd, wt, role, slugs, source_root)
    finally:
        os.close(role_fd)


def _fill_role_dir(role_fd: int, wt: Path, role: str, slugs: list[str], source_root: Path) -> dict[str, Any]:
    """Read every source, then copy each below the claimed *role_fd*; the manifest, ok or error, is written there."""
    try:
        sources: dict[str, _Source] = {}
        for slug in slugs:
            sources.setdefault(slug, _load_source(source_root, slug))
        entries = [_copy_skill(role_fd, source_root, slug, src) for slug, src in sources.items()]
    except ShadowError as exc:
        return _fail(role, exc.reason, role_fd)
    except Exception as exc:  # fail-open: the shadow never stops a dispatch
        return _fail(role, f"unexpected:{type(exc).__name__}", role_fd)
    _write_manifest(role_fd, {"schema": MANIFEST_SCHEMA, "status": "ok", "role": role, "skills": entries})
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


def _load_source(source_root: Path, slug: str) -> _Source:
    """Vet *slug* and read every file of its skill now; a copy never re-reads or re-lists the source."""
    src = _vet_slug(source_root, slug)
    files = tuple((p.relative_to(src), p.read_bytes(), p.stat().st_mode & 0o777) for p in _skill_files(src))
    if not any(rel == SKILL_FILE for rel, _data, _mode in files):
        raise ShadowError(f"slug_unknown:{slug}")
    return _Source(src, files)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_worktree(wt: Path) -> int:
    """The one open by absolute path: the worktree root the caller passed in (trusted)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(os.path.realpath(wt), flags)


def _claim_role_dir(root_fd: int, role: str) -> int:
    """Clear ``<SHADOW_DIR>/<role>`` through one fd, then claim a fresh one with one ``mkdir``; returns its fd.

    The previous folder is removed by :func:`_clear_role_dir`. Whatever takes the name
    after that, before or right after the new ``mkdir``, is never entered: the run
    refuses with ``role_dir_replaced``. Nothing is written into a role folder this run
    did not make.
    """
    base_fd = _descend(root_fd, (SHADOW_DIR,))
    try:
        _clear_role_dir(base_fd, role)
        try:
            os.mkdir(role, 0o755, dir_fd=base_fd)
            return _open_claimed(base_fd, role)
        except FileExistsError as exc:
            raise ShadowError("role_dir_replaced") from exc
    finally:
        os.close(base_fd)


def _clear_role_dir(base_fd: int, role: str) -> None:
    """Remove the role folder a previous run left; the folder that is opened is the folder that is emptied.

    The folder is opened once without following a link and emptied through that fd; its
    entry is removed only while it is still that folder (:func:`_remove_held`). A folder
    swapped in at the name meanwhile stays and refuses the run (``role_dir_replaced``). A
    link at the name refuses (``role_dir_symlink``); another non-directory is unlinked,
    never followed.
    """
    try:
        role_fd = os.open(role, DIR_FLAGS, dir_fd=base_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno not in NOT_A_DIR_ERRNOS:
            raise
        info = os.stat(role, dir_fd=base_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ShadowError("role_dir_symlink") from exc
        if not _unlink_if_same(base_fd, role, _ident(info)):
            raise ShadowError("role_dir_replaced") from exc
        return
    try:
        if not _remove_held(base_fd, role, role_fd):
            raise ShadowError("role_dir_replaced")
    finally:
        os.close(role_fd)


def _ident(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _entry_ident(parent_fd: int, name: str) -> tuple[int, int] | None:
    """``st_dev``/``st_ino`` of the entry *name* below *parent_fd* itself (a link is not followed); None when it is gone."""
    try:
        return _ident(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _rmdir_if_same(parent_fd: int, name: str, held_fd: int) -> bool:
    """Remove the empty entry *name* below *parent_fd* only while it is the directory *held_fd* holds.

    The entry's ``st_dev``/``st_ino`` is compared with ``fstat`` of the held fd right
    before the ``rmdir``; any other entry stays. ``rmdir`` refuses a directory that is not
    empty, so between the comparison and the removal at most an empty directory can take
    the name: nothing with content is ever removed by name. True when the name no longer
    holds anything of the held directory (removed, or gone already); False leaves the entry.
    """
    now = _entry_ident(parent_fd, name)
    if now is None:
        return True
    if now != _ident(os.fstat(held_fd)):
        return False
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    except OSError as exc:
        if exc.errno in NOT_EMPTY_ERRNOS:
            return False
        raise
    return True


def _unlink_if_same(dir_fd: int, name: str, ident: tuple[int, int]) -> bool:
    """Unlink *name* below *dir_fd* only while it is the inode *ident* this run wrote or identified.

    True when the name is gone afterwards; False when another entry stands there (it stays).
    """
    now = _entry_ident(dir_fd, name)
    if now is None:
        return True
    return now == ident and _unlink_entry(dir_fd, name)


def _unlink_entry(dir_fd: int, name: str) -> bool:
    """Unlink the entry *name* below *dir_fd*: a link is removed, never followed, and a directory is never removed.

    True when the name is gone; False when a directory stands there (``unlink`` refuses it).
    """
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return True
    except OSError as exc:
        if exc.errno in DIR_IN_THE_WAY_ERRNOS:
            return False
        raise
    return True


def _empty_fd(dir_fd: int, keep: str | None = None) -> bool:
    """Remove every entry of the directory *dir_fd* holds, except *keep*; True only when all of them are gone.

    The listing is read through the fd, so the entries removed are entries of that
    directory, wherever it stands now. An entry that cannot be removed safely stays.
    """
    clean = True
    for name in os.listdir(dir_fd):
        if name != keep and not _remove_entry(dir_fd, name):
            clean = False
    return clean


def _remove_entry(parent_fd: int, name: str) -> bool:
    """Remove the entry *name* of a directory this run holds: a directory through its own fd, anything else by identity.

    A directory is opened with :data:`DIR_FLAGS` relative to *parent_fd* and must be the
    directory the ``lstat`` saw; it is emptied through that fd before its entry goes. A
    file, link, FIFO or socket is unlinked only while it is the inode the ``lstat`` saw.
    """
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(info.st_mode):
        return _unlink_if_same(parent_fd, name, _ident(info))
    try:
        child = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    except OSError as exc:
        if exc.errno in NOT_A_DIR_ERRNOS:
            return False  # a non-directory replaced it since the lstat: it stays
        raise
    try:
        return _ident(os.fstat(child)) == _ident(info) and _remove_held(parent_fd, name, child)
    finally:
        os.close(child)


def _remove_held(parent_fd: int, name: str, held_fd: int) -> bool:
    """Empty the directory *held_fd* through that fd, then remove its entry *name* while it is still that directory."""
    return _empty_fd(held_fd) and _rmdir_if_same(parent_fd, name, held_fd)


def _read_below_worktree(wt: Path, parts: tuple[str, ...]) -> bytes:
    """Read the regular file at *parts* below the worktree root; every component is opened without following a link.

    ``O_NONBLOCK`` plus the ``fstat`` check: a FIFO at the name would otherwise block the
    open forever, and a directory would raise; both refuse as a wrong-typed entry instead.
    """
    fd = _open_worktree(wt)
    try:
        for part in parts[:-1]:
            nxt = _open_dir_nofollow(fd, part)
            os.close(fd)
            fd = nxt
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as exc:
            raise ShadowError("symlink_in_destination") from exc
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise ShadowError("symlink_in_destination")
        with os.fdopen(file_fd, "rb") as handle:
            return handle.read()
    finally:
        os.close(fd)


def _write_manifest(role_fd: int, manifest: dict[str, Any]) -> None:
    """Publish ``manifest.json`` in the role folder this run claimed: written once, it never replaces an entry."""
    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    _publish_file(role_fd, MANIFEST_NAME, data, 0o644)


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
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ShadowError("symlink_in_destination") from exc


def _descend(root_fd: int, parts: tuple[str, ...]) -> int:
    """Create-and-enter each directory of *parts* below *root_fd*; a link or file fails the write.

    Only for the shared parent folders (``.vnx-agent-skills``, ``.claude/skills``,
    ``.agents/skills``). Inside one this module claims a new name with ``mkdir``, removes
    only an entry it validated through a held fd, and never writes a file into it itself.
    """
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


def _publish_file(dir_fd: int, name: str, data: bytes, mode: int) -> tuple[int, int]:
    """The write-then-link publish of one file into the already opened directory *dir_fd*.

    Returns the ``st_dev``/``st_ino`` of the published file, so a take-back unlinks the
    final name only while it is still this file.
    """
    tmp, ident = _write_partial(dir_fd, name, data, mode)
    try:
        _publish(dir_fd, tmp, name)
    finally:
        _unlink_if_same(dir_fd, tmp, ident)
    return ident


def _write_partial(dir_fd: int, name: str, data: bytes, mode: int) -> tuple[str, tuple[int, int]]:
    """Write *data* to a new private name next to *name*, synced and length-checked; a failure removes it again."""
    tmp = f".{name}{PARTIAL_MARK}{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    ident = _ident(os.fstat(fd))
    try:
        with os.fdopen(fd, "wb") as handle:  # vnx-atomic-write: private O_EXCL name, published by os.link
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            if os.fstat(handle.fileno()).st_size != len(data):
                raise IncompleteWrite(errno.EIO, f"{name}: the private copy is not {len(data)} bytes")
    except BaseException:
        _unlink_if_same(dir_fd, tmp, ident)
        raise
    return tmp, ident


def _publish(dir_fd: int, tmp: str, name: str) -> None:
    """Hard-link the complete *tmp* to *name* below *dir_fd*; never a fallback write to *name* itself.

    A link at *name* refuses the run (``symlink_in_destination``); any other entry raises
    FileExistsError for the caller to name ``not_owned:<slug>``. A filesystem without hard
    links (``EPERM``/``ENOTSUP``) raises :class:`LinkUnsupported`.
    """
    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            raise ShadowError("symlink_in_destination") from None
        raise
    except OSError as exc:
        if exc.errno in NO_LINK_ERRNOS:
            raise LinkUnsupported(exc.errno, f"{name}: {exc.strerror}") from exc
        raise


def _copy_skill(parent_fd: int, source_root: Path, slug: str, src: _Source,
                marker: bytes | None = None) -> dict[str, Any]:
    """Claim ``<slug>/`` below *parent_fd* with one ``mkdir`` and copy every file of *src* into it.

    The claim is the only way in: a directory that exists by now (``FileExistsError``) was
    not created by this run and nothing is written into it, no marker and no file. The
    folder is opened right after the ``mkdir`` and must still be empty
    (:func:`_open_claimed`), so a folder swapped in between is never entered either. The
    same holds for every nested directory of the skill, and each one is held open for the
    whole copy, never re-opened by name. Every file appears at its final name only with
    all its bytes (:func:`_publish_file`), and SKILL.md is published last, after every
    folder of the copy proved it still stands at its name (:func:`_verify_claims`), so a
    SKILL.md at the final name is complete and sits in this run's own folder.

    A *marker* is published first, below the claimed fd. A failed copy keeps it (the next
    attempt of the same dispatch may reclaim the folder), except on ``FileExistsError``: a
    racing entry means the folder is no longer solely this run's, so everything this run
    published is taken back and the marker goes with it. Without a *marker* a failed copy
    takes back what it made. Only entries this run created are ever removed, each through
    the fd it was made in and only while it is still the entry this run made.
    """
    os.mkdir(slug, 0o755, dir_fd=parent_fd)
    slug_fd = _open_claimed(parent_fd, slug)
    made: dict[tuple[str, ...], int] = {}
    published: list[tuple[int, str, tuple[int, int]]] = []
    keep = False
    try:
        if marker is not None:
            published.append((slug_fd, MARKER_NAME, _publish_file(slug_fd, MARKER_NAME, marker, 0o644)))
        try:
            for rel, data, mode in sorted(src.files, key=lambda item: item[0] == SKILL_FILE):
                dir_fd = _claim_dirs(slug_fd, rel.parts[:-1], made)
                if rel == SKILL_FILE:
                    _verify_claims(parent_fd, slug, slug_fd, made)
                published.append((dir_fd, rel.name, _publish_file(dir_fd, rel.name, data, mode)))
        except FileExistsError:
            raise
        except BaseException:
            keep = marker is not None
            raise
    except BaseException:
        if not keep:
            _take_back(parent_fd, slug, slug_fd, made, published)
        raise
    finally:
        for fd in made.values():
            os.close(fd)
        os.close(slug_fd)
    files = [{"path": rel.as_posix(), "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for rel, data, _mode in src.files]
    real_root = Path(os.path.realpath(source_root))
    return {"slug": slug, "source_dir": src.root.relative_to(real_root).as_posix(), "files": files}


def _open_claimed(parent_fd: int, name: str) -> int:
    """Open the directory this run just made at *name*; it must still be empty, or it is not this run's claim.

    ``mkdir`` returns no handle, so the folder is opened by name right after it. A link
    there refuses the run (``symlink_in_destination``); a directory that already holds an
    entry was put in its place, so FileExistsError is raised and it is never entered.
    """
    fd = _open_dir_nofollow(parent_fd, name)
    try:
        if os.listdir(fd):
            raise FileExistsError(errno.EEXIST, f"{name}: not the empty folder this run made")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _claim_dirs(slug_fd: int, parts: tuple[str, ...], made: dict[tuple[str, ...], int]) -> int:
    """The fd of directory *parts* below *slug_fd*; each directory on the way is made by this copy and held in *made*.

    A directory is made with one ``mkdir`` and opened once (:func:`_open_claimed`); a later
    file in it goes through the held fd, never through a new lookup of its name. The
    returned fd is borrowed from *made* (or is *slug_fd*): the caller does not close it.
    """
    fd = slug_fd
    for depth in range(1, len(parts) + 1):
        key = parts[:depth]
        if key not in made:
            os.mkdir(key[-1], 0o755, dir_fd=fd)
            made[key] = _open_claimed(fd, key[-1])
        fd = made[key]
    return fd


def _verify_claims(parent_fd: int, slug: str, slug_fd: int, made: dict[tuple[str, ...], int]) -> None:
    """Raise FileExistsError unless the claimed folder and every folder the copy made still stand at their names."""
    held = [(parent_fd, slug, slug_fd)]
    held += [(made.get(key[:-1], slug_fd), key[-1], fd) for key, fd in made.items()]
    for parent, name, fd in held:
        if _entry_ident(parent, name) != _ident(os.fstat(fd)):
            raise FileExistsError(errno.EEXIST, f"{name}: no longer the folder this copy made")


def _take_back(parent_fd: int, slug: str, slug_fd: int, made: dict[tuple[str, ...], int],
               published: list[tuple[int, str, tuple[int, int]]]) -> None:
    """Unlink what this copy published, then the directories it made, deepest first, then the claimed folder.

    Each entry is removed through the fd it was made in and only while it is still the
    entry this copy made (:func:`_unlink_if_same`, :func:`_rmdir_if_same`). A directory
    that is not empty (something else moved in) or that another entry replaced is left,
    and nothing foreign is touched.
    """
    for dir_fd, name, ident in reversed(published):
        try:
            _unlink_if_same(dir_fd, name, ident)
        except OSError:
            continue  # vnx-silent-except: best-effort take-back of this run's own entry; a leftover stays foreign-safe
    for key in sorted(made, key=len, reverse=True):
        try:
            _rmdir_if_same(made.get(key[:-1], slug_fd), key[-1], made[key])
        except OSError:
            continue  # vnx-silent-except: rmdir refuses a directory something else filled; it stays
    try:
        _rmdir_if_same(parent_fd, slug, slug_fd)
    except OSError:
        pass  # vnx-silent-except: rmdir refuses a folder that still holds entries; it stays


def _refuse_dest_tree(wt: Path, slug: str, src: _Source, parts: tuple[str, ...] = NATIVE_PARTS) -> None:
    """Refuse before any write when the destination tree holds a link, a wrong-typed entry or leaves *wt*."""
    real_wt = Path(os.path.realpath(wt))
    dest = wt.joinpath(*parts, slug)
    chain = [wt / parts[0], wt.joinpath(*parts), dest]
    for _dirpath, dirs, names in os.walk(dest):
        chain += [Path(_dirpath) / n for n in dirs + names]
    for rel, _data, _mode in src.files:
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


def _refuse_wrong_types(dest: Path, src: _Source) -> None:
    """A file where a directory belongs, or a directory where a file belongs, refuses the skill."""
    for rel, _data, _mode in src.files:
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

    ``shadow`` delegates to :func:`materialize_role_skills`. ``on`` copies to
    ``<worktree>/.claude/skills/<slug>/`` (claude) or ``<worktree>/.agents/skills/<slug>/``
    (codex, each folder with an ownership marker), writes one record per attempt
    and returns ``status`` in :data:`STATUSES`. Anything else is ``skipped`` without
    a write (``off``, or a provider without a destination). Never reads the
    environment and never raises: the dispatch runs on.
    """
    if mode == "shadow":
        return _shadow_delegate(worktree, role)
    if mode != "on":
        return {"status": "skipped", "reason": "layout_off"}
    if provider not in DESTS:
        return {"status": "skipped", "reason": "provider_unsupported"}
    ids = {"role": role, "provider": provider, "dispatch_id": dispatch_id, "project_id": project_id,
           "worktree": str(worktree) if worktree else None}
    try:
        result = _native(provider, role, worktree, dispatch_id, state_dir, project_id)
    except Exception as exc:  # fail-open: materialisation never stops a dispatch
        result = _outcome("error", f"copy_failed:{type(exc).__name__}")
    if result["status"] != "ok":
        logger.warning("agent skill materialisation for role %r: %s", role, result.get("reason"))
    result["record"] = _write_record(state_dir, ids, result)
    return result


def record_skipped(reason: str, *, role: str | None, provider: str, worktree: Path | str | None,
                   dispatch_id: str | None, state_dir: Path | str | None, project_id: str | None) -> dict[str, Any]:
    """A caller that decides before the materialiser runs (the route is not isolated) writes the record itself."""
    logger.warning("agent skill materialisation for role %r: %s", role, reason)
    result = _outcome("skipped", reason)
    ids = {"role": role, "provider": provider, "dispatch_id": dispatch_id, "project_id": project_id,
           "worktree": str(worktree) if worktree else None}
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


def _native(provider, role, worktree, dispatch_id, state_dir, project_id) -> dict[str, Any]:
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
    owner = {"dispatch_id": dispatch_id, "role": role} if provider == "codex" else None
    return _copy_native(wt, config.skills, DESTS[provider], owner)


def _worktree_is_isolated(wt: Path) -> bool:
    from dispatch_worktree_isolation import resolve_consumer_project_root

    real = wt.resolve()
    if real == resolve_consumer_project_root():
        return False
    return not any(_inside(real, (Path.home() / d).resolve()) for d in (".claude", ".codex"))


def _copy_native(wt: Path, slugs: list[str], parts: tuple[str, ...] = NATIVE_PARTS,
                 owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decision 6 per destination file: every check for every slug runs before the first byte is written.

    Order: the native reads (``already_native``), the on-disk tree of each pending slug
    (a link, a wrong type or an escape refuses the whole run), then one pass that proves
    every file new, untracked and ignored (a slug that fails is refused, the rest copies).
    With an *owner* (codex) the folders marked for this run are reclaimed first, so every
    declared slug is pending; an unmarked folder is foreign and stays (``not_owned``).
    """
    base = wt.joinpath(*parts)
    if base.exists() and not _inside(Path(os.path.realpath(base)), Path(os.path.realpath(wt))):
        return _outcome("skipped", "path_outside_worktree")
    slugs = list(dict.fromkeys(slugs))
    failures = {s: "source_missing" for s in slugs if not skill_search_paths(wt, s)}
    try:
        foreign = _reclaim_owned(wt, parts, owner) if owner else []
        failures.update({s: f"not_owned:{s}" for s in slugs if s in foreign and s not in failures})
        valid = [s for s in slugs if s not in failures]
        pending = [s for s in valid if owner or not os.path.lexists(base / s / "SKILL.md")]
        entries = {s: _native_entry(wt, s, parts) for s in valid if s not in pending}
        sources = _vet_pending(wt, pending, failures, parts)
        failures.update(_refuse_foreign_files(wt, sources, parts))
    except ShadowError as exc:
        if exc.reason not in DEST_REFUSALS:
            raise
        return _outcome("skipped", exc.reason)
    stop = _write_pending(wt, sources, entries, failures, parts, owner)
    extra = [name for name in foreign if name not in slugs]
    failures.update({name: f"not_owned:{name}" for name in extra})
    return _native_outcome(slugs + extra, entries, failures, stop)


def _reclaim_owned(wt: Path, parts: tuple[str, ...], owner: dict[str, Any]) -> list[str]:
    """Remove every folder below the destination marked for this run; return the names that stay (foreign).

    The destination is opened once below the worktree fd and listed through that fd; each
    entry is decided and removed by :func:`_reclaim_one`. A folder is owned only when it
    carries ``.vnx-managed.json`` with this run's ``dispatch_id``, its own ``slug`` and the
    marker schema. Nothing else is ever removed.
    """
    root_fd = _open_worktree(wt)
    try:
        base_fd = _open_existing(root_fd, parts)
    finally:
        os.close(root_fd)
    if base_fd is None:
        return []
    try:
        return [name for name in sorted(os.listdir(base_fd))
                if not _reclaim_one(base_fd, name, owner["dispatch_id"])]
    finally:
        os.close(base_fd)


def _open_existing(root_fd: int, parts: tuple[str, ...]) -> int | None:
    """Open the directories *parts* below *root_fd* without following a link; None when one does not exist yet.

    A link or a non-directory on the way refuses the run (``symlink_in_destination``).
    """
    fd = os.dup(root_fd)
    for part in parts:
        try:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            nxt = None
        except OSError as exc:
            os.close(fd)
            raise ShadowError("symlink_in_destination") from exc
        os.close(fd)
        if nxt is None:
            return None
        fd = nxt
    return fd


def _reclaim_one(base_fd: int, name: str, dispatch_id: str) -> bool:
    """Remove the folder *name* below *base_fd* when it is this run's; True only when its name is free afterwards.

    The folder is opened once. Its marker is read and checked through that fd
    (:func:`_owned_marker`), the folder is emptied through that same fd with the marker
    last, and only the empty entry is removed by name, while it is still that folder
    (:func:`_rmdir_if_same`). A folder that is not owned, or that another process replaced
    or filled meanwhile, is left as it is and counts as foreign.
    """
    try:
        slug_fd = os.open(name, DIR_FLAGS, dir_fd=base_fd)
    except OSError:
        return False  # fail-closed: a link, a file or a vanished name is no owned folder; nothing is removed
    try:
        marker = _owned_marker(slug_fd, name, dispatch_id)
        if marker is None:
            return False
        emptied = _empty_fd(slug_fd, keep=MARKER_NAME)
        unmarked = _unlink_if_same(slug_fd, MARKER_NAME, marker)
        return emptied and unmarked and _rmdir_if_same(base_fd, name, slug_fd)
    finally:
        os.close(slug_fd)


def _owned_marker(slug_fd: int, name: str, dispatch_id: str) -> tuple[int, int] | None:
    """The ``st_dev``/``st_ino`` of this run's marker in the folder *slug_fd* holds; None for anything else (fail-closed).

    The marker is opened below *slug_fd*, so the folder the decision is about is the folder
    that is emptied afterwards. It must be a regular file with one link (a symlink, FIFO,
    directory or a hard link to another file is foreign), and carry the marker schema,
    this folder's own ``slug`` and this run's ``dispatch_id``.
    """
    try:
        fd = os.open(MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=slug_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            marker = json.loads(handle.read())
    except ValueError:
        return None
    finally:
        os.close(fd)
    owned = (isinstance(marker, dict) and marker.get("schema") == MARKER_SCHEMA
             and marker.get("slug") == name and marker.get("dispatch_id") == dispatch_id)
    return _ident(info) if owned else None


def _marker_bytes(owner: dict[str, Any], slug: str, src: _Source) -> bytes:
    marker = {"schema": MARKER_SCHEMA, "dispatch_id": owner["dispatch_id"], "role": owner["role"],
              "slug": slug, "sha256": hashlib.sha256(src.skill_md).hexdigest()}
    return json.dumps(marker, indent=2).encode("utf-8")


def _vet_pending(wt: Path, pending: list[str], failures: dict[str, str],
                 parts: tuple[str, ...] = NATIVE_PARTS) -> dict[str, _Source]:
    """Read each pending source once; a destination link, wrong type or escape raises for the whole run."""
    sources: dict[str, _Source] = {}
    for slug in pending:
        try:
            src = _load_source(wt, slug)
        except ShadowError:
            failures[slug] = "source_missing"  # unknown slug, outside the skill trees, or a link leaving it
            continue
        _refuse_dest_tree(wt, slug, src, parts)
        sources[slug] = src
    return sources


def _refuse_foreign_files(wt: Path, sources: dict[str, _Source],
                          parts: tuple[str, ...] = NATIVE_PARTS) -> dict[str, str]:
    """The pending slugs whose copy would write a file that is not new, untracked and ignored.

    One ``git ls-files`` and one ``git check-ignore`` cover every file of every slug. A
    file that exists or that git tracks (also at a parent or child position) is foreign:
    ``not_owned:<slug>``. A file git does not positively report ignored: ``not_ignored``.
    The on-disk check runs last, component by component below the worktree fd.
    """
    plan = {slug: [(*parts, slug, *rel.parts) for rel, _data, _mode in src.files]
            for slug, src in sources.items()}
    if not plan:
        return {}
    listed = _tracked_paths(wt, parts)
    names = ["/".join(parts) for group in plan.values() for parts in group]
    # an index git cannot list proves nothing, so then no file of any slug counts as ignored
    ignored = set() if listed is None else _ignored_paths(wt, names)
    tracked = listed or set()
    refused: dict[str, str] = {}
    root_fd = _open_worktree(wt)
    try:
        for slug, group in plan.items():
            if any(_collides("/".join(parts), tracked) or _entry_below(root_fd, parts) for parts in group):
                refused[slug] = f"not_owned:{slug}"
            elif not _dest_ignored(["/".join(parts) for parts in group], ignored):
                refused[slug] = "not_ignored"
    finally:
        os.close(root_fd)
    return refused


def _tracked_paths(wt: Path, parts: tuple[str, ...] = NATIVE_PARTS) -> set[str] | None:
    """Every path the index of *wt* holds under the destination's top folder; None when git gives no answer."""
    proc = subprocess.run(["git", "-C", str(wt), "ls-files", "-z", "--", parts[0]],
                          capture_output=True, check=False)
    if proc.returncode != 0:
        return None
    return {os.fsdecode(p) for p in proc.stdout.split(b"\0") if p}


def _ignored_paths(wt: Path, names: list[str]) -> set[str]:
    """The *names* git reports ignored, in one ``check-ignore --stdin -z``; only rc 0 is evidence.

    rc 1 means none is ignored and any other rc is a git failure: both give the empty set.
    Git never reports a tracked path as ignored.
    """
    proc = subprocess.run(["git", "-C", str(wt), "check-ignore", "--stdin", "-z"],
                          input=b"".join(os.fsencode(n) + b"\0" for n in names), capture_output=True, check=False)
    if proc.returncode != 0:
        return set()
    return {os.fsdecode(p) for p in proc.stdout.split(b"\0") if p}


def _dest_ignored(names: list[str], ignored: set[str]) -> bool:
    """True only when git reported every destination file of one slug as ignored."""
    return all(name in ignored for name in names)


def _collides(name: str, tracked: set[str]) -> bool:
    """A tracked path at *name*, at one of its parent directories, or below it."""
    parts = name.split("/")
    parents = {"/".join(parts[:i]) for i in range(1, len(parts))}
    return name in tracked or not parents.isdisjoint(tracked) or any(t.startswith(name + "/") for t in tracked)


def _entry_below(root_fd: int, parts: tuple[str, ...]) -> bool:
    """True when a regular file exists at *parts* below *root_fd*, every directory opened without a link.

    A link anywhere on the way, a non-directory at a directory position or a non-file at
    the file position raises ``symlink_in_destination`` for the whole run.
    """
    fd = os.dup(root_fd)
    try:
        for depth, part in enumerate(parts, start=1):
            try:
                mode = os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
            except FileNotFoundError:
                return False
            if depth == len(parts):
                if not stat.S_ISREG(mode):
                    raise ShadowError("symlink_in_destination")
                return True
            if not stat.S_ISDIR(mode):
                raise ShadowError("symlink_in_destination")
            nxt = _open_dir_nofollow(fd, part)
            os.close(fd)
            fd = nxt
        return False
    finally:
        os.close(fd)


def _write_pending(wt: Path, sources: dict[str, _Source], entries: dict[str, dict[str, Any]],
                   failures: dict[str, str], parts: tuple[str, ...] = NATIVE_PARTS,
                   owner: dict[str, Any] | None = None) -> str | None:
    """Copy each slug that passed every check; a destination refusal stops the run and is returned."""
    for slug, src in sources.items():
        if slug in failures:
            continue
        try:
            entries[slug] = _materialize_one(wt, slug, src, parts, owner)
        except ShadowError as exc:
            failures[slug] = exc.reason
            if exc.reason in DEST_REFUSALS:
                return exc.reason
        except Exception as exc:  # fail-open per skill: the next slug still gets its copy
            failures[slug] = f"copy_failed:{type(exc).__name__}"
    return None


def _materialize_one(wt: Path, slug: str, src: _Source, parts: tuple[str, ...] = NATIVE_PARTS,
                     owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy one checked slug below the worktree fd; a folder that exists by now is foreign and stays.

    :func:`_copy_skill` claims the slug folder with one ``mkdir`` before anything is written
    into it, so ``FileExistsError`` (the folder, or a nested one) is ``not_owned:<slug>`` with
    no marker and no file written. With an *owner* the marker is published first below the
    claimed folder, so a copy that stops halfway is still this run's and the next attempt of
    the same dispatch can reclaim it.
    """
    root_fd = _open_worktree(wt)
    try:
        base_fd = _descend(root_fd, parts)
        try:
            if owner:
                _copy_skill(base_fd, wt, slug, src, marker=_marker_bytes(owner, slug, src))
            else:
                _copy_skill(base_fd, wt, slug, src)
        except FileExistsError as exc:
            raise ShadowError(f"not_owned:{slug}") from exc
        finally:
            os.close(base_fd)
    finally:
        os.close(root_fd)
    return _skill_entry(slug, src.skill_md, "materialized", parts)


def _native_entry(wt: Path, slug: str, parts: tuple[str, ...] = NATIVE_PARTS) -> dict[str, Any]:
    """The entry of a skill whose SKILL.md already exists; read below the worktree fd, never written."""
    return _skill_entry(slug, _read_below_worktree(wt, (*parts, slug, "SKILL.md")), "native", parts)


def _skill_entry(slug: str, data: bytes, ownership: str, parts: tuple[str, ...] = NATIVE_PARTS) -> dict[str, Any]:
    return {"slug": slug, "dest": "/".join((*parts, slug)), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "ownership": ownership}


def _is_refusal(reason: str) -> bool:
    return reason in DEST_REFUSALS or reason == "not_ignored" or reason.startswith("not_owned:")


def _native_outcome(slugs: list[str], entries: dict[str, dict[str, Any]], failures: dict[str, str],
                    stop: str | None) -> dict[str, Any]:
    """``partial`` when some declared skill is visible; with none, ``skipped`` for refusals only, else ``error``."""
    skills = [entries[s] for s in slugs if s in entries]
    reason = stop or next((failures[s] for s in slugs if s in failures), None)
    if reason is None:
        copied = any(e["ownership"] == "materialized" for e in skills)
        return _outcome("ok", None if copied else "already_native", skills)
    if skills:
        return _outcome("partial", reason, skills)
    return _outcome("skipped" if all(_is_refusal(r) for r in failures.values()) else "error", reason, skills)


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
