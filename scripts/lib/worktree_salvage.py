"""worktree_salvage.py — preserve a worktree's unpushed commits (PR-2a1).

:func:`salvage` preserves exactly one kind of tree state — the commits no
``origin`` ref reaches — and :func:`verify` answers ``[]`` only when every piece
of the tree's state is that kind, positively checked. Any other status entry
(tracked or staged change, submodule, unmerged path, untracked file), a populated
submodule (its commits ride in no bundle), a tree without a readable HEAD, and
every artifact whose evidence is unproven is a named problem: a dirty tree never
verifies clean. An artifact that resolves inside the tree is refused as well —
removal would delete the very evidence the verify rests on, and following
``Path.resolve()`` on both sides means a symlink cannot smuggle it back in.

Each real run writes a directory of its own,
``dest_dir/<name>-<UTC %Y%m%dT%H%M%SZ>-<head[:8]>/``, created with ``os.mkdir``,
publishes each artifact from a temporary name with ``os.link``, and appends one
line to ``dest_dir/MANIFEST.txt``::

    <tree dir name> | <branch or (detached)> | head=<sha> | niet-op-remote=<n> | <artifacts> 

``<name>.bundle`` exists only when head has commits no ``origin/*`` ref reaches.
It is thin, so :func:`verify` fetches the closure of its prerequisites from
``repo_root`` and unpacks it in a scratch repository that borrows no object store
of the checkout: an object the checkout happens to have can never stand in for
one the bundle failed to carry.
"""
from __future__ import annotations

import fcntl
import itertools
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
_DETACHED = "(detached)"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
# The state verify reads: every untracked file and every submodule difference.
_STATUS = ["status", "--porcelain=v2", "-z", "--untracked-files=all",
           "--ignore-submodules=none"]
# Spaces before the path of a porcelain v2 record, per record type.
_PATH_FIELD = {"1": 8, "2": 9, "u": 10, "?": 1, "!": 1}
# A bundle header is one line per prerequisite; this bounds the read of a file
# whose header may never end.
_HEADER_LIMIT = 1 << 20


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    """What :func:`salvage` found, and for a real run wrote."""
    name: str
    branch: str
    head: str
    unpushed: int
    salvage_dir: Path
    artifacts: list[str] = field(default_factory=list)  # paths relative to dest_dir
    manifest_line: str = ""
    written: bool = False
    uncovered: list[str] = field(default_factory=list)  # paths no artifact preserves
    worktree: Path | None = None  # where the tree was measured; verify re-reads it


def _utc_stamp() -> str:
    return time.strftime(_STAMP_FORMAT, time.gmtime())


def _run_git(args: list[str], cwd: Path, what: str) -> bytes:
    """Run one git command; a non-zero exit is a SalvageError with its stderr."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    except OSError as exc:
        raise SalvageError(f"{what}: cannot run git: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    detail = stderr or proc.stdout.decode("utf-8", "replace").strip() or "no output"
    raise SalvageError(f"{what}: git {' '.join(args)} failed ({proc.returncode}): {detail}")


def _git_failure(args: list[str], cwd: Path) -> str | None:
    """The first stderr line of a failing git command — its ``fatal:``/``error:``
    reason, not the multi-line fallout that follows — or None on a zero exit."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    except OSError as exc:
        return f"cannot run git: {exc}"
    if proc.returncode == 0:
        return None
    lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
    if lines:
        return lines[0]
    return proc.stdout.decode("utf-8", "replace").strip() or f"exit status {proc.returncode}"


def _identity(worktree_path: Path, remote: str) -> tuple[str, str, int]:
    """``(head, branch, commits the remote lacks)``, read from git: an unreadable
    tree or a non-numeric count is a SalvageError, never a zero read as clean."""
    out = _run_git(["rev-parse", "HEAD", "--abbrev-ref", "HEAD"], worktree_path,
                   "resolve HEAD").decode().splitlines()
    if len(out) != 2 or not _SHA_RE.fullmatch(out[0].strip()):
        raise SalvageError(f"resolve HEAD: git returned no branch and full sha ({out})")
    head, branch = out[0].strip(), out[1].strip()
    raw = _run_git(["rev-list", "--count", head, "--not", f"--remotes={remote}"],
                   worktree_path, "count unpushed commits").decode().strip()
    if not raw.isdigit():
        raise SalvageError(f"count unpushed commits: git returned {raw!r}, not a count")
    return head, _DETACHED if branch == "HEAD" else branch, int(raw)


