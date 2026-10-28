"""worktree_salvage.py — preserve a worktree's unpushed commits, its tracked
changes and the work outside the index (WT-2a1a/2a1b/2a2/2b).

:func:`salvage` preserves four kinds of tree state: the commits no ``origin``
ref reaches (``<name>.bundle``), the staged changes to tracked files
(``<name>.staged.patch``, ``git diff --cached --binary --full-index <head>``),
the unstaged changes to tracked files (``<name>.unstaged.patch``,
``git diff --binary --full-index``) and every entry git reports outside the
index that is not throwaway — ``<name>.untracked.tgz`` and its
``<name>.untracked.list``.

The untracked archive carries each ``?`` (untracked) and each ``!`` (ignored,
not throwaway) entry, except those below ``.vnx-data/`` or ``.git/``: that is
evidence, not work to pack, and it keeps blocking removal. An untracked symlink
:func:`worktree_inventory.is_internal_symlink` calls a convenience link into the
checkout (``.venv`` -> the checkout's venv) is covered without being carried. A
directory entry — git's ``path/`` record, which is how a nested repository is
reported — is walked, so every regular file and symlink below it is archived
(those inside a nested ``.git`` included), and a symlink is archived as a
symlink, never followed. ``d`` marks an EMPTY directory, the one member no file
implies, so an empty nested repository is never passed silently.

``<name>.untracked.list`` holds one NUL-terminated record per archived member,
sorted by path; the separator is NUL, so a filename may hold any byte but NUL::

    <kind> <size> <sha256> <path>\\0

``kind`` is ``f`` (file), ``l`` (symlink) or ``d`` (directory); ``size`` is the
byte count of the file or of the symlink's target, ``0`` for a directory;
``sha256`` is the hex digest of those bytes, a single ``-`` for a directory;
``path`` is relative to the tree, POSIX-separated, and runs to the closing NUL.
Kind, size and digest hold no space, so the first three fields parse whatever
the filename holds.

:func:`verify` returns ``[]`` only when the tree holds nothing else — ``git
status --porcelain=v2 -z --untracked-files=all --ignored=matching
--ignore-submodules=none`` names no entry this salvage does not cover (a tracked
entry with a staged or unstaged change is carried by a patch; an untracked or
non-throwaway ignored entry is carried by the untracked archive; an IGNORED
entry is excused only inside a throwaway cache DIRECTORY from
:data:`THROWAWAY_IGNORED_DIRS` and never below ``.vnx-data/`` or ``.git/`` —
WT-2a1b, OI-2417; a regular file that only shares a cache directory's name is
never excused), the untracked list parses, the archive opens with ``tarfile``,
every listed member is in the archive and every archive member in the list with
the same kind, size and sha256, every listed path is still in the tree as the
list records it (a file edited or deleted after the salvage is named, as are
members the tree gained and the list never carried), every gitlink whose
checkout exists is named even when it is clean (a submodule keeps its commits in
its own object store, so no superproject bundle can carry them; nothing here
classifies or excuses one — submodule bundling is OI-2411), a gitlink whose
checkout was never made is not named, HEAD is readable and still the salvaged
commit, every bundle is a non-empty bundle that unpacks to that head in a
scratch repository borrowing no object store of the checkout, and every patch is
proved by applying it in such a checkout: the staged patch must reproduce the
tree's own index tree (``git write-tree``, read through a temporary
``GIT_INDEX_FILE`` so the worktree's index is never written) and the unstaged
patch must reproduce every changed file's bytes, both for the paths its apply
changes and for the paths the worktree itself changed against its index — a
tracked change the tree makes after the salvage, and that no patch mentions, is
a named problem too. A failed apply or a mismatch is a named problem. An
artifact or ``dest_dir`` inside the tree is refused: removal would delete the
evidence. A real run writes ``dest_dir/<name>-<UTC>-<head[:8]>/*`` and appends
the MANIFEST line.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import itertools
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
STAGED_SUFFIX = ".staged.patch"
UNSTAGED_SUFFIX = ".unstaged.patch"
UNTRACKED_ARCHIVE_SUFFIX = ".untracked.tgz"
UNTRACKED_LIST_SUFFIX = ".untracked.list"
_PATCH_SUFFIXES = (STAGED_SUFFIX, UNSTAGED_SUFFIX)
_ARCHIVE_SUFFIXES = (UNTRACKED_ARCHIVE_SUFFIX, UNTRACKED_LIST_SUFFIX)
# Every artifact kind this verify knows how to check; any other kind is named.
_ARTIFACT_SUFFIXES = (BUNDLE_SUFFIX, *_PATCH_SUFFIXES, *_ARCHIVE_SUFFIXES)
# Optional locks let a read-only command (status, diff) write the index's cached
# stat data back; a salvage and its verify must leave the worktree's index
# byte-identical, so every git call takes no optional lock.
_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}
_DETACHED = "(detached)"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_STATUS = ["status", "--porcelain=v2", "-z", "--untracked-files=all",
           "--ignored=matching", "--ignore-submodules=none"]
_PATH_FIELD = {"1": 8, "2": 9, "u": 10, "?": 1, "!": 1}  # spaces before the path
_HEADER_LIMIT = 1 << 20  # a bundle header may never end; bound the read
# Directory names whose IGNORED content is throwaway (WT-2a1b, OI-2417): caches
# a worker's tree fills on its own and nobody writes by hand. A status entry on
# this list is the only entry a removal may ignore, and only as a DIRECTORY
# component: a regular file named ``node_modules`` is not one.
THROWAWAY_IGNORED_DIRS = (
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
)
# Below these roots nothing is throwaway, whatever its name: the evidence a
# removal may not lose lives there. Matched as a path component, not a prefix.
_PROTECTED_IGNORE_ROOTS = frozenset({".vnx-data", ".git"})


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    name: str
    branch: str
    head: str
    unpushed: int
    salvage_dir: Path
    artifacts: list[str] = field(default_factory=list)
    manifest_line: str = ""
    written: bool = False
    uncovered: list[str] = field(default_factory=list)  # paths whose state is unpreserved
    worktree: Path | None = None  # where the tree was measured; verify re-reads it


class _Record(NamedTuple):
    """One archived member: kind (``f``/``l``/``d``), size, sha256 and path."""
    kind: str
    size: int
    digest: str
    path: str


@dataclass(frozen=True)
class _Member:
    """Content the untracked archive carries for one path: a file's bytes, a
    symlink's target string, or nothing for an empty directory."""
    kind: str
    path: str
    data: bytes | None = None

    def record(self) -> _Record:
        """The list record of this member; a directory has no bytes to hash."""
        if self.data is None:
            return _Record(self.kind, 0, "-", self.path)
        return _Record(self.kind, len(self.data),
                       hashlib.sha256(self.data).hexdigest(), self.path)


