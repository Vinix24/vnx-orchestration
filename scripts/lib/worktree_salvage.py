"""worktree_salvage.py — preserve a worktree's unpushed commits and tracked
changes (WT-2a1a/2a1b/2a2).

:func:`salvage` preserves three kinds of tree state: the commits no ``origin``
ref reaches (``<name>.bundle``), the staged changes to tracked files
(``<name>.staged.patch``, ``git diff --cached --binary --full-index <head>``)
and the unstaged changes to tracked files (``<name>.unstaged.patch``,
``git diff --binary --full-index``). :func:`verify` returns ``[]`` only when
the tree holds nothing else — ``git status --porcelain=v2 -z
--untracked-files=all --ignored=matching --ignore-submodules=none`` names no
entry this salvage does not cover (a tracked entry with a staged or unstaged
change is carried by a patch; untracked and unmerged entries are not; an
IGNORED entry is covered only inside a throwaway cache DIRECTORY from
:data:`THROWAWAY_IGNORED_DIRS` and never below ``.vnx-data/`` or ``.git/`` —
WT-2a1b, OI-2417; a regular file that only shares a cache directory's name is
never covered), every gitlink whose checkout exists is named even when it is
clean (a submodule keeps its commits in its own object store, so no
superproject bundle can carry them; nothing here classifies or excuses one —
submodule bundling is OI-2411), a gitlink whose checkout was never made is not
named, HEAD is readable and still the salvaged commit, every bundle is a
non-empty bundle that unpacks to that head in a scratch repository borrowing no
object store of the checkout, and every patch is proved by applying it in such
a checkout: the staged patch must reproduce the tree's own index tree
(``git write-tree``, read through a temporary ``GIT_INDEX_FILE`` so the
worktree's index is never written) and the unstaged patch must reproduce every
changed file's bytes, both for the paths its apply changes and for the paths the
worktree itself changed against its index — a tracked change the tree makes
after the salvage, and that no patch mentions, is a named problem too. A failed
apply or a mismatch is a named problem. An
artifact or ``dest_dir`` inside the tree is refused: removal would delete the
evidence. A real run writes ``dest_dir/<name>-<UTC>-<head[:8]>/*`` and appends
the MANIFEST line.
"""
from __future__ import annotations

import fcntl
import itertools
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
STAGED_SUFFIX = ".staged.patch"
UNSTAGED_SUFFIX = ".unstaged.patch"
_PATCH_SUFFIXES = (STAGED_SUFFIX, UNSTAGED_SUFFIX)
# Every artifact kind this verify knows how to check; any other kind is named.
_ARTIFACT_SUFFIXES = (BUNDLE_SUFFIX, *_PATCH_SUFFIXES)
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


def _uncovered_paths(entries: list[tuple[str, str]], populated: set[str]) -> list[str]:
    """The status entries this salvage does not cover; an ignored throwaway cache
    directory and a tracked change carried by a patch are excused, and every
    other entry is named."""
    return [path for kind, path in entries
            if not (_is_throwaway_ignored(kind, path) or _is_patch_covered(kind, path, populated))]


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
    name, remote = worktree_path.name, base_ref.split("/", 1)[0]
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")
    # A salvage measured against another repository says nothing about this tree.
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    if _run_git(common, Path(repo_root), "resolve repo_root") != _run_git(
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
    uncovered = _uncovered_paths(entries, set(populated))
    for path in populated:
        if path not in uncovered:
            uncovered.append(path)
    staged, unstaged = _patch_bodies(worktree_path, head, entries, populated)
    artifacts = [f"{name}{BUNDLE_SUFFIX}"] if unpushed > 0 else []
    patches = [(f"{name}{STAGED_SUFFIX}", staged), (f"{name}{UNSTAGED_SUFFIX}", unstaged)]
    artifacts.extend(artifact for artifact, body in patches if body)
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
    for artifact, body in patches:
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


def _tree_problems(tree: Path, result: SalvageResult) -> list[str]:
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
    uncovered = _uncovered_paths(entries, set(populated))
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
        problems.extend(_tree_problems(tree, result))
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
        elif not artifact.endswith(BUNDLE_SUFFIX):
            patches.append(artifact)  # proved by applying it, after this loop
        else:
            try:
                problems.extend(f"{artifact}: {p}" for p in
                                _bundle_problems(path, result.head, repo))
            except SalvageError as exc:  # a git failure is a problem, never a pass
                problems.append(f"{artifact}: {exc}")
    if tree is not None and tree.is_dir():
        problems.extend(_apply_problems(result, dest, tree, patches))
    return problems
