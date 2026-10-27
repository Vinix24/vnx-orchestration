"""worktree_salvage.py — preserve a worktree's commits and tracked work.

Each salvage writes into its own new directory
``dest_dir/<name>-<UTC %Y%m%dT%H%M%SZ>-<head[:8]>/`` (``os.mkdir``: never one that
exists) and appends one line to ``dest_dir/MANIFEST.txt``::

    <tree dir name> | <branch or (detached)> | head=<sha> | niet-op-remote=<n> | <artifact paths> 

``<name>.bundle`` holds the commits the remote in ``base_ref`` lacks; it is thin
(``git fetch <bundle> HEAD`` restores it where that history is present).
``<name>.staged.patch`` is HEAD to index, ``<name>.patch`` index to worktree — two
patches, because a staged change the tree undoes again cancels out in one ``git
diff HEAD``; ``git status``, not an empty diff, decides whether there is tracked
work. Untracked files are PR-2b's: a tree with any never verifies clean.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
STAGED_SUFFIX = ".staged.patch"
PATCH_SUFFIX = ".patch"
_DETACHED = "(detached)"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
# Fixed output whatever the operator's diff config says, so the patch applies.
_DIFF = ["diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
         "--src-prefix=a/", "--dst-prefix=b/"]
# Porcelain v1 codes of a path in a merge conflict: no patch carries a stage.
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    """What :func:`salvage` found (and, for a real run, wrote)."""
    name: str
    branch: str
    head: str
    unpushed: int
    salvage_dir: Path  # written into; computed only, nothing created, on a dry run
    artifacts: list[str] = field(default_factory=list)  # paths relative to dest_dir
    untracked_pending: bool = False
    manifest_line: str = ""
    written: bool = False


def _utc_stamp() -> str:
    return time.strftime(_STAMP_FORMAT, time.gmtime())


def _run_git(args: list[str], cwd: Path, what: str, env: dict[str, str] | None = None) -> bytes:
    """Run one git command; a non-zero exit is a SalvageError with its stderr."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, env=env)
    except OSError as exc:
        raise SalvageError(f"{what}: cannot run git: {exc}") from exc
    if proc.returncode == 0:
        return proc.stdout
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    detail = stderr or proc.stdout.decode("utf-8", "replace").strip() or "no output"
    raise SalvageError(f"{what}: git {' '.join(args)} failed ({proc.returncode}): {detail}")


