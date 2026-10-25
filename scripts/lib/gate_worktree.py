"""gate_worktree.py — ephemeral git worktree checkout for gate execution (OI-708).

codex_gate and gemini_review spawn a real CLI agent (codex/gemini) that can run
its own shell tools (sed/rg/cat/...) against whatever `cwd` the subprocess
inherits. The gate's diff is fetched authoritatively via `gh pr diff`, but the
agent's OWN file reads previously hit the orchestrator's ambient working
directory — which can be stale relative to the PR branch (uncommitted local
drift, or simply not fast-forwarded to `origin/<branch>` HEAD).

This module checks out `origin/<branch>` into an isolated, detached-HEAD git
worktree so the gate subprocess's `cwd` matches the diff it is reviewing, and
removes the worktree unconditionally afterward (success or failure) so no
per-execution worktree leaks and the orchestrator's own checkout is never
touched (no `git checkout` in the caller's tree).

R9 (dlv-8acd09eb3983): every gate worktree also gets a fixed, gate-authored
AGENTS.md at its root. codex loads an AGENTS.md by itself at start and its own
system prompt tells the model to follow such files; the repo's root AGENTS.md
is gitignored, so the worktree had none and codex spent its first turn
searching for one, also outside the repo, and sent that output again on every
later turn. The file is written by the gate and never taken from the tree under
review: a PR that changes AGENTS.md would otherwise write its reviewer's
instructions. For git the worktree stays the PR's tree (see
``_install_review_instructions``).

PR-4 round 2: every gate checkout also gets a lifetime lock — a sidecar
``<checkout>.live`` next to it in ``.vnx-data/worktrees/`` (outside the tree,
so the checkout's own ``git status`` never sees it), held with
``fcntl.flock(LOCK_EX)`` from before ``git worktree add`` until
``remove_gate_worktree``. A running reviewer therefore signals "this checkout
is in use" for its whole lifetime, and a process that dies drops the lock by
itself. ``gate_checkout_liveness`` is the read-only probe the worktree sweep
uses before it may release a gate tree.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import IO, List, Optional, Sequence

from atomic_io import atomic_write_text

log = logging.getLogger(__name__)

# Collapse any char that is not alphanumeric, hyphen, or underscore.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SAFE_LEN = 60

# Conservative git ref-name allowlist: must start with an alnum (never '-', so
# a branch cannot be mistaken for a git option), and contain only the chars
# real branch names use. Whitespace of any kind is excluded implicitly.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Managed worktree directory prefix — must match _worktree_dir() below. Used
# by remove_gate_worktree to scope its shutil.rmtree fallback.
_MANAGED_WORKTREE_PREFIX = "gate-"

# The gate-authored instruction file (R9). codex 0.153.4 reads, per directory
# from the project root (nearest ancestor holding `.git`) down to its cwd, the
# first of AGENTS.override.md, AGENTS.md, then the configured
# project_doc_fallback_filenames (default empty). The gate runs the reviewer
# with cwd at the worktree root, so this name at that root is what it loads.
# R12: the last sentence. The rest says where not to look, and on a small diff
# codex then looked nowhere and was refused as degenerate (PR #2173).
REVIEW_INSTRUCTIONS_FILENAME = "AGENTS.md"
REVIEW_INSTRUCTIONS_TEXT = (
    "# Review workspace\n"
    "\n"
    "This directory is a disposable checkout made by the VNX review gate. "
    "Every instruction for this review is in the prompt you were given. "
    "There are no other AGENTS.md files to follow: do not search for AGENTS.md, "
    "CLAUDE.md or other instruction files, in this directory or outside it, "
    "and do not read outside this directory. "
    "Inside it, the files are there to check the diff against before you give "
    "your verdict.\n"
)

# Names that win over AGENTS.md in the same directory and are therefore
# removed from the worktree. A fallback filename only counts when AGENTS.md is
# absent, so it can never win over the gate's file.
_OUTRANKING_INSTRUCTION_FILENAMES = ("AGENTS.override.md",)


class GateWorktreeError(RuntimeError):
    """Raised when a gate's isolated worktree cannot be created.

    Callers MUST treat this as a gate execution failure — never fall back to
    running the gate subprocess in the orchestrator's ambient checkout, as
    that reintroduces the stale-checkout bug this module exists to fix.
    """


def _sanitize(value: str) -> str:
    return _UNSAFE_RE.sub("-", value or "")[:_MAX_SAFE_LEN] or "unknown"


def _resolve_project_root(project_root: Optional[Path]) -> Path:
    if project_root is not None:
        return project_root.resolve()
    try:
        from project_root import resolve_project_root  # type: ignore[attr-defined]
        return Path(resolve_project_root(__file__)).resolve()
    except Exception:
        return Path(__file__).resolve().parents[2]


def _worktree_dir(project_root: Path, gate: str, identifier: str) -> Path:
    token = secrets.token_hex(4)
    return (
        project_root / ".vnx-data" / "worktrees"
        / f"{_MANAGED_WORKTREE_PREFIX}{_sanitize(gate)}-{_sanitize(identifier)}-{token}"
    )


# Sidecar suffix for the gate lifetime lock. The file lives NEXT TO the
# checkout in ``.vnx-data/worktrees/``, never inside it, so the checkout's own
# ``git status`` stays clean and the ``.gitignore`` of the PR under review is
# irrelevant. Readers that list that directory as a directory must not treat
# the sidecar as a worktree (``worktree_inventory`` reads git's porcelain and
# is safe by construction; ``orphan_sweep`` skips it explicitly).
LIFETIME_LOCK_SUFFIX = ".live"

# Open sidecar handles of THIS process, keyed by the resolved checkout path.
# Holding the open file object is what keeps the flock; a process that dies
# drops it without any cleanup code running.
_lifetime_locks: dict[str, IO[str]] = {}


def gate_lifetime_lock_path(wt_path: Path) -> Path:
    """The sidecar that carries the lifetime lock of gate checkout *wt_path*."""
    wt_path = Path(wt_path)
    return wt_path.parent / (wt_path.name + LIFETIME_LOCK_SUFFIX)


def _acquire_lifetime_lock(wt_path: Path) -> None:
    """Take the exclusive lifetime lock of *wt_path* and keep its handle open.

    MUST run before ``git worktree add``: no gate checkout may ever exist
    without a held lock. Raises :class:`GateWorktreeError` when the sidecar
    cannot be created or locked (and removes it again in that case).
    """
    key = str(Path(wt_path).resolve())
    if key in _lifetime_locks:
        return
    sidecar = gate_lifetime_lock_path(wt_path)
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        handle = open(sidecar, "a+")
    except OSError as exc:
        raise GateWorktreeError(
            f"create_gate_worktree: cannot open lifetime lock {sidecar}: {exc}"
        ) from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError as exc:
        handle.close()
        try:
            sidecar.unlink()
        except OSError:
            log.warning("could not remove lifetime lock %s after a failed lock", sidecar)
        raise GateWorktreeError(
            f"create_gate_worktree: cannot take lifetime lock {sidecar}: {exc}"
        ) from exc
    _lifetime_locks[key] = handle


def _release_lifetime_lock(wt_path: Path) -> None:
    """Release this process's lifetime lock on *wt_path*. Idempotent, never raises."""
    key = str(Path(wt_path).resolve())
    handle = _lifetime_locks.pop(key, None)
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as exc:  # pragma: no cover - closing releases the lock anyway
        log.warning("could not unlock lifetime lock for %s: %s", wt_path, exc)
    finally:
        try:
            handle.close()
        except OSError as exc:  # pragma: no cover - a close failure is not actionable
            log.warning("could not close lifetime lock for %s: %s", wt_path, exc)