def _utc_stamp() -> str:
    return time.strftime(_STAMP_FORMAT, time.gmtime())


def _run_git(args: list[str], cwd: Path, what: str, *,
             env: dict[str, str] | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              env={**os.environ, **_GIT_ENV, **(env or {})})
    except OSError as exc:
        raise SalvageError(f"{what}: cannot run git: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout
    detail = (proc.stderr.decode("utf-8", "replace").strip()
              or proc.stdout.decode("utf-8", "replace").strip() or "no output")
    raise SalvageError(
        f"{what}: git {' '.join(args)} failed ({proc.returncode}): {detail}")


def _git_failure(args: list[str], cwd: Path, what: str) -> str | None:
    try:
        _run_git(args, cwd, what)
    except SalvageError as exc:
        return str(exc).splitlines()[0]
    return None


def _status_entries(worktree_path: Path) -> list[tuple[str, str]]:
    """Every status entry as ``(kind, path)``, kind being the porcelain v2 entry
    type character (``1`` tracked, ``2`` rename/copy, ``u`` unmerged, ``?``
    untracked, ``!`` ignored)."""
    fields = iter(_run_git(_STATUS, worktree_path, "read status").split(b"\0"))
    entries: list[tuple[str, str]] = []
    for chunk in fields:
        entry = os.fsdecode(chunk)
        if not entry:
            continue
        spaces = _PATH_FIELD.get(entry[0])
        parts = entry.split(" ", spaces) if spaces else []
        if len(parts) != spaces + 1 or not parts[spaces]:
            raise SalvageError(f"read status: unparseable porcelain v2 entry {entry[:60]!r}")
        entries.append((entry[0], parts[spaces]))
        if entry[0] == "2":
            next(fields, None)  # a rename or copy names its source path too
    return entries


def _directory_components(path: str) -> list[str]:
    """The path's DIRECTORY components; a leaf that is a plain file is not one.

    Git marks a directory entry with a trailing ``/`` in porcelain v2, and only
    then is the last name a directory: ``node_modules/`` has one directory
    component, ``node_modules`` (a regular file that merely shares the name)
    has none.
    """
    # ``pkg/x`` -> ["pkg"] and ``pkg/`` -> ["pkg"] (its empty last field is git's
    # directory marker, not a name); ``x`` -> [] (a leaf without the marker is a
    # regular file, so no component of it is a directory).
    return path.split("/")[:-1]


def _is_throwaway_ignored(kind: str, path: str) -> bool:
    """Whether one status entry is a throwaway cache this salvage may ignore.

    Only an IGNORED entry qualifies, and only when a DIRECTORY component of its
    path is a name from :data:`THROWAWAY_IGNORED_DIRS` that is not itself below
    a protected root: ``pkg/__pycache__/x.pyc`` is throwaway, the same path
    below ``.vnx-data/`` or ``.git/`` is not. A regular file that only shares
    an allowlisted directory's name — ``node_modules`` the file, not the
    directory, which git prints without a trailing ``/`` — is never throwaway:
    nothing bundles an ignored file, so excusing it would approve losing it. A
    tracked, untracked or unmerged entry never qualifies, whatever its path
    (WT-2a1b, OI-2417).
    """
    if kind != "!":
        return False
    directories = _directory_components(path)
    for index, component in enumerate(directories):
        if component not in THROWAWAY_IGNORED_DIRS:
            continue
        if _PROTECTED_IGNORE_ROOTS.intersection(directories[:index]):
            continue  # a throwaway name inside evidence is not throwaway itself
        return True
    return False


def _is_patch_covered(kind: str, path: str, populated: set[str]) -> bool:
    """Whether one status entry is a tracked change a patch carries.

    Only a tracked (``1``) or rename/copy (``2``) entry qualifies: its staged
    and/or unstaged change is what ``.staged.patch`` and ``.unstaged.patch``
    reproduce. A gitlink still named as a populated submodule is not covered
    whatever the patch says — its commits ride in the submodule's own object
    store, which no superproject patch can carry.
    """
    return kind in ("1", "2") and path not in populated


def _is_archive_covered(kind: str, path: str) -> bool:
    """Whether one status entry is work the untracked archive carries: an
    untracked (``?``) or non-throwaway ignored (``!``) entry.

    An entry below ``.vnx-data/`` or ``.git/`` never qualifies, whatever its
    kind: that is evidence, not work to pack, and removal must keep blocking.
    """
    if kind not in ("?", "!") or _is_throwaway_ignored(kind, path):
        return False
    return not _PROTECTED_IGNORE_ROOTS.intersection(_directory_components(path))


def _is_internal_link(kind: str, path: str, tree: Path, repo_root: Path) -> bool:
    """Whether one entry is an untracked convenience symlink into the checkout —
    ``.venv -> <checkout>/.venv`` holds no work, so it is covered without being
    carried. An ignored link is archived like any other ignored content."""
    if kind != "?" or path.endswith("/"):
        return False
    from worktree_inventory import is_internal_symlink  # the one shared rule
    return is_internal_symlink(tree / path, repo_root)


def _member_of(entry: Path, rel: str) -> _Member:
    """One member for a path git names as a file: a symlink by the bytes of its
    target, a regular file by its bytes. A device, socket or fifo raises."""
    try:
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            return _Member("l", rel, os.fsencode(os.readlink(entry)))
        if stat.S_ISREG(mode):
            return _Member("f", rel, entry.read_bytes())
    except OSError as exc:
        raise SalvageError(f"read {rel}: {exc}") from exc
    raise SalvageError(f"read {rel}: not a regular file or symlink")


def _walk_members(base: Path, rel: str, out: list[_Member]) -> None:
    """Every regular file and symlink below ``base``, appended to ``out``.

    A symlink is archived as the link it is, never followed. An empty directory
    is a ``d`` member of its own — the one member no file implies, so without it
    an empty nested repository would vanish silently. An unreadable entry raises.
    """
    try:
        names = sorted(os.listdir(base))
    except OSError as exc:
        raise SalvageError(f"read {rel}: {exc}") from exc
    if not names:
        if rel:
            out.append(_Member("d", rel))
        return
    for name in names:
        child, child_rel = base / name, f"{rel}/{name}"
        try:
            mode = os.lstat(child).st_mode
        except OSError as exc:
            raise SalvageError(f"read {child_rel}: {exc}") from exc
        if stat.S_ISDIR(mode):
            _walk_members(child, child_rel, out)
        else:
            out.append(_member_of(child, child_rel))


def _archive_members(entries: list[tuple[str, str]], tree: Path,
                     repo_root: Path) -> list[_Member]:
    """Every member the untracked archive carries for ``entries``, sorted by path.

    A covered directory entry — git's ``path/`` record, how a nested repository
    is reported — is walked, so the files inside it are archived, not its name.
    """
    members: list[_Member] = []
    for kind, path in entries:
        if not _is_archive_covered(kind, path) or _is_internal_link(
                kind, path, tree, repo_root):
            continue
        rel = path[:-1] if path.endswith("/") else path
        if not rel or os.path.isabs(rel):
            raise SalvageError(f"read status: {path!r} is no relative path inside the tree")
        if path.endswith("/"):
            _walk_members(tree / rel, rel, members)
        else:
            members.append(_member_of(tree / rel, rel))
    members.sort(key=lambda member: member.path)
    if len({member.path for member in members}) != len(members):
        raise SalvageError("read status: two entries name one archived path")
    return members


def _uncovered_paths(entries: list[tuple[str, str]], populated: set[str], *,
                     tree: Path, repo_root: Path) -> list[str]:
    """The status entries this salvage does not cover; an ignored throwaway cache
    directory, a tracked change carried by a patch and work the untracked archive
    carries are excused, and every other entry is named."""
    return [path for kind, path in entries
            if not (_is_throwaway_ignored(kind, path)
                    or _is_patch_covered(kind, path, populated)
                    or _is_archive_covered(kind, path))]


def _tar_bytes(members: list[_Member]) -> bytes:
    """The members as one gzip'd tar named by their relative paths. Owner, mode
    and mtime are fixed; content, kind and link target are the evidence."""
    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for member in members:
                info = tarfile.TarInfo(member.path)
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                data = member.data or b""
                if member.kind == "d":
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                elif member.kind == "l":
                    info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, os.fsdecode(data)
                    tar.addfile(info)
                elif member.kind == "f":
                    info.type, info.mode, info.size = tarfile.REGTYPE, 0o644, len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:  # a member kind this module never builds is not written
                    raise SalvageError(f"build the untracked archive: no member kind {member.kind!r}")
    except (OSError, tarfile.TarError) as exc:
        raise SalvageError(f"build the untracked archive: {exc}") from exc
    return buffer.getvalue()


def _list_bytes(members: list[_Member]) -> bytes:
    """The list artifact: one NUL-terminated record per member, in archive order."""
    return b"".join(
        (f"{record.kind} {record.size} {record.digest} {record.path}"
         .encode("utf-8", "surrogateescape") + b"\0")
        for record in (member.record() for member in members))


def _list_records(path: Path) -> dict[str, _Record]:
    """The records of one ``.untracked.list`` artifact, keyed by path. A record
    that does not parse, an empty one and a path listed twice are each a
    SalvageError: an unreadable list proves nothing about its archive."""
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise SalvageError(f"cannot read the list ({exc})") from exc
    records: dict[str, _Record] = {}
    chunks = body.split(b"\0")
    for index, chunk in enumerate(chunks):
        if not chunk:
            if index < len(chunks) - 1:  # only the last NUL closes the final record
                raise SalvageError("the list holds an empty record")
            continue
        fields = os.fsdecode(chunk).split(" ", 3)
        if len(fields) != 4 or not fields[3]:
            raise SalvageError(f"unparseable record {chunk[:60]!r}")
        kind, size, digest, rel = fields
        if (kind not in ("f", "l", "d") or not size.isdigit()
                or (digest != "-" if kind == "d" else not _SHA_RE.fullmatch(digest))):
            raise SalvageError(f"unparseable record {chunk[:60]!r}")
        if rel in records:
            raise SalvageError(f"path {rel} is listed twice")
        records[rel] = _Record(kind, int(size), digest, rel)
    return records


def _archive_record(tar: tarfile.TarFile, info: tarfile.TarInfo) -> _Record:
    """One archive member as a record, hashed from the archive's own bytes."""
    if info.isdir():
        return _Record("d", 0, "-", info.name)
    if info.issym():
        target = os.fsencode(info.linkname)
        return _Record("l", len(target), hashlib.sha256(target).hexdigest(), info.name)
    handle = tar.extractfile(info) if info.isfile() else None
    data = handle.read() if handle is not None else None
    if data is None:
        raise SalvageError(f"member {info.name} is of no kind this list records")
    return _Record("f", len(data), hashlib.sha256(data).hexdigest(), info.name)


def _archive_records(path: Path) -> dict[str, _Record]:
    """The members of one ``.untracked.tgz``, keyed by path. An archive that
    will not open, or a member archived twice, is a SalvageError, never a pass."""
    records: dict[str, _Record] = {}
    try:
        with tarfile.open(path, "r:gz") as tar:
            for info in tar.getmembers():
                if info.name in records:
                    raise SalvageError(f"member {info.name} is archived twice")
                records[info.name] = _archive_record(tar, info)
    except (OSError, tarfile.TarError) as exc:
        raise SalvageError(f"cannot read the archive ({exc})") from exc
    return records


def _archive_artifacts(entries: list[tuple[str, str]], tree: Path, repo_root: Path,
                       name: str) -> list[tuple[str, bytes]]:
    """The untracked archive and its list as ``(artifact, body)`` pairs, or
    nothing when the tree has no member to carry (a clean tree keeps salvaging to
    its MANIFEST line alone). Both join the same run, so no list rides alone."""
    members = _archive_members(entries, tree, repo_root)
    if not members:
        return []
    return [(f"{name}{UNTRACKED_ARCHIVE_SUFFIX}", _tar_bytes(members)),
            (f"{name}{UNTRACKED_LIST_SUFFIX}", _list_bytes(members))]


def _gitlinks(worktree_path: Path) -> list[str]:
    """Every gitlink path (mode ``160000``) of the tree's index. Read-only."""
    raw = _run_git(["ls-files", "--stage", "-z"], worktree_path, "read gitlinks")
    gitlinks: list[str] = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        entry = os.fsdecode(chunk)
        try:
            meta, path = entry.split("\t", 1)
            mode = meta.split(" ", 1)[0]
        except ValueError:
            raise SalvageError(f"read gitlinks: unparseable index entry {entry[:60]!r}")
        if mode != "160000":
            continue
        if not path or os.path.isabs(path):
            raise SalvageError(f"read gitlinks: gitlink names no path {entry[:60]!r}")
        gitlinks.append(path)
    return gitlinks


def _populated_submodules(worktree_path: Path) -> list[str]:
    """Every gitlink of the tree's index whose directory holds a checkout (a
    ``.git`` inside it). A populated submodule keeps its commits in its own
    object store, which no superproject bundle can carry, so those commits cannot
    be proved present: dirty or clean, it is named. A gitlink whose checkout was
    never made is not named — nothing local to lose. Read-only: index plus stat."""
    return [path for path in _gitlinks(worktree_path)
            if os.path.lexists(os.path.join(worktree_path, path, ".git"))]


def _patch_bodies(worktree_path: Path, head: str, entries: list[tuple[str, str]],
                  populated: list[str]) -> tuple[bytes, bytes]:
    """The tree's staged and unstaged diffs of tracked files, as patch bytes.

    Two paths are left out, because no patch reproduces them: an unmerged path
    (its conflict lives in the index stages, not in a diff) and a populated
    submodule (its dirty state is a checkout in another object store, and a
    patch of its gitlink would claim a state the tree does not hold).
    """
    excluded = {path for kind, path in entries if kind == "u"} | set(populated)
    specs = ["--", "."] + [f":(exclude,literal){path}" for path in sorted(excluded)]
    flags = ["--binary", "--full-index"]
    return (
        _run_git(["diff", "--cached", *flags, head, *specs], worktree_path,
                 "read staged changes"),
        _run_git(["diff", *flags, *specs], worktree_path, "read unstaged changes"),
    )


def _claim_dir(dest_dir: Path, base: str, *, create: bool) -> Path:
    for suffix in itertools.count(1):
        candidate = dest_dir / (base if suffix == 1 else f"{base}-{suffix}")
        if not create:
            if not os.path.lexists(candidate):
                return candidate
            continue
        try:
            os.mkdir(candidate)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise SalvageError(f"create {candidate}: {exc}") from exc


def _discard_scratch(scratch: Path) -> None:
    """Remove the staging directory of one ``_write_bundle`` call.

    A leftover would sit in the destination as state no MANIFEST line explains,
    so a failed removal is a SalvageError and never silence.
    """
    try:
        shutil.rmtree(scratch)
    except OSError as exc:
        raise SalvageError(f"remove scratch {scratch.name}: {exc}") from exc


def _write_bundle(dir_path: Path, artifact: str, worktree_path: Path, remote: str) -> None:
    """Publish the bundle under ``dir_path / artifact`` by hard link.

    The staging directory is named by ``tempfile.mkdtemp``, never by the
    destination (OI-1486): a scratch path derived from the destination alone is
    one path for every writer of that artifact, so two writers stage into the
    same file, overwrite each other's bytes, and the first to publish lands the
    other's content under its own name. Each call owns the directory it stages
    in and removes it when it is done, whether or not the bundle was written.
    """
    try:
        scratch = Path(tempfile.mkdtemp(dir=dir_path, prefix=f".{artifact}."))
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    try:
        staged = scratch / artifact
        _run_git(["bundle", "create", str(staged), "HEAD", "--not", f"--remotes={remote}"],
                 worktree_path, "create bundle")
        os.link(staged, dir_path / artifact)
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    finally:
        _discard_scratch(scratch)


def _write_bytes(dir_path: Path, artifact: str, body: bytes) -> None:
    """Publish ``body`` under ``dir_path / artifact`` by hard link.

    The same publish rule as the bundle: staged inside a directory
    ``tempfile.mkdtemp`` names for this call (OI-1486), linked into place so an
    existing artifact is never overwritten, and the staging directory removed
    whether or not the write succeeded.
    """
    try:
        scratch = Path(tempfile.mkdtemp(dir=dir_path, prefix=f".{artifact}."))
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    try:
        staged = scratch / artifact
        staged.write_bytes(body)
        os.link(staged, dir_path / artifact)
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    finally:
        _discard_scratch(scratch)


def _append_manifest(dest_dir: Path, line: str) -> None:
    try:
        with open(dest_dir / MANIFEST_NAME, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(line + "\n")
            os.fsync(fh.fileno())
    except OSError as exc:
        raise SalvageError(f"append {MANIFEST_NAME}: {exc}") from exc


def salvage(worktree_path: Path | str, dest_dir: Path | str, *, repo_root: Path | str,
            base_ref: str = "origin/main", dry_run: bool = True) -> SalvageResult:
    worktree_path, dest_dir = Path(worktree_path), Path(dest_dir)
    repo_root = Path(repo_root)
    name, remote = worktree_path.name, base_ref.split("/", 1)[0]
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")
    # A salvage measured against another repository says nothing about this tree.
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    if _run_git(common, repo_root, "resolve repo_root") != _run_git(
            common, worktree_path, "resolve worktree"):
        raise SalvageError(f"{worktree_path} is not a worktree of {repo_root}")
    out = _run_git(["rev-parse", "HEAD", "--abbrev-ref", "HEAD"],
                   worktree_path, "resolve HEAD").decode().splitlines()
    if len(out) != 2 or not _SHA_RE.fullmatch(out[0].strip()):
        raise SalvageError(f"resolve HEAD: git returned no branch and full sha ({out})")
    head, branch = out[0].strip(), _DETACHED if out[1].strip() == "HEAD" else out[1].strip()
    raw = _run_git(["rev-list", "--count", head, "--not", f"--remotes={remote}"],
                   worktree_path, "count unpushed commits").decode().strip()
    if not raw.isdigit():  # a missing count is no zero read as clean
        raise SalvageError(f"count unpushed commits: git returned {raw!r}, not a count")
    # Read the state first: a dest_dir inside the worktree must not show up in it.
    entries, unpushed = _status_entries(worktree_path), int(raw)
    # A populated submodule's commits ride in no bundle this salvage writes; record
    # it uncovered while the tree is still readable, before anything is written.
    populated = _populated_submodules(worktree_path)
    uncovered = _uncovered_paths(entries, set(populated),
                                 tree=worktree_path, repo_root=repo_root)
    for path in populated:
        if path not in uncovered:
            uncovered.append(path)
    staged, unstaged = _patch_bodies(worktree_path, head, entries, populated)
    artifacts = [f"{name}{BUNDLE_SUFFIX}"] if unpushed > 0 else []
    patches = [(f"{name}{STAGED_SUFFIX}", staged), (f"{name}{UNSTAGED_SUFFIX}", unstaged)]
    artifacts.extend(artifact for artifact, body in patches if body)
    archive = _archive_artifacts(entries, worktree_path, repo_root, name)
    artifacts.extend(artifact for artifact, body in archive)
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    salvage_dir = _claim_dir(dest_dir, f"{name}-{_utc_stamp()}-{head[:8]}", create=not dry_run)
    relative = [f"{salvage_dir.name}/{artifact}" for artifact in artifacts]
    result = SalvageResult(
        name=name, branch=branch, head=head, unpushed=unpushed, salvage_dir=salvage_dir,
        artifacts=relative, uncovered=uncovered, worktree=worktree_path,
        manifest_line=(f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
                       + "".join(f"{artifact} " for artifact in relative)))
    if dry_run:
        return result
    for artifact in artifacts:
        if artifact.endswith(BUNDLE_SUFFIX):
            _write_bundle(salvage_dir, artifact, worktree_path, remote)
    for artifact, body in [*patches, *archive]:
        if artifact in artifacts:
            _write_bytes(salvage_dir, artifact, body)
    _append_manifest(dest_dir, result.manifest_line)
    result.written = True
    return result


def _bundle_prerequisites(lines: list[bytes]) -> tuple[list[str], str | None]:
    prerequisites: list[str] = []
    for raw in lines[1:]:
        if not raw:
            return prerequisites, None
        if raw.startswith(b"-"):
            sha = raw.decode("utf-8", "replace")[1:].split(" ", 1)[0]
            if not _SHA_RE.fullmatch(sha):
                return [], f"unreadable bundle prerequisite {raw[:60]!r}"
            prerequisites.append(sha)
    return [], "bundle header is not terminated inside the file"


def _bundle_problems(path: Path, head: str, cwd: Path) -> list[str]:
    bundle = path.resolve()  # the commands below run in another directory
    failure = _git_failure(["bundle", "verify", str(bundle)], cwd, "verify bundle")
    if failure:
        return [failure]
    try:
        lines = bundle.read_bytes()[:_HEADER_LIMIT].split(b"\n")
    except OSError as exc:
        return [f"cannot read the bundle header ({exc})"]
    if not lines[0].startswith(b"# v"):
        return [f"not a bundle header ({lines[0][:40]!r})"]
    prerequisites, problem = _bundle_prerequisites(lines)
    if problem:
        return [problem]
    with tempfile.TemporaryDirectory(prefix="salvage-unpack.") as name:
        scratch, source = Path(name), str(cwd.resolve())
        failure = _git_failure(["init", "-q", "--bare", str(scratch)], scratch, "init")
        if not failure and prerequisites:
            # -c does NOT reach the upload-pack git spawns for a local path
            # (measured), so allowAnySHA1InWant rides on it via --upload-pack.
            failure = _git_failure(
                ["fetch", "--quiet", "--no-tags",
                 "--upload-pack=git -c uploadpack.allowAnySHA1InWant=true upload-pack",
                 source, *prerequisites], scratch, "fetch prerequisites")
        # Walk the pack (minus prerequisites): an omitted object fails here.
        walk = ["rev-list", "--objects", head] + (["--not", *prerequisites] if prerequisites else [])
        failure = (failure
                   or _git_failure(["bundle", "unbundle", str(bundle)], scratch, "unbundle")
                   or _git_failure(walk, scratch, "walk"))
        return [f"bundle_unpack_failed: {failure}"] if failure else []


def _resolved_inside(path: Path, tree: Path) -> tuple[bool, str | None]:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        return False, f"cannot resolve its location ({exc})"
    return resolved == tree or tree in resolved.parents, None


def _missing_patch_problems(result: SalvageResult, staged: bytes,
                            unstaged: bytes) -> list[str]:
    """A staged or unstaged change in the tree needs its patch listed: verify
    proves a patch by applying it, so an unwritten one leaves the change
    unchecked and the tree must not clear."""
    problems = []
    for body, suffix, label in ((staged, STAGED_SUFFIX, "staged"),
                                (unstaged, UNSTAGED_SUFFIX, "unstaged")):
        if body and not any(a.endswith(suffix) for a in result.artifacts):
            problems.append(f"tree {result.name}: {label} changes but no "
                            f"{result.name}{suffix} artifact is listed")
    return problems


def _tree_problems(tree: Path, result: SalvageResult, repo_root: Path) -> list[str]:
    """The tree's own state, read fail-closed: no readable HEAD, a HEAD that moved
    past the salvaged commit, any state this salvage does not cover, or a patch
    the state needs and the result does not list."""
    try:
        head = _run_git(["rev-parse", "HEAD"], tree, "resolve HEAD").decode().strip()
    except SalvageError as exc:
        return [f"tree {result.name}: state unreadable: {exc}"]
    if not _SHA_RE.fullmatch(head):
        return [f"tree {result.name}: state unreadable: HEAD is {head!r}, not a commit"]
    problems = []
    if head != result.head:
        problems.append(
            f"tree {result.name}: HEAD moved to {head} after the salvage of {result.head}")
    try:
        entries = _status_entries(tree)
        populated = _populated_submodules(tree)
    except SalvageError as exc:
        return problems + [f"tree {result.name}: state unreadable: {exc}"]
    uncovered = _uncovered_paths(entries, set(populated), tree=tree, repo_root=repo_root)
    if uncovered:
        rest = f" (+{len(uncovered) - 1} more)" if len(uncovered) > 1 else ""
        problems.append(f"tree has state this salvage does not cover: {uncovered[0]}{rest}")
    # Dirty or clean, a populated submodule holds commits no bundle carried. One
    # already named above as an uncovered status entry needs no second line.
    problems.extend(
        f"tree has state this salvage does not cover: {path}"
        for path in populated if path not in uncovered)
    if head == result.head:  # a moved HEAD is already a problem of its own
        try:
            problems.extend(_missing_patch_problems(result, *_patch_bodies(
                tree, head, entries, populated)))
        except SalvageError as exc:
            return problems + [f"tree {result.name}: state unreadable: {exc}"]
    return problems


def _diff_paths(cwd: Path, args: list[str], what: str) -> list[str]:
    """Every path ``git diff --name-status -z`` names, both sides of a rename or
    copy. An entry with a status but no path is unreadable, never skipped."""
    fields = iter(_run_git(args, cwd, what).split(b"\0"))
    paths: list[str] = []
    for chunk in fields:
        status = os.fsdecode(chunk)
        if not status:
            continue
        for _ in range(2 if status[0] in "RC" else 1):
            path = os.fsdecode(next(fields, b""))
            if not path:
                raise SalvageError(f"{what}: unparseable diff entry {status[:60]!r}")
            paths.append(path)
    return paths


def _same_file(one: Path, other: Path) -> bool:
    """Whether two paths hold the same kind of entry with the same bytes — and,
    for a regular file, the same executable bit. Anything unreadable differs."""
    one_here, other_here = os.path.lexists(one), os.path.lexists(other)
    if not (one_here and other_here):
        return one_here == other_here  # a file one side deleted must be gone on both
    try:
        one_stat, other_stat = os.lstat(one), os.lstat(other)
        if stat.S_ISLNK(one_stat.st_mode) or stat.S_ISLNK(other_stat.st_mode):
            return (stat.S_ISLNK(one_stat.st_mode) and stat.S_ISLNK(other_stat.st_mode)
                    and os.readlink(one) == os.readlink(other))
        if not (stat.S_ISREG(one_stat.st_mode) and stat.S_ISREG(other_stat.st_mode)):
            return False
        if (one_stat.st_mode ^ other_stat.st_mode) & 0o111:
            return False
        return one.read_bytes() == other.read_bytes()
    except OSError:
        return False


def _index_tree(tree: Path) -> str:
    """The tree hash of the worktree's own index, read through a copy of it in a
    temporary ``GIT_INDEX_FILE``: writing this tree's index is never allowed, so
    the comparison cannot disturb the tree it measures."""
    try:
        raw = _run_git(["rev-parse", "--path-format=absolute", "--git-path", "index"],
                       tree, "locate the index").decode().strip()
        with tempfile.TemporaryDirectory(prefix="salvage-index.") as name:
            copy = Path(name) / "index"
            shutil.copyfile(raw, copy)
            return _run_git(["write-tree"], tree, "write the index tree",
                            env={"GIT_INDEX_FILE": str(copy)}).decode().strip()
    except OSError as exc:
        raise SalvageError(f"read the index tree: {exc}") from exc


def _staged_apply(scratch: Path, patch: Path, index_tree: str, artifact: str) -> list[str]:
    """Apply the staged patch to a scratch index; it must reproduce the tree's
    own index tree, which is the only proof the staged change is preserved."""
    failure = _git_failure(["apply", "--index", str(patch.resolve())], scratch,
                           "apply staged patch")
    if failure:
        return [f"{artifact}: does not apply: {failure}"]
    try:
        applied = _run_git(["write-tree"], scratch, "write the applied tree").decode().strip()
    except SalvageError as exc:
        return [f"{artifact}: {exc}"]
    if applied != index_tree:
        return [f"{artifact}: applies to {applied}, not the tree's index {index_tree}"]
    return []


def _unstaged_apply(scratch: Path, patch: Path, tree: Path, artifact: str) -> list[str]:
    """Apply the unstaged patch to the scratch worktree; every file the apply
    changes — and every file the worktree itself changed — must match the tree
    byte for byte (types and exec bit included).

    The tree's own changed paths are read against its index: a tracked change
    the tree made after the salvage, and that the saved patch therefore never
    mentions, is state the patch does not preserve. Measuring only the applied
    diff would leave it unverified."""
    failure = _git_failure(["apply", str(patch.resolve())], scratch, "apply unstaged patch")
    if failure:
        return [f"{artifact}: does not apply: {failure}"]
    try:
        applied = _diff_paths(scratch, ["diff", "--name-status", "-z"],
                              "read the applied diff")
        if not applied:  # an apply that moves no file proves nothing
            return [f"{artifact}: applies but changes no file"]
        live = _diff_paths(tree, ["diff", "--name-status", "-z"],
                           "read the worktree diff")
    except SalvageError as exc:  # an unreadable diff is a problem, never a pass
        return [f"{artifact}: {exc}"]
    changed = list(dict.fromkeys(applied + live))  # both sides of a rename, in order
    return [f"{artifact}: {path} differs from the worktree"
            for path in changed if not _same_file(scratch / path, tree / path)]


def _apply_problems(result: SalvageResult, dest: Path, tree: Path,
                    patches: list[str]) -> list[str]:
    """Prove the written patches by applying them to a scratch checkout of head.

    The scratch repository borrows no object store of the checkout: a fresh
    ``git init`` is given the salvaged head by sha. The staged patch is applied
    to its index and must reproduce the tree's own index tree; the unstaged
    patch is then applied to its worktree and must reproduce every changed
    file's bytes. Only a positive apply with an exact match clears a patch.
    ``patches`` holds the artifacts that already passed their existence checks.
    """
    staged = [a for a in patches if a.endswith(STAGED_SUFFIX)]
    unstaged = [a for a in patches if a.endswith(UNSTAGED_SUFFIX)]
    if not staged and not unstaged:
        return []
    problems = [f"{extra}: a second {kind} patch is listed"
                for kind, listed in (("staged", staged), ("unstaged", unstaged))
                for extra in listed[1:]]
    try:
        index_tree = _index_tree(tree) if staged else ""
        with tempfile.TemporaryDirectory(prefix="salvage-apply.") as name:
            scratch = Path(name)
            failure = _git_failure(["init", "-q", str(scratch)], scratch, "init scratch")
            failure = failure or _git_failure(
                ["fetch", "--quiet", "--no-tags", str(tree.resolve()), result.head],
                scratch, "fetch head into scratch")
            failure = failure or _git_failure(["checkout", "-q", "-f", "FETCH_HEAD"],
                                              scratch, "check out head in scratch")
            if failure:
                return problems + [f"apply check: {failure}"]
            if staged:
                problems.extend(_staged_apply(scratch, dest / staged[0], index_tree, staged[0]))
            if unstaged:
                problems.extend(_unstaged_apply(scratch, dest / unstaged[0], tree, unstaged[0]))
    except (SalvageError, OSError) as exc:
        problems.append(f"apply check: {exc}")
    return problems


def _archive_vs_list(packed: dict[str, _Record], records: dict[str, _Record],
                     artifact: str) -> list[str]:
    """Every member one of the two lacks, and every one they disagree on: an
    archive that holds less than its list restores a tree nobody described."""
    problems = [f"{artifact}: member {path} is in the list but not in the archive"
                for path in sorted(records) if path not in packed]
    problems.extend(f"{artifact}: member {path} is in the archive but not in the list"
                    for path in sorted(packed) if path not in records)
    problems.extend(f"{artifact}: member {path} differs from the list"
                    for path in sorted(packed)
                    if path in records and packed[path] != records[path])
    return problems


def _list_vs_tree(records: dict[str, _Record], fresh: dict[str, _Record],
                  name: str) -> list[str]:
    """Every listed path that left or changed in the tree since the salvage, and
    every member the tree gained that the list never carried.

    A file edited, deleted or added after the salvage is state the archive does
    not hold, so it blocks removal as a moved HEAD does. Comparing the whole
    member set — not only the listed paths — keeps a NEW untracked file from
    riding along on the coverage of the ones already archived.
    """
    problems = []
    for path, record in sorted(records.items()):
        current = fresh.get(path)
        if current is None:
            problems.append(f"tree {name}: {path} is not in the tree as the untracked "
                            "list records it")
        elif current != record:
            problems.append(f"tree {name}: {path} differs from the untracked list")
    problems.extend(f"tree {name}: {path} is in the tree but not in the untracked list"
                    for path in sorted(fresh) if path not in records)
    return problems


def _archive_problems(result: SalvageResult, dest: Path, tree: Path, repo_root: Path,
                      archives: list[str], lists: list[str]) -> list[str]:
    """Prove the untracked archive: one archive and one list, the list parsing,
    the archive opening, the two holding the same members with the same kind,
    size and sha256, and every listed path still in the tree as recorded.

    ``archives`` and ``lists`` passed their existence checks. The tree is measured
    whether or not an archive is listed: untracked content with no archive to
    carry it leaves the work unchecked.
    """
    problems = [f"{extra}: a second {label} is listed"
                for label, listed in (("untracked archive", archives),
                                      ("untracked list", lists))
                for extra in listed[1:]]
    try:
        fresh = {member.path: member.record() for member in _archive_members(
            _status_entries(tree), tree, repo_root)}
    except SalvageError as exc:
        return problems + [f"tree {result.name}: state unreadable: {exc}"]
    if not archives:
        if not lists and not fresh:
            return problems
        return problems + [f"tree {result.name}: untracked content is not archived "
                           f"(no {result.name}{UNTRACKED_ARCHIVE_SUFFIX} is listed)"]
    if not lists:
        return problems + [f"{archives[0]}: no {result.name}{UNTRACKED_LIST_SUFFIX} "
                           "is listed for this archive"]
    try:
        records = _list_records(dest / lists[0])
    except SalvageError as exc:
        return problems + [f"{lists[0]}: {exc}"]
    try:
        packed = _archive_records(dest / archives[0])
    except SalvageError as exc:
        return problems + [f"{archives[0]}: {exc}"]
    problems.extend(_archive_vs_list(packed, records, archives[0]))
    problems.extend(_list_vs_tree(records, fresh, result.name))
    return problems


def verify(result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str) -> list[str]:
    """Every problem keeping this salvage from being complete; only ``[]`` lets a removal act."""
    dest, repo, problems = Path(dest_dir), Path(repo_root), []
    tree: Path | None = None
    if result.worktree is not None:
        try:
            tree = Path(result.worktree).resolve()
        except (OSError, RuntimeError) as exc:  # 3.12 calls a symlink loop a RuntimeError
            problems.append(f"cannot resolve the worktree {result.worktree!r} ({exc})")
    if tree is None or not tree.is_dir():
        problems.append(f"tree {result.name}: no directory to re-read its state from")
    else:
        problems.extend(_tree_problems(tree, result, repo))
    if _git_failure(["rev-parse", "--git-dir"], repo, "resolve repo_root") is not None:
        problems.append(f"no usable repo_root: {repo_root!r} is not a git checkout")
    if result.unpushed > 0 and not any(a.endswith(BUNDLE_SUFFIX) for a in result.artifacts):
        problems.append(f"head={result.head} has {result.unpushed} commit(s) off origin "
                        "but no bundle is listed")
    # Removal deletes the whole tree: where the path lands decides, not its spelling.
    dest_inside = False
    if tree is not None:
        dest_inside, why = _resolved_inside(dest, tree)
        if why:
            problems.append(f"dest_dir {dest}: {why}")
        elif dest_inside:
            problems.append(f"dest_dir {dest}: resolves inside the worktree {tree}, "
                            "so removing the tree would delete this salvage")
    patches: list[str] = []  # patch artifacts that passed their existence checks
    archives: list[str] = []  # untracked archives that passed theirs
    lists: list[str] = []  # untracked lists that passed theirs
    for artifact in result.artifacts:
        path = dest / artifact
        if tree is not None and not dest_inside:
            inside, why = _resolved_inside(path, tree)
            if why:
                problems.append(f"{artifact}: {why}")
                continue
            if inside:
                problems.append(f"{artifact}: resolves inside the worktree {tree}, "
                                "so removing the tree would delete this artifact")
                continue
        if not artifact.endswith(_ARTIFACT_SUFFIXES):
            problems.append(f"{artifact}: not an artifact kind this verify can check")
            continue
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError as exc:
            problems.append(f"{artifact}: cannot stat ({exc})")
            continue
        if size is None:
            problems.append(f"{artifact}: missing")
        elif size == 0:
            problems.append(f"{artifact}: empty")
        elif artifact.endswith(BUNDLE_SUFFIX):
            try:
                problems.extend(f"{artifact}: {p}" for p in
                                _bundle_problems(path, result.head, repo))
            except SalvageError as exc:  # a git failure is a problem, never a pass
                problems.append(f"{artifact}: {exc}")
        elif artifact.endswith(_PATCH_SUFFIXES):
            patches.append(artifact)  # proved by applying it, after this loop
        elif artifact.endswith(UNTRACKED_ARCHIVE_SUFFIX):
            archives.append(artifact)
        else:  # UNTRACKED_LIST_SUFFIX
            lists.append(artifact)
    if tree is not None and tree.is_dir():
        problems.extend(_apply_problems(result, dest, tree, patches))
        problems.extend(_archive_problems(result, dest, tree, repo, archives, lists))
    return problems