def _git_problem(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """``(problem, stdout)`` of one git command; None only on a zero exit status."""
    try:
        return None, _run_git(args, cwd, " ".join(args[:2]), env).decode("utf-8", "replace").strip()
    except SalvageError as exc:
        return str(exc), ""


def _identity(worktree_path: Path, remote: str) -> tuple[str, str, int]:
    """``(head, branch, commits the remote lacks)``, read from git: unreadable is an
    error, never a zero that reads as "clean"."""
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


def _tracked_diffs(worktree_path: Path) -> tuple[bytes, bytes]:
    """``(HEAD to index, index to worktree)`` as binary patches; tracked work that
    neither carries, and every merge conflict, is a SalvageError, never empty."""
    raw = _run_git(["status", "--porcelain=v1", "-z"], worktree_path, "read status")
    fields, changes = iter(raw.split(b"\0")), []
    for chunk in fields:
        if not chunk:
            continue
        entry = os.fsdecode(chunk)
        if len(entry) < 4 or entry[2] != " ":
            raise SalvageError(f"read status: unparseable entry {entry!r}")
        if entry[:2] != "??":
            changes.append((entry[:2], entry[3:]))
        if "R" in entry[:2] or "C" in entry[:2]:
            next(fields, None)  # a rename or copy names its source path too
    unmerged = sorted(path for xy, path in changes if xy in _UNMERGED)
    if unmerged:
        raise SalvageError(f"unmerged paths no patch can carry: {', '.join(unmerged)}")
    staged = _run_git([*_DIFF, "--cached", "HEAD"], worktree_path, "diff HEAD to index")
    unstaged = _run_git(_DIFF, worktree_path, "diff index to worktree")
    if changes and not (staged or unstaged):
        raise SalvageError(f"git status reports {len(changes)} change(s), both diffs empty")
    return staged, unstaged


def _claim_dir(dest_dir: Path, base: str, *, create: bool) -> Path:
    """The directory of this salvage alone: ``os.mkdir`` never succeeds on an existing path."""
    suffix = 1
    while True:
        candidate = dest_dir / (base if suffix == 1 else f"{base}-{suffix}")
        if create:
            try:
                os.mkdir(candidate)
                return candidate
            except FileExistsError:
                pass
            except OSError as exc:
                raise SalvageError(f"create {candidate}: {exc}") from exc
        elif not os.path.lexists(candidate):
            return candidate
        suffix += 1


def _write_artifact(dir_path: Path, artifact: str, worktree_path: Path, head: str,
                     remote: str, patches: dict[str, bytes]) -> None:
    """Fill a temporary name in this salvage's own directory, then publish it
    under its final name with ``os.link``, which fails if that file exists."""
    tmp = dir_path / f".{artifact}.tmp"
    try:
        if artifact.endswith(BUNDLE_SUFFIX):
            # ``git bundle create`` refuses a bare sha: name the tip HEAD, then
            # prove the bundle advertises the head we measured.
            _run_git(["bundle", "create", str(tmp), "HEAD", "--not", f"--remotes={remote}"],
                     worktree_path, "create bundle")
            _run_git(["bundle", "verify", str(tmp)], worktree_path, "verify bundle")
            heads = _run_git(["bundle", "list-heads", str(tmp)], worktree_path, "list heads")
            advertised = {line.split()[0] for line in heads.decode().splitlines() if line.strip()}
            if head not in advertised:
                raise SalvageError(f"create bundle: bundle does not advertise head {head}")
        else:
            tmp.write_bytes(patches[artifact])
        os.link(tmp, dir_path / artifact)
    except OSError as exc:
        raise SalvageError(f"write {artifact}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def _append_manifest(dest_dir: Path, line: str) -> None:
    """Append one line under an exclusive lock, so two salvages cannot interleave it."""
    try:
        fd = os.open(str(dest_dir / MANIFEST_NAME), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as exc:
        raise SalvageError(f"open {MANIFEST_NAME}: {exc}") from exc
    try:
        with os.fdopen(fd, "ab") as fh:
            fcntl.flock(fd, fcntl.LOCK_EX)
            fh.write((line + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fd)
    except OSError as exc:
        raise SalvageError(f"append {MANIFEST_NAME}: {exc}") from exc


def salvage(
    worktree_path: Path | str, dest_dir: Path | str, *, repo_root: Path | str,
    base_ref: str = "origin/main", dry_run: bool = True,
) -> SalvageResult:
    """Compute the salvage of ``worktree_path`` into ``dest_dir``.

    ``dry_run`` (the default) computes every artifact and the MANIFEST line and
    writes NOTHING (``written`` stays False); a real run writes them into a
    directory of its own plus one appended MANIFEST line."""
    worktree_path, dest_dir = Path(worktree_path), Path(dest_dir)
    name = worktree_path.name
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")
    remote = base_ref.split("/", 1)[0]
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")
    # A salvage measured against another repository says nothing about this tree.
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    root_dir = _run_git(common, Path(repo_root), "resolve repo_root").decode().strip()
    if root_dir != _run_git(common, worktree_path, "resolve worktree").decode().strip():
        raise SalvageError(f"{worktree_path} is not a worktree of {repo_root}")
    head, branch, unpushed = _identity(worktree_path, remote)
    staged, unstaged = _tracked_diffs(worktree_path)
    artifacts = [f"{name}{BUNDLE_SUFFIX}"] if unpushed > 0 else []
    patches = {f"{name}{suffix}": diff
               for suffix, diff in ((STAGED_SUFFIX, staged), (PATCH_SUFFIX, unstaged)) if diff}
    # Read the untracked set before creating anything, so a dest_dir inside the
    # worktree cannot become an untracked entry of the tree it describes.
    untracked = any(_run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], worktree_path, "list untracked",
    ).split(b"\0"))
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
    salvage_dir = _claim_dir(dest_dir, f"{name}-{_utc_stamp()}-{head[:8]}", create=not dry_run)
    relative = [f"{salvage_dir.name}/{artifact}" for artifact in artifacts + list(patches)]
    result = SalvageResult(
        name=name, branch=branch, head=head, unpushed=unpushed, salvage_dir=salvage_dir,
        artifacts=relative, untracked_pending=untracked,
        manifest_line=(
            f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
            + "".join(f"{artifact} " for artifact in relative)
        ),
    )
    if dry_run:
        return result
    for artifact in artifacts + list(patches):
        _write_artifact(salvage_dir, artifact, worktree_path, head, remote, patches)
    _append_manifest(dest_dir, result.manifest_line)
    result.written = True
    return result


def _patch_problems(paths: list[Path], head: str, cwd: Path) -> list[str]:
    """Apply ``paths`` in order to a scratch index loaded from ``head``: every
    patch but the last for real, the last as a check on what it builds on. Blobs
    go to a scratch object directory that borrows the repository's."""
    problem, objects = _git_problem(
        ["rev-parse", "--path-format=absolute", "--git-path", "objects"], cwd
    )
    if problem:
        return [f"{paths[0].name}: no repository to apply against: {problem}"]
    with tempfile.TemporaryDirectory(prefix="salvage-verify.") as scratch:
        env = {**os.environ, "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
               "GIT_INDEX_FILE": os.path.join(scratch, "index"),
               "GIT_OBJECT_DIRECTORY": os.path.join(scratch, "objects")}
        os.mkdir(env["GIT_OBJECT_DIRECTORY"])
        problem, _out = _git_problem(["read-tree", head], cwd, env)
        if problem:
            return [f"{paths[0].name}: cannot load head {head}: {problem}"]
        for index, path in enumerate(paths):
            check = ["--check"] if index == len(paths) - 1 else []
            problem, _out = _git_problem(["apply", *check, "--cached", str(path)], cwd, env)
            if problem:
                return [f"{path.name}: does not apply: {problem}"]
    return []


def verify(
    result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str | None = None
) -> list[str]:
    """Every problem that keeps this salvage from being complete; only an empty list
    lets a removal act: a missing or empty artifact, a rejected bundle, a patch that
    does not apply to ``result.head``, untracked work, and no usable ``repo_root``."""
    dest = Path(dest_dir)
    cwd = Path(repo_root) if repo_root is not None else None
    if cwd is None or not cwd.is_dir() or _git_problem(["rev-parse", "--git-dir"], cwd)[0]:
        cwd = None
    problems: list[str] = []
    if cwd is None:
        problems.append(f"no usable repo_root: {repo_root!r} is not a git checkout")
    usable = set()
    for artifact in result.artifacts:
        path = dest / artifact
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError as exc:
            problems.append(f"{artifact}: cannot stat ({exc})")
            continue
        if size is None:
            problems.append(f"{artifact}: missing")
        elif size == 0:
            problems.append(f"{artifact}: empty")
        elif artifact.endswith(BUNDLE_SUFFIX) and cwd is not None:
            problem, _out = _git_problem(["bundle", "verify", str(path)], cwd)
            if problem:
                problems.append(f"{artifact}: {problem}")
        else:
            usable.add(artifact)
    # Staged first: the worktree patch starts from the index, not from HEAD.
    patches = [a for a in result.artifacts if a.endswith(PATCH_SUFFIX) and a in usable]
    patches.sort(key=lambda a: not a.endswith(STAGED_SUFFIX))
    if cwd is not None and patches:
        problems.extend(_patch_problems([dest / a for a in patches], result.head, cwd))
    if result.untracked_pending:
        problems.append("untracked entries not salvaged (PR-2b)")
    return problems