def _unlink_lifetime_sidecar(wt_path: Path) -> None:
    """Remove the sidecar of *wt_path*. Idempotent, never raises."""
    sidecar = gate_lifetime_lock_path(wt_path)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - best-effort cleanup
        log.warning("could not remove lifetime lock %s: %s", sidecar, exc)


def gate_checkout_liveness(wt_path: Path, *, detail: Optional[List[str]] = None) -> str:
    """Probe whether gate checkout *wt_path* is in use, without disturbing it.

    Returns exactly one of:

    * ``"live"`` — the sidecar exists and ``LOCK_EX|LOCK_NB`` raises
      ``BlockingIOError``: a gate seat holds the lifetime lock right now.
    * ``"released"`` — the sidecar exists and the non-blocking lock succeeds;
      it is released again at once (no stale lock is left behind).
    * ``"no_lock"`` — there is no sidecar (checkout predates the lifetime
      lock, or was not created by ``create_gate_worktree``).
    * ``"unmeasured"`` — any other ``OSError``. *detail*, when given, receives
      the error text so a caller can name it in its keep reason.

    Only a positive measurement may ever clear a caller to act; the sweep
    keeps the tree on ``unmeasured`` and everything it does not recognise.
    """
    sidecar = gate_lifetime_lock_path(wt_path)
    try:
        fd = os.open(sidecar, os.O_RDWR)
    except FileNotFoundError:
        return "no_lock"
    except OSError as exc:
        if detail is not None:
            detail.append(f"{sidecar}: {exc}")
        return "unmeasured"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "live"
        except OSError as exc:
            if detail is not None:
                detail.append(f"{sidecar}: {exc}")
            return "unmeasured"
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            if detail is not None:
                detail.append(f"{sidecar}: {exc}")
            return "unmeasured"
        return "released"
    finally:
        os.close(fd)