def _status_paths(worktree_path: Path) -> list[str]:
    """Every path git status names. An entry this code cannot parse is a
    SalvageError: a status read as empty would be a tree read as clean."""
    raw = _run_git(_STATUS, worktree_path, "read status")
    fields, paths = iter(raw.split(b"\0")), []
    for chunk in fields:
        if not chunk:
            continue
        entry = os.fsdecode(chunk)
        spaces = _PATH_FIELD.get(entry[0])
        parts = entry.split(" ", spaces) if spaces else []
        if not parts or len(parts) != spaces + 1 or not parts[spaces]:
            raise SalvageError(f"read status: unparseable porcelain v2 entry {entry[:60]!r}")
        paths.append(parts[spaces])
        if entry[0] == "2":
            next(fields, None)  # a rename or copy names its source path too
    return paths


def _populated_submodules(worktree_path: Path) -> list[str]:
    """Every gitlink of the tree's index whose directory holds a checkout (a
    ``.git`` inside it). A populated submodule keeps its commits in its own
    object store, which no superproject bundle can carry, so those commits cannot
    be proved present: dirty or clean, it is named. A gitlink whose checkout was
    never made is not named — nothing local to lose. Read-only: index plus stat."""
    raw = _run_git(["ls-files", "--stage", "-z"], worktree_path, "read gitlinks")
    populated: list[str] = []
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
        if os.path.lexists(os.path.join(worktree_path, path, ".git")):
            populated.append(path)
    return populated


def _claim_dir(dest_dir: Path, base: str, *, create: bool) -> Path:
    """The directory of this salvage alone: ``os.mkdir`` never succeeds on a path
    that exists; a dry run only probes, it creates nothing."""
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


