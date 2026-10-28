"""worktree_salvage.py — preserve a worktree's unpushed commits (WT-2a1a).

:func:`salvage` preserves exactly one kind of tree state: the commits no
``origin`` ref reaches. :func:`verify` returns ``[]`` only when the tree holds
nothing else — ``git status --porcelain=v2 -z --untracked-files=all
--ignored=matching --ignore-submodules=none`` names no entry at all (tracked,
staged, untracked, ignored (OI-2417), submodule or unmerged; no allowlist yet,
so a ``__pycache__`` blocks too), every gitlink whose checkout exists is named
even when it is clean (a submodule keeps its commits in its own object store, so
no superproject bundle can carry them; nothing here classifies or excuses one —
that is WT-2a1b), a gitlink whose checkout was never made is not named, HEAD is
readable and still the salvaged commit, and every artifact is a non-empty bundle
that unpacks to that head in a scratch repository borrowing no object store of
the checkout. An artifact or ``dest_dir`` inside the tree is refused: removal
would delete the evidence. A real run writes
``dest_dir/<name>-<UTC>-<head[:8]>/*`` and appends the MANIFEST line.
"""
from __future__ import annotations

import fcntl
import itertools
import os
import re
import shutil
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
_STATUS = ["status", "--porcelain=v2", "-z", "--untracked-files=all",
           "--ignored=matching", "--ignore-submodules=none"]
_PATH_FIELD = {"1": 8, "2": 9, "u": 10, "?": 1, "!": 1}  # spaces before the path
_HEADER_LIMIT = 1 << 20  # a bundle header may never end; bound the read


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


def _run_git(args: list[str], cwd: Path, what: str) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
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


def _status_paths(worktree_path: Path) -> list[str]:
    fields = iter(_run_git(_STATUS, worktree_path, "read status").split(b"\0"))
    paths: list[str] = []
    for chunk in fields:
        entry = os.fsdecode(chunk)
        if not entry:
            continue
        spaces = _PATH_FIELD.get(entry[0])
        parts = entry.split(" ", spaces) if spaces else []
        if len(parts) != spaces + 1 or not parts[spaces]:
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
    uncovered, unpushed = _status_paths(worktree_path), int(raw)
    # A populated submodule's commits ride in no bundle this salvage writes; record
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
        manifest_line=(f"{name} | {branch} | head={head} | niet-op-remote={unpushed} | "
                       + "".join(f"{artifact} " for artifact in relative)))
    if dry_run:
        return result
    for artifact in artifacts:
        _write_bundle(salvage_dir, artifact, worktree_path, remote)
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


def _tree_problems(tree: Path, result: SalvageResult) -> list[str]:
    """The tree's own state, read fail-closed: no readable HEAD, a HEAD that moved
    past the salvaged commit, or any state this salvage does not cover."""
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
        entries = _status_paths(tree)
    except SalvageError as exc:
        return problems + [f"tree {result.name}: state unreadable: {exc}"]
    if entries:
        rest = f" (+{len(entries) - 1} more)" if len(entries) > 1 else ""
        problems.append(f"tree has state this salvage does not cover: {entries[0]}{rest}")
    try:
        gitlinks = _populated_submodules(tree)
    except SalvageError as exc:
        return problems + [f"tree {result.name}: state unreadable: {exc}"]
    # Dirty or clean, a populated submodule holds commits no bundle carried. One
    # already named above as a status entry needs no second line.
    problems.extend(
        f"tree has state this salvage does not cover: {path}"
        for path in gitlinks if path not in entries)
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
        else:
            try:
                problems.extend(f"{artifact}: {p}" for p in
                                _bundle_problems(path, result.head, repo))
            except SalvageError as exc:  # a git failure is a problem, never a pass
                problems.append(f"{artifact}: {exc}")
    return problems
