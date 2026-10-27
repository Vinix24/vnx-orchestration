"""worktree_salvage.py — preserve a dirty worktree before any later removal.

Writes the artifacts a locked dispatch worktree may still hold, plus one
appended ``MANIFEST.txt`` line in the format of the 27-09-2026 hand salvage::

    <tree dir> | <branch or (detached)> | head=<sha> | niet-op-remote=<n> | <artifacts> 

* ``<name>.bundle`` — commits not reachable from the remote named by
  ``base_ref`` (``origin`` for the default ``origin/main``). Thin: it restores
  with ``git fetch <bundle> HEAD`` in a clone that has that remote's history,
  and it cannot be cloned on its own. Bundling the full ancestry instead would
  write tens of megabytes per tree.
* ``<name>.staged.patch`` — HEAD to index (``git diff --binary --cached HEAD``).
* ``<name>.patch`` — index to worktree (``git diff --binary``). Two patches,
  not one ``git diff HEAD``: a staged change the tree undoes again cancels out
  in that single diff and the index content would be lost. ``git status``, not
  an empty diff, decides whether there is tracked work.
* ``<name>.untracked.tgz`` — untracked, non-ignored files minus the internal
  convenience symlinks (the filter is imported from worktree_inventory).
  ``git ls-files --others`` reports an untracked nested repository as the one
  entry ``nested/``, so every entry is archived recursively: the archive holds
  every file under that directory, symlinks as links and never followed.
  :func:`verify` refuses an archive that holds such an entry without its files.

Dry-run by default and no caller yet: a later PR removes a tree only after
:func:`verify` returns an empty list.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MANIFEST_NAME = "MANIFEST.txt"

_DETACHED = "(detached)"
_BUNDLE_SUFFIX = ".bundle"
_STAGED_SUFFIX = ".staged.patch"
_PATCH_SUFFIX = ".patch"
_TARBALL_SUFFIX = ".untracked.tgz"
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
# Fixed output whatever the operator's diff config says, so the patch applies.
_DIFF = [
    "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv",
    "--src-prefix=a/", "--dst-prefix=b/",
]
# Porcelain v1 codes of a path in a merge conflict.
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
    artifacts: list[str] = field(default_factory=list)
    # The entries git listed as untracked (a nested repository is one ``dir/``
    # entry), so verify can prove the tarball carries them.
    untracked_paths: list[str] = field(default_factory=list)
    manifest_line: str = ""
    written: bool = False


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()


def _run_git(args: list[str], cwd: Path, what: str) -> bytes:
    """Run one git command and fail closed: a non-zero exit is a SalvageError
    carrying git's own stderr, never an empty result a caller could mistake
    for "nothing to salvage"."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    except OSError as exc:
        raise SalvageError(f"{what}: cannot run git: {exc}") from exc
    if proc.returncode != 0:
        detail = _text(proc.stderr) or _text(proc.stdout) or "no output"
        raise SalvageError(f"{what}: git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def _head_identity(worktree_path: Path) -> tuple[str, str]:
    head = _text(_run_git(["rev-parse", "HEAD"], worktree_path, "resolve HEAD"))
    if not _SHA_RE.fullmatch(head):
        raise SalvageError(f"resolve HEAD: git returned no full commit sha ({head!r})")
    branch = _text(_run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path, "resolve branch"))
    return head, _DETACHED if branch == "HEAD" else branch


def _unpushed_count(worktree_path: Path, head: str, remote: str) -> int:
    raw = _text(_run_git(
        ["rev-list", "--count", head, "--not", f"--remotes={remote}"],
        worktree_path, "count unpushed commits",
    ))
    if not raw.isdigit():
        raise SalvageError(f"count unpushed commits: git returned {raw!r}, not a count")
    return int(raw)


