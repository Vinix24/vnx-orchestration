"""worktree_salvage.py — preserve a dirty worktree before any later removal.

Salvage is step one of the ``worktree-opruiming-structureel`` track. This
module writes the three artifacts a locked dispatch worktree may still hold,
plus a ``MANIFEST.txt`` line describing them, in exactly the format the
hand-run salvage of 27-09-2026 used under
``$VNX_DATA_DIR/salvage/worktrees-2026-09-27/``::

    <tree dir name> | <branch or (detached)> | head=<full sha> | niet-op-remote=<n> | <artifacts> 

Artifacts:

* ``<name>.bundle`` — only when ``head`` has commits not reachable from the
  remote named by ``base_ref`` (``origin`` for the default ``origin/main``).
* ``<name>.patch`` — only when the working tree or index differs from ``head``.
* ``<name>.untracked.tgz`` — only when there are untracked, non-ignored files,
  with every internal convenience symlink (``.venv`` style) left out. That
  filter is ``cleanup_reviewed_worktrees._is_internal_symlink``, imported
  rather than copied, so the two agree on what counts as "not work".

There is no caller yet: a later PR removes a worktree only after
:func:`verify` returns an empty list.

A ``--not --remotes=<remote>`` bundle is *thin*: it records the remote tip as
a prerequisite, so it restores with ``git fetch <bundle> HEAD`` in a clone
that already has the remote's history, and it cannot be cloned on its own.
That is deliberate — bundling the full ancestry instead would write tens of
megabytes per tree.
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
    """Run one git command and fail closed: any non-zero exit is a SalvageError
    carrying git's own stderr, never an empty result a caller could mistake
    for "nothing to salvage"."""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    except OSError as exc:
        raise SalvageError(f"{what}: cannot run git: {exc}") from exc
    if proc.returncode != 0:
        detail = _text(proc.stderr) or _text(proc.stdout) or "no output"
        raise SalvageError(
            f"{what}: git {' '.join(args)} failed ({proc.returncode}): {detail}"
        )
    return proc.stdout


def _remote_name(base_ref: str) -> str:
    """``base_ref`` names the remote to compare against: ``origin/main`` means
    ``origin``; a bare ref keeps the conventional ``origin``."""
    remote = base_ref.split("/", 1)[0] if "/" in base_ref else "origin"
    if not remote:
        raise SalvageError(f"base_ref {base_ref!r} names no remote")
    return remote


def _head_identity(worktree_path: Path) -> tuple[str, str]:
    head = _text(_run_git(["rev-parse", "HEAD"], worktree_path, "resolve HEAD"))
    if not _SHA_RE.fullmatch(head):
        raise SalvageError(f"resolve HEAD: git returned no full commit sha ({head!r})")
    branch = _text(
        _run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path, "resolve branch")
    )
    return head, _DETACHED if branch == "HEAD" else branch


def _unpushed_count(worktree_path: Path, head: str, remote: str) -> int:
    raw = _text(
        _run_git(
            ["rev-list", "--count", head, "--not", f"--remotes={remote}"],
            worktree_path,
            "count unpushed commits",
        )
    )
    if not raw.isdigit():
        raise SalvageError(f"count unpushed commits: git returned {raw!r}, not a count")
    return int(raw)


def _tracked_diff(worktree_path: Path) -> bytes:
    return _run_git(["diff", "--binary", "HEAD"], worktree_path, "diff against HEAD")


def _internal_symlink(entry_path: Path, repo_root: Path) -> bool:
    """True when ``entry_path`` is the ``.venv``-style link that points back
    into the checkout. One definition lives in ``cleanup_reviewed_worktrees``;
    import it there rather than carry a second copy that can drift."""
    scripts_dir = str(Path(__file__).resolve().parents[1])
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from cleanup_reviewed_worktrees import _is_internal_symlink

    return _is_internal_symlink(entry_path, repo_root)


def _untracked_paths(worktree_path: Path, repo_root: Path) -> list[str]:
    raw = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        worktree_path,
        "list untracked files",
    )
    paths = []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        rel = os.fsdecode(chunk)
        if _internal_symlink(worktree_path / rel, repo_root):
            continue
        paths.append(rel)
    return sorted(paths)


def _scratch(dest_dir: Path, artifact: str) -> Path:
    """A unique scratch name in ``dest_dir`` so the final file lands by rename."""
    fd, path = tempfile.mkstemp(dir=str(dest_dir), prefix=f".{artifact}.", suffix=".tmp")
    os.close(fd)
    return Path(path)


def _write_bytes(dest_dir: Path, artifact: str, data: bytes) -> tuple[Path, Path]:
    tmp = _scratch(dest_dir, artifact)
    try:
        tmp.write_bytes(data)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SalvageError(f"write {artifact}: {exc}") from exc
    return tmp, dest_dir / artifact


def _write_bundle(
    worktree_path: Path, dest_dir: Path, name: str, head: str, remote: str
) -> tuple[Path, Path]:
    artifact = f"{name}{_BUNDLE_SUFFIX}"
    tmp = _scratch(dest_dir, artifact)
    try:
        # ``git bundle create`` refuses a raw sha ("Refusing to create empty
        # bundle"), so name the checked-out tip as HEAD and then prove the
        # bundle advertises exactly the head we measured.
        _run_git(
            ["bundle", "create", str(tmp), "HEAD", "--not", f"--remotes={remote}"],
            worktree_path,
            "create bundle",
        )
        _run_git(["bundle", "verify", str(tmp)], worktree_path, "verify bundle")
        advertised = _text(
            _run_git(
                ["bundle", "list-heads", str(tmp)], worktree_path, "list bundle heads"
            )
        )
        if not any(line.split()[0] == head for line in advertised.splitlines() if line.strip()):
            raise SalvageError(f"create bundle: bundle does not advertise head {head}")
    except SalvageError:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, dest_dir / artifact


def _write_tarball(
    worktree_path: Path, dest_dir: Path, name: str, paths: list[str]
) -> tuple[Path, Path]:
    artifact = f"{name}{_TARBALL_SUFFIX}"
    tmp = _scratch(dest_dir, artifact)
    try:
        with tarfile.open(str(tmp), "w:gz") as tar:
            for rel in paths:
                tar.add(str(worktree_path / rel), arcname=rel, recursive=False)
    except (OSError, tarfile.TarError) as exc:
        tmp.unlink(missing_ok=True)
        raise SalvageError(f"write {artifact}: {exc}") from exc
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
    worktree_path: Path,
    dest_dir: Path,
    name: str,
    head: str,
    remote: str,
    patch: bytes,
    untracked: list[str],
    artifacts: list[str],
    manifest_line: str,
) -> None:
    """Write every artifact to a scratch name, then rename them into place and
    append the MANIFEST line last: a manifest line only exists for a complete
    salvage, and a failure leaves no half-written artifact under its final name."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[Path, Path]] = []
    try:
        if f"{name}{_BUNDLE_SUFFIX}" in artifacts:
            pending.append(_write_bundle(worktree_path, dest_dir, name, head, remote))
        if f"{name}{_PATCH_SUFFIX}" in artifacts:
            pending.append(_write_bytes(dest_dir, f"{name}{_PATCH_SUFFIX}", patch))
        if f"{name}{_TARBALL_SUFFIX}" in artifacts:
            pending.append(_write_tarball(worktree_path, dest_dir, name, untracked))
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

    ``dry_run`` (the default) computes every artifact and the MANIFEST line
    and writes NOTHING; :attr:`SalvageResult.written` stays False. A real run
    writes the artifacts plus one appended ``MANIFEST.txt`` line.
    """
    worktree_path = Path(worktree_path)
    dest_dir = Path(dest_dir)
    repo_root = Path(repo_root)
    name = worktree_path.name
    if not name:
        raise SalvageError(f"{worktree_path}: worktree path names no directory")

    remote = _remote_name(base_ref)
    head, branch = _head_identity(worktree_path)
    unpushed = _unpushed_count(worktree_path, head, remote)
    patch = _tracked_diff(worktree_path)
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
        name=name,
        branch=branch,
        head=head,
        unpushed=unpushed,
        artifacts=artifacts,
        manifest_line=manifest_line,
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
        if not path.is_file():
            problems.append(f"{artifact}: missing")
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            problems.append(f"{artifact}: cannot stat ({exc})")
            continue
        if size == 0:
            problems.append(f"{artifact}: empty")
            continue
        if artifact.endswith(_BUNDLE_SUFFIX):
            problems.extend(_bundle_problems(path, cwd))
        elif artifact.endswith(_TARBALL_SUFFIX):
            problems.extend(_tarball_problems(path))
    return problems