def _validate_branch(branch: str, *, gate: str, identifier: str) -> None:
    """Reject any branch that is not a safe git ref name.

    MUST run before any git subprocess call: a branch beginning with '-'
    (e.g. ``--upload-pack=...``) would otherwise be parsed by git as an
    option rather than a ref.
    """
    if not _SAFE_BRANCH_RE.match(branch):
        raise GateWorktreeError(
            f"create_gate_worktree: unsafe branch {branch!r} rejected before any git call "
            f"(gate={gate!r}, identifier={identifier!r})"
        )


def _git_common_dir(root: Path) -> Path:
    """Resolve the shared git dir for a repo, handling linked worktrees.

    Thin wrapper around the shared ``git_common.git_common_dir`` (the single
    implementation every lane imports — OI-1713). Kept as a local symbol so
    callers and tests that import ``gate_worktree._git_common_dir`` keep
    working. In a linked git worktree ``.git`` is an ASCII file pointing at
    the real gitdir, not a directory — deriving a lock path from
    ``root / ".git"`` would raise ``NotADirectoryError`` (OI-905).
    """
    from git_common import git_common_dir
    return git_common_dir(root)


@contextmanager
def _worktree_lock(root: Path):
    """Serialize `git worktree` add/remove via an exclusive fcntl lock.

    Uses the SAME lock path as dispatch_worktree_isolation / tmux_worktree
    (``<git-common-dir>/worktrees/.vnx-lock``) so gate execution never races
    other lanes' concurrent ``git worktree add/remove`` against this repo. The
    common dir is resolved via ``git rev-parse --git-common-dir`` (see
    ``git_common.git_common_dir``) so this works from a linked worktree where
    ``.git`` is a file, not a directory.
    """
    lock_dir = _git_common_dir(root) / "worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".vnx-lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def fetch_branch(
    branch: str,
    *,
    gate: str,
    identifier: str,
    project_root: Optional[Path] = None,
) -> Path:
    """Validate *branch* and ``git fetch origin <branch>``. Returns the resolved project root.

    Extracted from ``create_gate_worktree`` (OI-1887) so a caller that only
    needs to confirm a PR branch is fetchable — the harness-lane pre-flight in
    ``gate_runner._run_harness_lane_path``, which isolates via
    ``provider_dispatch --base-ref`` rather than a ``create_gate_worktree``
    checkout — does not have to create a worktree just to find that out, and
    never duplicates the validate-then-fetch sequence.

    Raises GateWorktreeError when branch is empty, unsafe, or the fetch fails
    — callers must book an execution failure rather than silently falling
    back to another ref (e.g. main).
    """
    if not branch:
        raise GateWorktreeError(
            f"fetch_branch requires a non-empty branch (gate={gate!r}, identifier={identifier!r})"
        )
    _validate_branch(branch, gate=gate, identifier=identifier)

    root = _resolve_project_root(project_root)
    try:
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GateWorktreeError(
            f"fetch_branch: git fetch origin {branch!r} failed: {detail}"
        ) from exc
    return root