def _write_bundle(dir_path: Path, artifact: str, worktree_path: Path, head: str,
                  remote: str) -> None:
    """Fill a temporary name in this salvage's own directory, prove the bundle
    advertises ``head``, publish it with ``os.link``, then drop the temp name."""
    tmp = dir_path / f".{artifact}.tmp"
    try:
        # ``git bundle create`` refuses a bare sha: name the tip HEAD, then prove
        # the bundle advertises the head this salvage measured.
        _run_git(["bundle", "create", str(tmp), "HEAD", "--not", f"--remotes={remote}"],
                 worktree_path, "create bundle")
        heads = _run_git(["bundle", "list-heads", str(tmp)], worktree_path, "list heads")
        if head not in {line.split()[0] for line in heads.decode().splitlines() if line.split()}:
            raise SalvageError(f"create bundle: the bundle does not advertise head {head}")
        os.link(tmp, dir_path / artifact)
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def _append_manifest(dest_dir: Path, line: str) -> None:
    """Append one line under an exclusive lock, so two salvages cannot interleave it."""
    try:
        with open(dest_dir / MANIFEST_NAME, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise SalvageError(f"append {MANIFEST_NAME}: {exc}") from exc


def salvage(
    worktree_path: Path | str, dest_dir: Path | str, *, repo_root: Path | str,
    base_ref: str = "origin/main", dry_run: bool = True,
) -> SalvageResult:
    """Compute the salvage of ``worktree_path`` into ``dest_dir``. ``dry_run`` (the
    default) computes the directory name, artifacts and MANIFEST line and writes
    NOTHING; a real run writes them into a directory of its own plus one line."""
    worktree_path, dest_dir = Path(worktree_path), Path(dest_dir)
    name = worktree_path.name
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")
    remote = base_ref.split("/", 1)[0]
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")
    # A salvage measured against another repository says nothing about this tree.
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    if _run_git(common, Path(repo_root), "resolve repo_root") != _run_git(
            common, worktree_path, "resolve worktree"):
        raise SalvageError(f"{worktree_path} is not a worktree of {repo_root}")
    head, branch, unpushed = _identity(worktree_path, remote)
    # Read the tree's state before creating anything, so a dest_dir inside the
    # worktree cannot become a status entry of the tree it describes.
    uncovered = _status_paths(worktree_path)
    # A populated submodule's commits are in no bundle this salvage writes; record
    # it uncovered while the tree is still readable, before anything is written.
    for path in _populated_submodules(worktree_path):
        if path not in uncovered:
            uncovered.append(path)
    artifacts = [f"{name}{BUNDLE_SUFFIX}"] if unpushed > 0 else []
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    salvage_dir = _claim_dir(dest_dir, f"{name}-{_utc_stamp()}-{head[:8]}", create=not dry_run)
    relative = [f"{salvage_dir.name}/{artifact}" for artifact in artifacts]
    result = SalvageResult(
        name=name, branch=branch, head=head, unpushed=unpushed, salvage_dir=salvage_dir,
        artifacts=relative, uncovered=uncovered, worktree=worktree_path,
        manifest_line=(
            f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
            + "".join(f"{artifact} " for artifact in relative)
        ),
    )
    if dry_run:
        return result
    for artifact in artifacts:
        _write_bundle(salvage_dir, artifact, worktree_path, head, remote)
    _append_manifest(dest_dir, result.manifest_line)
    result.written = True
    return result


def _bundle_prerequisites(path: Path) -> tuple[list[str], str | None]:
    """The commits a thin bundle leaves out, read from its own header (``-<sha>``
    lines). An unreadable or unterminated header is a problem, never an empty
    prerequisite list."""
    try:
        with path.open("rb") as fh:
            data = fh.read(_HEADER_LIMIT)
    except OSError as exc:
        return [], f"cannot read the bundle header ({exc})"
    lines = data.split(b"\n")
    if not lines[0].startswith(b"# v"):
        return [], f"not a bundle header ({lines[0][:40]!r})"
    prerequisites: list[str] = []
    for raw in lines[1:]:
        if not raw:
            return prerequisites, None
        line = raw.decode("utf-8", "replace")
        if not line.startswith("-"):
            continue  # a ref line, or a v3 capability line
        sha = line[1:].split(" ", 1)[0]
        if not _SHA_RE.fullmatch(sha):
            return [], f"unreadable bundle prerequisite {line[:60]!r}"
        prerequisites.append(sha)
    return [], "bundle header is not terminated inside the file"


def _fetch_prerequisites(scratch: Path, repo: Path, prerequisites: list[str]) -> str | None:
    """Give the bare ``scratch`` the closure of ``prerequisites``, fetched from
    ``repo`` by sha, or the reason it could not arrive. ``-c`` does NOT reach the
    ``upload-pack`` git spawns for a local path (measured: with
    ``-c uploadpack.hideRefs=refs/heads/main`` the ref still comes back), so
    ``allowAnySHA1InWant`` rides on that command through ``--upload-pack``; without
    it a prerequisite no ref of ``repo`` advertises cannot be asked for."""
    if not prerequisites:
        return None  # a full bundle starts from an empty repository, nothing fetched
    source = str(repo.resolve())
    failure = _git_failure(
        ["fetch", "--quiet", "--no-tags",
         "--upload-pack=git -c uploadpack.allowAnySHA1InWant=true upload-pack",
         source, *prerequisites],
        scratch,
    )
    if failure:
        return f"cannot fetch the prerequisites from {source}: {failure}"
    for sha in prerequisites:
        failure = _git_failure(["cat-file", "-e", f"{sha}^{{commit}}"], scratch)
        if failure:
            return f"prerequisite {sha} did not arrive from {source}: {failure}"
    return None


def _bundle_problems(path: Path, head: str, cwd: Path) -> list[str]:
    """Every problem that keeps this bundle from restoring ``head``.

    ``git bundle verify`` reads the header and never a packed object, so a bundle
    truncated right after its header still verifies. Only an unpack proves the
    objects: into a temporary bare repository that is given the closure of the
    bundle's prerequisites, fetched from ``cwd`` by sha, and that borrows nothing
    else of ``cwd``'s store."""
    bundle = path.resolve()  # the commands below run in another directory
    failure = _git_failure(["bundle", "verify", str(bundle)], cwd)
    if failure:
        return [failure]
    advertised = _run_git(["bundle", "list-heads", str(bundle)], cwd, "list heads")
    heads = {line.split()[0] for line in advertised.decode().splitlines() if line.split()}
    if head not in heads:
        return [f"bundle does not advertise head {head}"]
    prerequisites, problem = _bundle_prerequisites(bundle)
    if problem:
        return [problem]
    with tempfile.TemporaryDirectory(prefix="salvage-unpack.") as name:
        scratch = Path(name)
        failure = _git_failure(["init", "-q", "--bare", str(scratch)], scratch)
        if failure:
            return [f"bundle_unpack_failed: cannot create the scratch repository: {failure}"]
        failure = _fetch_prerequisites(scratch, cwd, prerequisites) or _git_failure(
            ["bundle", "unbundle", str(bundle)], scratch)
        if failure:
            return [f"bundle_unpack_failed: {failure}"]
        failure = _git_failure(["cat-file", "-e", f"{head}^{{commit}}"], scratch)
        if failure:
            return [f"bundle_unpack_failed: head {head} is no commit: {failure}"]
        # Walk what the pack carries: an object of head's closure the pack omitted
        # fails here rather than in a restore.
        failure = _git_failure(
            ["rev-list", "--objects", head, *(["--not", *prerequisites] if prerequisites else [])],
            scratch,
        )
        return [f"bundle_unpack_failed: {failure}"] if failure else []


def _tree_problems(result: SalvageResult) -> list[str]:
    """The tree's own state, read fail-closed: no directory to re-read, no HEAD, a
    HEAD that moved past the salvaged commit, or any status entry at all."""
    tree = Path(result.worktree) if result.worktree is not None else None
    if tree is None or not tree.is_dir():
        return [f"tree {result.name}: no directory to re-read its state from"]
    try:
        head = _run_git(["rev-parse", "HEAD"], tree, "resolve HEAD").decode().strip()
    except SalvageError as exc:
        return [f"tree {result.name}: no readable HEAD: {exc}"]
    if not _SHA_RE.fullmatch(head):
        return [f"tree {result.name}: HEAD is {head!r}, not a commit"]
    problems = []
    if head != result.head:
        problems.append(
            f"tree {result.name}: HEAD moved to {head} after the salvage of {result.head}")
    try:
        entries = _status_paths(tree)
    except SalvageError as exc:
        return problems + [f"tree {result.name}: status unreadable: {exc}"]
    if entries:
        rest = f" (+{len(entries) - 1} more)" if len(entries) > 1 else ""
        problems.append(f"tree has state this salvage does not cover: {entries[0]}{rest}")
    try:
        gitlinks = _populated_submodules(tree)
    except SalvageError as exc:
        return problems + [f"tree {result.name}: gitlinks unreadable: {exc}"]
    # Dirty or clean, a populated submodule holds commits no bundle carried. One
    # already refused above as a status entry needs no second line.
    problems.extend(
        f"submodule_not_preserved {path}" for path in gitlinks if path not in entries)
    return problems


def verify(
    result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str
) -> list[str]:
    """Every problem that keeps this salvage from being complete; only ``[]`` lets
    a removal act. The tree may hold no state beyond the commits it salvaged (see
    the module docstring), the result must record the bundle its head needs, and
    every artifact must still be evidence this verify checks that removal will
    not delete — an artifact whose resolved location lies inside the worktree is
    refused, symlinks followed on both sides."""
    dest, repo = Path(dest_dir), Path(repo_root)
    problems = _tree_problems(result)
    repo_ok = _git_failure(["rev-parse", "--git-dir"], repo) is None
    if not repo_ok:
        problems.append(f"no usable repo_root: {repo_root!r} is not a git checkout")
    if result.unpushed > 0 and not any(a.endswith(BUNDLE_SUFFIX) for a in result.artifacts):
        problems.append(
            f"head={result.head} has {result.unpushed} commit(s) off origin "
            "but no bundle is listed")
    tree: Path | None = None
    if result.worktree is not None:
        try:
            tree = Path(result.worktree).resolve()
        except OSError as exc:  # cannot place the tree: no artifact can be proven outside
            problems.append(f"cannot resolve the worktree {result.worktree!r} ({exc})")
    for artifact in result.artifacts:
        path = dest / artifact
        try:
            resolved = path.resolve()
        except OSError as exc:
            problems.append(f"{artifact}: cannot resolve its location ({exc})")
            continue
        # Removal deletes the whole tree, so evidence that would be deleted with
        # it proves nothing: where the path lands decides, not how it is spelled.
        if tree is not None and (resolved == tree or tree in resolved.parents):
            problems.append(
                f"{artifact}: resolves inside the worktree {tree}, "
                "so removing the tree would delete this artifact")
            continue
        if not artifact.endswith(BUNDLE_SUFFIX):
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
        elif repo_ok:
            try:
                problems.extend(f"{artifact}: {p}" for p in
                                _bundle_problems(path, result.head, repo))
            except SalvageError as exc:  # a git failure is a problem, never a pass
                problems.append(f"{artifact}: {exc}")
    return problems
