"""worktree_salvage.py — preserve a dirty worktree before any later removal.

Writes the artifacts a locked dispatch worktree may still hold, plus one
appended ``MANIFEST.txt`` line in the format of the 27-09-2026 hand salvage::

    <tree dir> | <branch or (detached)> | head=<sha> | niet-op-remote=<n> | <artifacts> 

* ``<name>.bundle`` — commits not reachable from the remote named by
  ``base_ref`` (``origin`` for the default ``origin/main``). Thin: it restores
  with ``git fetch <bundle> HEAD`` in a clone that has that remote's history,
  and it cannot be cloned on its own. Bundling the full ancestry instead would
  write tens of megabytes per tree.
* ``<name>.patch`` — ``git diff --binary HEAD`` when the tree or index differs.
* ``<name>.untracked.tgz`` — untracked, non-ignored files minus the internal
  convenience symlinks (the filter is imported from cleanup_reviewed_worktrees).

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
_PATCH_SUFFIX = ".patch"
_TARBALL_SUFFIX = ".untracked.tgz"
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


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


def _internal_symlink(entry_path: Path, repo_root: Path) -> bool:
    """True for the ``.venv``-style link that points back into the checkout.
    One definition lives in ``cleanup_reviewed_worktrees``; import it there
    rather than carry a second copy that can drift."""
    scripts_dir = str(Path(__file__).resolve().parents[1])
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from cleanup_reviewed_worktrees import _is_internal_symlink

    return _is_internal_symlink(entry_path, repo_root)


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
    with tarfile.open(str(tmp), "w:gz") as tar:
        for rel in paths:
            tar.add(str(worktree_path / rel), arcname=rel, recursive=False)


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
    patch: bytes, untracked: list[str], artifacts: list[str], manifest_line: str,
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
        if f"{name}{_PATCH_SUFFIX}" in artifacts:
            pending.append(_stage(dest_dir, f"{name}{_PATCH_SUFFIX}", _write_patch, patch))
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
    patch = _run_git(["diff", "--binary", "HEAD"], worktree_path, "diff against HEAD")
    untracked = _untracked_paths(worktree_path, repo_root)

    artifacts = []
    if unpushed > 0:
        artifacts.append(f"{name}{_BUNDLE_SUFFIX}")
    if patch:
        artifacts.append(f"{name}{_PATCH_SUFFIX}")
    if untracked:
        artifacts.append(f"{name}{_TARBALL_SUFFIX}")
    manifest_line = (
        f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
        + "".join(f"{artifact} " for artifact in artifacts)
    )
    result = SalvageResult(
        name=name, branch=branch, head=head, unpushed=unpushed,
        artifacts=artifacts, manifest_line=manifest_line,
    )
    if dry_run:
        return result
    _write_all(
        worktree_path, dest_dir, name, head, remote, patch, untracked,
        artifacts, manifest_line,
    )
    result.written = True
    return result


def _bundle_problems(path: Path, cwd: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "bundle", "verify", str(path)], cwd=str(cwd),
            capture_output=True, text=True,
        )
    except OSError as exc:
        return [f"{path.name}: cannot run git bundle verify: {exc}"]
    if proc.returncode == 0:
        return []
    detail = (proc.stderr or proc.stdout).strip() or "no output"
    return [f"{path.name}: git bundle verify failed ({proc.returncode}): {detail}"]


def _tarball_problems(path: Path) -> list[str]:
    try:
        with tarfile.open(str(path), "r:gz") as tar:
            tar.getnames()
    except (OSError, tarfile.TarError) as exc:
        return [f"{path.name}: cannot list tarball: {exc}"]
    return []


def verify(
    result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str | None = None
) -> list[str]:
    """Every problem that keeps this salvage from being complete.

    An artifact named in ``result`` that is missing or empty is a problem, as
    is a bundle ``git bundle verify`` rejects and a tarball ``tarfile`` cannot
    list. An empty list is the only result a later removal may act on.
    ``repo_root`` is where ``git bundle verify`` runs; it defaults to
    ``dest_dir`` and is needed when the salvage directory is not itself inside
    the repository (a central ``$VNX_DATA_DIR``).
    """
    dest = Path(dest_dir)
    cwd = Path(repo_root) if repo_root is not None else dest
    problems = []
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
            problems.extend(_tarball_problems(path))
    return problems