def _git_in_worktree(wt_path: Path, args: Sequence[str], *, stdin: Optional[str] = None) -> str:
    """Run one git command in the gate worktree; a failure is a GateWorktreeError."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(wt_path), input=stdin,
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GateWorktreeError(f"git {' '.join(args)} failed in {wt_path}: {detail}") from exc
    return proc.stdout


def _icase(names: Sequence[str]) -> List[str]:
    """Pathspecs matching *names* without case.

    On a case-insensitive filesystem (the APFS default) a tracked `agents.md`
    is the very file codex opens as AGENTS.md.
    """
    return [f":(icase){name}" for name in names]


def _tracked_entries(wt_path: Path, pathspecs: Sequence[str]) -> List[str]:
    out = _git_in_worktree(wt_path, ["ls-files", "-z", "--", *pathspecs])
    return [entry for entry in out.split("\0") if entry]


def _visible_to_git(wt_path: Path, pathspecs: Sequence[str]) -> List[str]:
    """`git status --porcelain` entries for *pathspecs*, untracked files included."""
    out = _git_in_worktree(
        wt_path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs],
    )
    return [entry for entry in out.split("\0") if entry]


def _skip_worktree(wt_path: Path, entries: Sequence[str]) -> None:
    """Set skip-worktree on *entries* in this worktree's own index.

    A linked worktree has an index of its own
    (``<common-dir>/worktrees/<id>/index``), so the bit changes nothing for
    the main checkout or any other worktree. git stops comparing the copy on
    disk with the index: HEAD and the index keep the PR's version.
    """
    if entries:
        _git_in_worktree(
            wt_path, ["update-index", "--skip-worktree", "-z", "--stdin"],
            stdin="".join(f"{entry}\0" for entry in entries),
        )


def _remove_entry(path: Path) -> None:
    """Remove the file, symlink or directory at *path*; a symlink is never followed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _ignore_in_worktree(wt_path: Path, name: str) -> None:
    """Ignore an untracked, unignored *name* through the worktree's own root .gitignore.

    git has no exclude file per worktree: ``info/exclude`` lives in the common
    dir and every worktree shares it, and a per-worktree ``core.excludesFile``
    needs ``extensions.worktreeConfig`` in the shared config. The root
    .gitignore on disk belongs to this worktree alone. When the PR tracks one,
    its rules stay, the rule is appended and skip-worktree hides the change;
    when it does not, the new file ignores itself as well.
    """
    gitignore = wt_path / ".gitignore"
    tracked = _tracked_entries(wt_path, [".gitignore"])
    existing = ""
    if gitignore.is_file() and not gitignore.is_symlink():
        # latin-1 maps every byte to one code point, so the PR's bytes come
        # back unchanged whatever their encoding.
        existing = gitignore.read_bytes().decode("latin-1")
    else:
        _remove_entry(gitignore)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    rules = [f"/{name}"] if tracked else [f"/{name}", "/.gitignore"]
    atomic_write_text(gitignore, existing + "\n".join(rules) + "\n", encoding="latin-1")
    _skip_worktree(wt_path, tracked)


def _install_review_instructions(wt_path: Path) -> None:
    """Write the gate-authored AGENTS.md into a fresh gate worktree (R9).

    Whatever the PR tree holds under that name, or under a name that outranks
    it, is replaced in the worktree only, and `git status --porcelain` stays
    empty for every name touched here, whatever its state in the PR tree:
      * ignored (this repo's `/AGENTS.md`): nothing more to do;
      * tracked: skip-worktree in the worktree's own index, so git keeps the
        PR's version and the reviewer sees it only through the diff;
      * neither: an ignore rule in the worktree's own root .gitignore.
    The result is checked: a name git still sees fails the setup.
    """
    names = (REVIEW_INSTRUCTIONS_FILENAME, *_OUTRANKING_INSTRUCTION_FILENAMES)
    target = wt_path / REVIEW_INSTRUCTIONS_FILENAME
    try:
        _skip_worktree(wt_path, _tracked_entries(wt_path, _icase(names)))
        for name in names:
            _remove_entry(wt_path / name)
        atomic_write_text(target, REVIEW_INSTRUCTIONS_TEXT)
        if _visible_to_git(wt_path, [REVIEW_INSTRUCTIONS_FILENAME]):
            _ignore_in_worktree(wt_path, REVIEW_INSTRUCTIONS_FILENAME)
        still_visible = _visible_to_git(wt_path, [*_icase(names), ".gitignore"])
        written = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateWorktreeError(f"placing {target} failed: {exc}") from exc
    if still_visible:
        raise GateWorktreeError(f"git still sees {still_visible!r} after writing {target}")
    if written != REVIEW_INSTRUCTIONS_TEXT:
        raise GateWorktreeError(f"{target} does not hold the gate's text after writing")