def _tracked_changes(worktree_path: Path) -> list[tuple[str, str]]:
    """``(XY, path)`` for every tracked change ``git status`` reports; with
    ``--untracked-files=no`` no ``??`` entry is listed. A rename or copy
    carries its source path as one more NUL-separated field."""
    raw = _run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=no"], worktree_path,
        "read status",
    )
    fields = iter(raw.split(b"\0"))
    changes = []
    for chunk in fields:
        if not chunk:
            continue
        entry = os.fsdecode(chunk)
        if len(entry) < 4 or entry[2] != " ":
            raise SalvageError(f"read status: unparseable entry {entry!r}")
        changes.append((entry[:2], entry[3:]))
        if "R" in entry[:2] or "C" in entry[:2]:
            next(fields, None)
    return changes


def _tracked_diffs(worktree_path: Path) -> tuple[bytes, bytes]:
    """``(HEAD to index, index to worktree)`` as binary patches. A status that
    reports tracked work neither patch carries is a SalvageError, never an
    empty salvage, and so is a merge conflict: no patch carries its stages."""
    changes = _tracked_changes(worktree_path)
    unmerged = sorted(path for xy, path in changes if xy in _UNMERGED)
    if unmerged:
        raise SalvageError(f"unmerged paths no patch can carry: {', '.join(unmerged)}")
    staged = _run_git([*_DIFF, "--cached", "HEAD"], worktree_path, "diff HEAD to index")
    unstaged = _run_git(_DIFF, worktree_path, "diff index to worktree")
    if changes and not (staged or unstaged):
        raise SalvageError(
            f"git status reports {len(changes)} tracked change(s) but both diffs are empty"
        )
    return staged, unstaged


def _internal_symlink(entry_path: Path, repo_root: Path) -> bool:
    """True for the ``.venv``-style link that points back into the checkout.
    One definition lives in ``worktree_inventory``; import it there rather
    than carry a second copy that can drift."""
    lib_dir = str(Path(__file__).resolve().parent)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from worktree_inventory import is_internal_symlink

    return is_internal_symlink(entry_path, repo_root)


def _untracked_paths(worktree_path: Path, repo_root: Path) -> list[str]:
    raw = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], worktree_path,
        "list untracked files",
    )
    paths = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        rel = os.fsdecode(chunk)
        if not _internal_symlink(worktree_path / rel, repo_root):
            paths.append(rel)
    return sorted(paths)


def _write_bundle(tmp: Path, worktree_path: Path, head: str, remote: str) -> None:
    # ``git bundle create`` refuses a raw sha ("Refusing to create empty
    # bundle"), so name the checked-out tip as HEAD, then prove the bundle
    # advertises exactly the head we measured.
    _run_git(
        ["bundle", "create", str(tmp), "HEAD", "--not", f"--remotes={remote}"],
        worktree_path, "create bundle",
    )
    _run_git(["bundle", "verify", str(tmp)], worktree_path, "verify bundle")
    heads = _text(_run_git(["bundle", "list-heads", str(tmp)], worktree_path, "list bundle heads"))
    if not any(line.split()[0] == head for line in heads.splitlines() if line.strip()):
        raise SalvageError(f"create bundle: bundle does not advertise head {head}")


def _write_patch(tmp: Path, patch: bytes) -> None:
    tmp.write_bytes(patch)


def _write_tarball(tmp: Path, worktree_path: Path, paths: list[str]) -> None:
    """Every file of every listed entry. ``recursive=True`` because git lists
    an untracked nested repository as the single entry ``nested/``: without
    recursion the archive would hold that directory line and none of the work
    under it. ``tarfile`` reads each entry with ``lstat``, so a symlink is
    stored as a link and never followed."""
    with tarfile.open(str(tmp), "w:gz") as tar:
        for rel in paths:
            tar.add(str(worktree_path / rel), arcname=rel, recursive=True)