def create_gate_worktree(
    *,
    branch: str,
    gate: str,
    identifier: str,
    project_root: Optional[Path] = None,
) -> Path:
    """Fetch origin/<branch> and check it out into an isolated detached worktree.

    Steps:
      1. git fetch origin <branch>  (via fetch_branch — validates + fetches)
      2. take the lifetime lock on the sidecar ``<path>.live``
      3. git worktree add --detach <path> origin/<branch>
      4. write the gate-authored AGENTS.md at the worktree root (R9), for
         every gate alike: a reviewer that does not load it pays nothing

    Raises GateWorktreeError when branch is empty or any step fails —
    callers must fail the gate rather than silently falling back to the
    orchestrator's (possibly stale) checkout, or reviewing without the file.
    A worktree whose step 4 failed is removed (with its lock and sidecar)
    before the error is raised.
    """
    root = fetch_branch(branch, gate=gate, identifier=identifier, project_root=project_root)
    wt_path = _worktree_dir(root, gate, identifier)
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    # The lifetime lock is taken BEFORE the checkout exists, so the sweep can
    # never observe a gate tree without a lock held by its creating process.
    _acquire_lifetime_lock(wt_path)

    created = False
    try:
        with _worktree_lock(root):
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), f"origin/{branch}"],
                cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
            )
        created = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GateWorktreeError(
            f"create_gate_worktree: git worktree add failed for gate={gate!r} "
            f"branch={branch!r}: {detail}"
        ) from exc
    finally:
        if not created:
            _release_lifetime_lock(wt_path)
            _unlink_lifetime_sidecar(wt_path)

    try:
        _install_review_instructions(wt_path)
    except GateWorktreeError as exc:
        remove_gate_worktree(wt_path, project_root=root)
        raise GateWorktreeError(
            f"create_gate_worktree: review instructions not written for gate={gate!r} "
            f"branch={branch!r}: {exc}"
        ) from exc

    log.info("gate worktree created: %s (origin/%s, detached)", wt_path, branch)
    return wt_path.resolve()


def remove_gate_worktree(wt_path: Optional[Path], *, project_root: Optional[Path] = None) -> None:
    """Remove a gate worktree. Best-effort + idempotent — never raises.

    Called on both success and failure paths (including when the gate
    subprocess itself failed) so no worktree ever leaks past one gate
    execution.
    """
    if not wt_path:
        return
    wt_path = Path(wt_path)
    if not wt_path.exists():
        # The checkout is already gone; release its lifetime lock and drop the
        # sidecar so no stale "released" marker is left for the sweep.
        _release_lifetime_lock(wt_path)
        _unlink_lifetime_sidecar(wt_path)
        return

    root = _resolve_project_root(project_root)
    try:
        with _worktree_lock(root):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
                )
                log.info("gate worktree removed: %s", wt_path)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                log.warning(
                    "git worktree remove failed: %s; falling back to shutil.rmtree", detail,
                )
                resolved = wt_path.resolve()
                if wt_path.is_symlink():
                    log.warning("refusing rmtree: %s is a symlink", wt_path)
                    return
                # Verify the STRUCTURE of the actual path being removed — a
                # managed gate worktree always looks like
                # ``.vnx-data/worktrees/gate-*`` — rather than reconstructing
                # an expected root from project_root (which may itself fall
                # back to a __file__-anchored resolution). All three checks
                # are plain string comparisons on `resolved`, a function
                # parameter, never a Path(...) join.
                grandparent_name = resolved.parent.parent.name
                parent_name = resolved.parent.name
                if not (
                    resolved.name.startswith(_MANAGED_WORKTREE_PREFIX)
                    and parent_name == "worktrees"
                    and grandparent_name == ".vnx-data"
                ):
                    log.warning(
                        "refusing rmtree: %s does not match the managed gate-worktree "
                        "structure ('.vnx-data/worktrees/%s*'); got parent=%r grandparent=%r",
                        resolved, _MANAGED_WORKTREE_PREFIX, parent_name, grandparent_name,
                    )
                    return
                shutil.rmtree(str(resolved), ignore_errors=True)

            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                log.warning("git worktree prune failed: %s", detail)
    except Exception as exc:  # pragma: no cover - cleanup must never raise
        log.warning("remove_gate_worktree: unexpected error removing %s: %s", wt_path, exc)
    finally:
        # The seat is over on every path, so its lifetime lock must be gone
        # from this process either way. The sidecar is removed only once the
        # checkout itself is gone; a tree the cleanup refused to remove keeps
        # its (now unheld) sidecar for a later sweep/age rule.
        _release_lifetime_lock(wt_path)
        if not wt_path.exists():
            _unlink_lifetime_sidecar(wt_path)