def _stage(
    dest_dir: Path, artifact: str, writer: Callable[..., None], *args: object
) -> tuple[Path, Path]:
    """Fill a unique scratch file in ``dest_dir``; the caller renames it last."""
    fd, raw = tempfile.mkstemp(dir=str(dest_dir), prefix=f".{artifact}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(raw)
    try:
        writer(tmp, *args)
    except (OSError, tarfile.TarError) as exc:
        tmp.unlink(missing_ok=True)
        raise SalvageError(f"write {artifact}: {exc}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, dest_dir / artifact


def _append_manifest(dest_dir: Path, line: str) -> None:
    """Append one line under an exclusive lock: a concurrent salvage into the
    same directory can never interleave two halves of a line."""
    path = dest_dir / MANIFEST_NAME
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as exc:
        raise SalvageError(f"open {MANIFEST_NAME}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError as exc:
        raise SalvageError(f"append {MANIFEST_NAME}: {exc}") from exc
    finally:
        os.close(fd)


def _write_all(
    worktree_path: Path, dest_dir: Path, name: str, head: str, remote: str,
    patches: dict[str, bytes], untracked: list[str], artifacts: list[str],
    manifest_line: str,
) -> None:
    """Stage every artifact, rename them into place, then append the MANIFEST
    line: a line only exists for a complete salvage, and no half-written file
    ever lands under its final name."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[Path, Path]] = []
    try:
        if f"{name}{_BUNDLE_SUFFIX}" in artifacts:
            pending.append(_stage(
                dest_dir, f"{name}{_BUNDLE_SUFFIX}",
                _write_bundle, worktree_path, head, remote,
            ))
        for artifact, patch in patches.items():
            pending.append(_stage(dest_dir, artifact, _write_patch, patch))
        if f"{name}{_TARBALL_SUFFIX}" in artifacts:
            pending.append(_stage(
                dest_dir, f"{name}{_TARBALL_SUFFIX}",
                _write_tarball, worktree_path, untracked,
            ))
        for tmp, final in pending:
            os.replace(tmp, final)
        _append_manifest(dest_dir, manifest_line)
    except BaseException:
        for tmp, _final in pending:
            if tmp.exists():
                tmp.unlink()
        raise


def salvage(
    worktree_path: Path | str,
    dest_dir: Path | str,
    *,
    repo_root: Path | str,
    base_ref: str = "origin/main",
    dry_run: bool = True,
) -> SalvageResult:
    """Compute the salvage of ``worktree_path`` into ``dest_dir``.

    ``dry_run`` (the default) computes every artifact and the MANIFEST line and
    writes NOTHING; :attr:`SalvageResult.written` stays False. A real run writes
    the artifacts plus one appended ``MANIFEST.txt`` line.
    """
    worktree_path = Path(worktree_path)
    dest_dir = Path(dest_dir)
    repo_root = Path(repo_root)
    name = worktree_path.name
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")
    remote = base_ref.split("/", 1)[0] if "/" in base_ref else "origin"
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")

    head, branch = _head_identity(worktree_path)
    unpushed = _unpushed_count(worktree_path, head, remote)
    staged, unstaged = _tracked_diffs(worktree_path)
    untracked = _untracked_paths(worktree_path, repo_root)

    artifacts = []
    if unpushed > 0:
        artifacts.append(f"{name}{_BUNDLE_SUFFIX}")
    patches = {
        f"{name}{suffix}": patch
        for suffix, patch in ((_STAGED_SUFFIX, staged), (_PATCH_SUFFIX, unstaged))
        if patch
    }
    artifacts.extend(patches)
    if untracked:
        artifacts.append(f"{name}{_TARBALL_SUFFIX}")
    manifest_line = (
        f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
        + "".join(f"{artifact} " for artifact in artifacts)
    )
    result = SalvageResult(
        name=name, branch=branch, head=head, unpushed=unpushed,
        artifacts=artifacts, untracked_paths=untracked, manifest_line=manifest_line,
    )
    if dry_run:
        return result
    _write_all(
        worktree_path, dest_dir, name, head, remote, patches, untracked,
        artifacts, manifest_line,
    )
    result.written = True
    return result


def _git_problem(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """``(problem, stdout)`` of one git command run by :func:`verify`; the
    problem is None only for exit status 0."""
    command = " ".join(args[:2])
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env,
        )
    except OSError as exc:
        return f"cannot run git {command}: {exc}", ""
    if proc.returncode == 0:
        return None, proc.stdout.strip()
    detail = (proc.stderr or proc.stdout).strip() or "no output"
    return f"git {command} failed ({proc.returncode}): {detail}", ""


def _bundle_problems(path: Path, cwd: Path) -> list[str]:
    problem, _out = _git_problem(["bundle", "verify", str(path)], cwd)
    return [f"{path.name}: {problem}"] if problem else []


def _patch_problems(paths: list[Path], head: str, cwd: Path) -> list[str]:
    """Apply ``paths`` in order to a scratch index loaded from ``head``: the
    staged patch to head, the worktree patch on top of it. New blobs go to a
    scratch object directory that borrows the repository's objects, so the
    check writes nothing into the repository."""
    problem, objects = _git_problem(
        ["rev-parse", "--path-format=absolute", "--git-path", "objects"], cwd,
    )
    if problem:
        return [f"{paths[0].name}: no repository to apply against: {problem}"]
    with tempfile.TemporaryDirectory(prefix="salvage-verify.") as scratch:
        env = {
            **os.environ,
            "GIT_INDEX_FILE": os.path.join(scratch, "index"),
            "GIT_OBJECT_DIRECTORY": os.path.join(scratch, "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
        }
        os.mkdir(env["GIT_OBJECT_DIRECTORY"])
        problem, _out = _git_problem(["read-tree", head], cwd, env)
        if problem:
            return [f"{paths[0].name}: cannot load head {head}: {problem}"]
        for path in paths:
            problem, _out = _git_problem(["apply", "--cached", str(path)], cwd, env)
            if problem:
                return [f"{path.name}: does not apply: {problem}"]
    return []


def _tarball_problems(path: Path, entries: list[str]) -> list[str]:
    """Every problem with a tarball that should carry ``entries``: a tarball
    that cannot be listed, an entry missing from it, and a recorded directory
    entry that holds none of its files. That last one is the round-2 archive —
    a non-empty tarball naming ``nested/`` and nothing under it — which a
    removal gated on this check would take for a complete salvage. No recorded
    entry means the contents cannot be confirmed, so it is a problem too: a
    tarball nobody can check is never a pass."""
    try:
        with tarfile.open(str(path), "r:gz") as tar:
            members = tar.getmembers()
    except (OSError, tarfile.TarError) as exc:
        return [f"{path.name}: cannot list tarball: {exc}"]
    if not entries:
        return [f"{path.name}: no untracked entries recorded, contents unconfirmed"]
    listed = {member.name.rstrip("/"): member for member in members}
    names = list(listed)
    problems = []
    for entry in entries:
        rel = entry.rstrip("/")
        if rel not in listed:
            problems.append(f"{path.name}: untracked entry {rel} is not in the archive")
        elif listed[rel].isdir() and not any(name.startswith(rel + "/") for name in names):
            problems.append(f"{path.name}: archived directory {rel} holds no files")
    return problems


def verify(
    result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str | None = None
) -> list[str]:
    """Every problem that keeps this salvage from being complete.

    An artifact named in ``result`` that is missing or empty is a problem, as
    is a bundle ``git bundle verify`` rejects, a tarball ``tarfile`` cannot
    list or that does not carry every untracked entry git listed (an archived
    directory entry with no files under it is an incomplete salvage, however
    non-empty the tarball), and a staged or worktree patch that does not apply
    in order to ``result.head``. An empty list is the only result a later
    removal may act on. ``repo_root`` is where git runs; it defaults to
    ``dest_dir`` and is needed when the salvage directory is not itself inside
    the repository (a central ``$VNX_DATA_DIR``).
    """
    dest = Path(dest_dir)
    cwd = Path(repo_root) if repo_root is not None else dest
    problems = []
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
        elif artifact.endswith(_BUNDLE_SUFFIX):
            problems.extend(_bundle_problems(path, cwd))
        elif artifact.endswith(_TARBALL_SUFFIX):
            problems.extend(_tarball_problems(path, result.untracked_paths))
        else:
            usable.add(artifact)
    # Staged first: the worktree patch starts from the index, not from HEAD.
    patches = sorted(
        (a for a in result.artifacts if a.endswith(_PATCH_SUFFIX)),
        key=lambda a: not a.endswith(_STAGED_SUFFIX),
    )
    # A missing or empty patch is already a problem; its order cannot be checked.
    if patches and all(a in usable for a in patches):
        problems.extend(_patch_problems([dest / a for a in patches], result.head, cwd))
    return problems
