"""dispatch_worktree_isolation.py — per-dispatch ephemeral git worktree.

Feature-flag gated: only active when VNX_ISOLATED_WORKTREE=1.
Each dispatch gets a fresh worktree rooted at origin/main under
.vnx-data/worktrees/dispatch-{safe_id}/.  The worktree is removed
(success OR failure) so no state leaks between dispatches.

Dispatch-identity binding (OI-861):
A worktree's path is derived from the dispatch id, but the path alone does
not prove identity.  Two dispatches fired back-to-back on the same lane can
cross: one worker ends up in the other's worktree and — worst case — one
completion reaps the other's still-live worktree.  Every worktree created
here is therefore STAMPED with an atomic dispatch-id claim (O_EXCL), keyed on
the SAFE dispatch id, mirroring ``dispatch_process_registry``.
``verify_worktree_identity()`` is the hard refusal a worker must call before
doing any work in an offered worktree: a stamped id that differs from the
worker's own dispatch id fails loud instead of running for 25 minutes on the
wrong identity.

The claim REGISTRY lives under the canonical state root resolved by
``vnx_paths.resolve_paths()["VNX_STATE_DIR"]`` — the ADR-026 SSOT, not a
per-checkout ``<repo>/.vnx-data/state``.  A claim map that must serialize two
concurrent dispatches has to be visible from BOTH racing contexts (the main
checkout and the dispatch worktree); a repo-local pin would fork the registry
exactly as far apart as the racing worktrees are, and the OI-861 race would
run straight through it (PR #1274 review).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Collapse any char that is not alphanumeric, hyphen, or underscore.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SAFE_ID_LEN = 60

# The shared VNX central-install tree (e.g. ~/.vnx-system/versions/<v>/). A
# dispatch worktree must NEVER be created here — see CentralInstallWorktreeError.
_CENTRAL_INSTALL_ROOT = Path.home() / ".vnx-system"


class CentralInstallWorktreeError(RuntimeError):
    """Raised when dispatch-worktree resolution would land inside the shared
    VNX central install tree (``~/.vnx-system/...``) instead of a consumer
    project.

    A dispatch worktree must NEVER be created there: ``git worktree add``
    against the shared fabric checkout that every central-install consumer
    (SC/MC/SEO/...) reads from causes cross-consumer branch/worktree
    collisions (P0 provider-worktree-root-fix). Callers must resolve and pass
    an explicit consumer ``project_root`` instead of relying on the
    ``__file__``-based fallback — see ``resolve_consumer_project_root()``.
    """


class WorktreeClaimError(RuntimeError):
    """Base class for dispatch-worktree identity/claim failures (OI-861)."""


class WorktreeIdentityConflict(WorktreeClaimError):
    """The worktree is already claimed by a DIFFERENT dispatch id.

    This is the OI-861 crossing: a worker being offered a worktree that is
    stamped for another dispatch.  The worker MUST stop — running on the wrong
    identity delivers nothing and can have its worktree reaped mid-flight by
    the other dispatch's completion.
    """


class WorktreeIdentityMissing(WorktreeClaimError):
    """The worktree carries no dispatch-id claim at all.

    Identity cannot be verified, so a worker must not operate in it.  A missing
    claim means the worktree predates the stamping mechanism or was created by
    a lane that does not stamp — either way it is not provably this dispatch's.
    """


def _is_central_install_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(_CENTRAL_INSTALL_ROOT.resolve())
        return True
    except (OSError, ValueError):
        return False


def _sanitize_dispatch_id(dispatch_id: str) -> str:
    """Return a filesystem- and git-branch-safe version of dispatch_id."""
    return _UNSAFE_RE.sub("-", dispatch_id)[:_MAX_SAFE_ID_LEN]


def _dispatch_worktree_dir(project_root: Path, dispatch_id: str) -> Path:
    safe_id = _sanitize_dispatch_id(dispatch_id)
    # VNX_BENCH_WORKTREE_ROOT: place worktrees OUTSIDE the main repo so an UNSANDBOXED
    # worker (claude -p, deepseek-harness) cannot reach the main checkout via repo-relative
    # navigation and leak its output into the committed seed. From-scratch / introspection
    # tasks (t3 07/08/09, t4) triggered exactly this when worktrees lived under
    # <repo>/.vnx-data/worktrees/. The GLM agentic runner is sandboxed and is unaffected.
    # Default (unset): the in-repo path — production dispatch behaviour is unchanged.
    root_override = os.environ.get("VNX_BENCH_WORKTREE_ROOT", "").strip()
    if root_override:
        return Path(root_override).expanduser().resolve() / f"dispatch-{safe_id}"
    return project_root / ".vnx-data" / "worktrees" / f"dispatch-{safe_id}"


# ---------------------------------------------------------------------------
# Dispatch-id claim registry (OI-861)
#
# A worktree's identity is its CLAIM, not its path.  One JSON entry per
# sanitized dispatch id under the canonical state root
# (``vnx_paths.resolve_data_root(project_root) / "state"`` /
# ``dispatch_worktree_claims``) — the ADR-026 SSOT, shared by every checkout
# and worktree of a project.  The claim is created with O_EXCL so exactly one
# dispatch ever wins a given slot; every later reader compares the stamped
# dispatch_id against its own before operating in or removing the worktree.
# The registry must live OUTSIDE any single repo root: two racing dispatches
# resolve their project roots to DIFFERENT checkouts, and a per-checkout claim
# map serializes nothing between them — that fork is exactly the OI-861
# crossing (PR #1274 review).  Anchoring on the project root via vnx_paths
# keeps the claim map at the project's central state dir in production while
# letting tests pin it to one shared temp dir with VNX_DATA_DIR_EXPLICIT=1.
# ---------------------------------------------------------------------------

def _claim_dir(project_root: Path) -> Path:
    """Return the SHARED dispatch-worktree claim registry directory.

    Resolved via ``vnx_paths.resolve_data_root(project_root)`` — the canonical
    state-root resolver anchored on the given project root (ADR-026 SSOT), not
    a hardcoded ``<repo>/.vnx-data/state``.  Honors ``VNX_DATA_DIR_EXPLICIT=1``
    + ``VNX_DATA_DIR`` for tests and CI, so two simulated worktrees can share
    one temp claim map.
    """
    from vnx_paths import resolve_data_root  # noqa: PLC0415
    data_dir = Path(resolve_data_root(project_root))
    return data_dir / "state" / "dispatch_worktree_claims"


def _claim_path(safe_id: str, project_root: Path) -> Path:
    return _claim_dir(project_root) / f"{safe_id}.json"


def _read_claim_entry(safe_id: str, project_root: Path) -> "dict | None":
    """Read the claim for *safe_id*; None when absent or corrupt (never raises)."""
    path = _claim_path(safe_id, project_root)
    try:
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)
        if not isinstance(entry, dict):
            return None
        return entry
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "dispatch_worktree_isolation: unreadable claim %s: %s — "
            "treating as absent",
            path,
            exc,
        )
        return None


def _read_claim(dispatch_id: str, project_root: Path) -> "dict | None":
    return _read_claim_entry(_sanitize_dispatch_id(dispatch_id), project_root)


def _write_claim_atomic(
    dispatch_id: str,
    *,
    worktree_path: Path,
    project_root: Path,
) -> dict:
    """Claim *worktree_path* for *dispatch_id* with an O_EXCL write.

    This is the load-bearing atomicity of the identity binding: if the claim
    file already exists the write fails and the caller must decide whether the
    existing claim is the same dispatch (idempotent re-entry) or a different
    one (OI-861 crossing → WorktreeIdentityConflict).
    """
    safe_id = _sanitize_dispatch_id(dispatch_id)
    path = _claim_path(safe_id, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "dispatch_id": dispatch_id,
        "safe_id": safe_id,
        "worktree_path": str(Path(worktree_path).resolve()),
        "claimed_at": time.time(),
    }
    try:
        with open(path, "x", encoding="utf-8") as fh:
            json.dump(claim, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except FileExistsError:
        raise WorktreeClaimError(
            f"claim already exists for dispatch {dispatch_id!r} ({path})"
        ) from None
    log.info(
        "dispatch worktree claimed: %s -> dispatch %s",
        claim["worktree_path"],
        dispatch_id,
    )
    return claim


def _clear_claim(dispatch_id: str, project_root: Path) -> None:
    """Remove the claim for *dispatch_id*.  Idempotent, never raises."""
    try:
        _claim_path(_sanitize_dispatch_id(dispatch_id), project_root).unlink(missing_ok=True)
    except OSError as exc:
        log.debug(
            "dispatch_worktree_isolation: clear claim failed for %s: %s",
            dispatch_id,
            exc,
        )


def _claim_belongs_to_or_raise(
    dispatch_id: str,
    claim: dict,
    worktree_path: Path,
) -> None:
    """Raise WorktreeIdentityConflict when *claim* is not *dispatch_id*'s.

    The single canonical OI-861 refusal: a worktree already claimed by another
    dispatch identity must never be reused — not by a second create, not by a
    worker's verification, not by a teardown.
    """
    stamped_id = claim.get("dispatch_id")
    if stamped_id != dispatch_id:
        raise WorktreeIdentityConflict(
            f"worktree {worktree_path} is already claimed by dispatch "
            f"{stamped_id!r}, not {dispatch_id!r}; refusing — a worktree must "
            f"never be shared by two dispatch identities"
        )
    recorded_path = claim.get("worktree_path")
    if recorded_path:
        recorded = Path(recorded_path).resolve()
        if recorded != Path(worktree_path).resolve():
            raise WorktreeClaimError(
                f"worktree {worktree_path} is claimed by {dispatch_id!r} but "
                f"the claim points at {recorded}; refusing"
            )


def _repo_root_from_worktree_path(worktree_path: Path) -> Path:
    """Derive the project root from a standard dispatch-worktree path.

    ``<root>/.vnx-data/worktrees/dispatch-<safe>`` → ``<root>``.  Worktrees
    living under ``VNX_BENCH_WORKTREE_ROOT`` carry no structural marker, so
    their callers must pass ``project_root`` explicitly.
    """
    resolved = Path(worktree_path).resolve()
    if resolved.parent.name == "worktrees" and resolved.parent.parent.name == ".vnx-data":
        return resolved.parent.parent.parent
    raise WorktreeClaimError(
        f"cannot derive the project root from worktree path {resolved}; "
        f"pass project_root explicitly (the worktree may live under "
        f"VNX_BENCH_WORKTREE_ROOT)"
    )


def verify_worktree_identity(
    dispatch_id: str,
    worktree_path: Path,
    *,
    project_root: "Optional[Path]" = None,
) -> dict:
    """Verify that *worktree_path* is stamped for *dispatch_id*.  Fail-loud.

    A worker that is offered a worktree MUST call this before doing any work.
    Operating in a worktree whose stamped dispatch id differs from its own is
    the exact OI-861 failure that ran for 25 minutes and delivered nothing.

    The claim is looked up by the safe id carved out of the WORKTREE'S OWN NAME
    (``dispatch-<safe>``), not the caller's dispatch id — so a worker offered
    the wrong worktree reads the owner's claim and gets a truthful
    ``WorktreeIdentityConflict`` instead of a misleading "no claim".

    Args:
        project_root: the project root to anchor the claim-dir resolution on.
            Defaults to the root derived from a standard dispatch-worktree
            path (``<root>/.vnx-data/worktrees/dispatch-<safe>``).  Either way
            the registry resolves to the project's canonical state dir, so a
            worker in the worktree reads the same map the dispatcher wrote.

    Raises:
        WorktreeIdentityMissing: the worktree carries no dispatch-id claim.
        WorktreeIdentityConflict: the worktree is stamped for a different
            dispatch id (or the offered path is not a dispatch worktree).
        WorktreeClaimError: the claim is corrupt or points at another path.

    Returns the claim dict on success.
    """
    resolved_wt = Path(worktree_path).resolve()
    if project_root is not None:
        root = _resolve_project_root(project_root)
    else:
        root = _repo_root_from_worktree_path(resolved_wt)

    name = resolved_wt.name
    path_safe_id = (
        name[len("dispatch-"):] if name.startswith("dispatch-") else ""
    )
    claim = (
        _read_claim_entry(path_safe_id, root)
        if path_safe_id
        else _read_claim(dispatch_id, root)
    )

    if claim is None:
        raise WorktreeIdentityMissing(
            f"worktree {resolved_wt} carries no dispatch-id claim; refusing to "
            f"operate in an unclaimed worktree (expected dispatch "
            f"{dispatch_id!r})"
        )

    stamped_id = claim.get("dispatch_id")
    if stamped_id != dispatch_id:
        raise WorktreeIdentityConflict(
            f"worktree {resolved_wt} is stamped for dispatch {stamped_id!r}, "
            f"not {dispatch_id!r}; refusing to operate on the wrong identity"
        )

    recorded_path = claim.get("worktree_path")
    if recorded_path:
        recorded = Path(recorded_path).resolve()
        if recorded != resolved_wt:
            raise WorktreeClaimError(
                f"worktree {resolved_wt} is claimed by {dispatch_id!r} but the "
                f"claim points at {recorded}; refusing"
            )
    return claim


def _resolve_project_root(project_root: Optional[Path]) -> Path:
    if project_root is not None:
        root = project_root.resolve()
    else:
        try:
            from project_root import resolve_project_root  # type: ignore[attr-defined]
            root = Path(resolve_project_root(__file__)).resolve()
        except Exception:
            root = Path(__file__).resolve().parents[2]

    if _is_central_install_path(root):
        raise CentralInstallWorktreeError(
            f"dispatch-worktree root resolved to the shared VNX central install "
            f"({root}) instead of a consumer project; refusing to create/remove a "
            f"worktree there. Pass an explicit consumer project_root — see "
            f"resolve_consumer_project_root()."
        )
    return root


def resolve_consumer_project_root() -> Path:
    """Resolve the CONSUMER project root a dispatch worktree must be created in.

    Delegates to ``vnx_paths.resolve_paths()["PROJECT_ROOT"]`` — the canonical
    resolver that already threads ``VNX_PROJECT_ROOT`` (exported by the
    central-install shim) and CWD-git-toplevel resolution ahead of any
    ``__file__``-based fallback. This is the same resolver ``gate_executor``
    passes into ``create_gate_worktree`` (OI-708) and that the tmux lane's
    ``_resolve_invocation_project_root`` mirrors, so a consumer running the
    central install (SC/MC/SEO/...) resolves to ITS OWN project instead of the
    shared ``~/.vnx-system`` checkout — the root cause of cross-consumer
    dispatch-worktree collisions (P0 provider-worktree-root-fix).

    Callers MUST pass the result explicitly:
    ``create_dispatch_worktree(..., project_root=resolve_consumer_project_root())``.
    Relying on ``create_dispatch_worktree``'s own zero-arg ``__file__`` fallback
    resolves the shared fabric install in a central-install consumer.
    """
    from vnx_paths import resolve_paths  # noqa: PLC0415
    return Path(resolve_paths()["PROJECT_ROOT"]).resolve()


@contextmanager
def _worktree_lock(root: Path):
    """Serialize `git worktree` add/remove via an exclusive fcntl lock.

    Uses the SAME lock path as tmux_worktree._flock_context
    (``<repo>/.git/worktrees/.vnx-lock``) so the provider lane and the tmux lane
    never run concurrent ``git worktree add/remove`` against one repo. Concurrent
    adds contend on git's internal index/HEAD locks and fail in ~0.8s, which under
    VNX_BENCH_REQUIRE_ISOLATION=1 cascades into spurious isolation DNFs (observed
    2026-06-18 at --parallel 8: every provider cell DNF'd at 0.8s).
    """
    lock_dir = (root / ".git").resolve() / "worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".vnx-lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def create_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
) -> Path:
    """Create an ephemeral git worktree based on origin/main for one dispatch.

    Steps:
      1. git fetch origin main  (best-effort — warns on failure)
      2. git worktree add <path> -b dispatch/<safe_id> origin/main

    Returns the resolved worktree Path.
    Raises RuntimeError when worktree creation fails.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch_name = f"dispatch/{safe_id}"

    # VNX_BENCH_WORKTREE_BASE_REF: base the worktree on a given ref instead of origin/main.
    # The benchmark sets this to the bench checkout's HEAD so worktrees carry the bench
    # branch's committed task seeds (e.g. the t4_02 SWE-bench seed) without merging WIP
    # benchmark tasks to main. Default (unset) keeps origin/main — production unchanged.
    base_ref = os.environ.get("VNX_BENCH_WORKTREE_BASE_REF", "").strip() or "origin/main"
    is_remote = base_ref.startswith("origin/")

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    if is_remote:
        try:
            subprocess.run(
                ["git", "fetch", "origin", base_ref[len("origin/"):]],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.warning(
                "create_dispatch_worktree: git fetch %s failed (continuing): %s",
                base_ref, (exc.stderr or "").strip(),
            )

    with _worktree_lock(root):
        # OI-861 identity check BEFORE creating: if this worktree already exists
        # and is claimed by a DIFFERENT dispatch id, refuse immediately.  A
        # same-id re-entry (double-fire/retry) is idempotent and returns the
        # existing, correctly-stamped worktree.
        existing = _read_claim(dispatch_id, root)
        if existing is not None:
            _claim_belongs_to_or_raise(dispatch_id, existing, wt_path)
            log.info(
                "dispatch worktree already exists (claimed by %s): %s",
                dispatch_id, wt_path,
            )
            return wt_path.resolve()

        try:
            subprocess.run(
                [
                    "git", "worktree", "add",
                    str(wt_path),
                    "-b", branch_name,
                    base_ref,
                ],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            # A concurrent dispatch collided on this slot between our identity
            # check and the git add.  Re-read the claim: a different stamped id
            # is the OI-861 crossing — fail loud with the identity error.
            raced = _read_claim(dispatch_id, root)
            if raced is not None:
                _claim_belongs_to_or_raise(dispatch_id, raced, wt_path)
            raise RuntimeError(
                f"create_dispatch_worktree failed for {dispatch_id!r}: "
                f"{(exc.stderr or '').strip()}"
            ) from exc

        # Claim the freshly-created worktree atomically (O_EXCL).  From here on
        # the worktree's identity is bound to dispatch_id and no other dispatch
        # may reuse it.
        try:
            _write_claim_atomic(dispatch_id, worktree_path=wt_path, project_root=root)
        except WorktreeClaimError:
            raced = _read_claim(dispatch_id, root)
            if raced is not None:
                _claim_belongs_to_or_raise(dispatch_id, raced, wt_path)
            raise

    log.info("dispatch worktree created: %s (branch %s)", wt_path, branch_name)
    return wt_path.resolve()


def remove_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
) -> None:
    """Remove the ephemeral dispatch worktree.  Idempotent.

    Called on both success and failure paths — the worker's pushed branch
    survives on origin; only the local working tree is removed.

    Before removing the worktree, kills any processes still running inside it
    (OI-873): a SessionStart hook spawned from the worktree can survive the
    dispatch and hold the coordination DB write lock, blocking every fleet-wide
    track write.  Process cleanup happens OUTSIDE the worktree lock so it never
    blocks concurrent worktree creation.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)

    if not wt_path.exists():
        # Nothing to remove.  Drop any stale claim so a later dispatch mapping
        # to this safe id can claim cleanly (idempotent, never raises).
        _clear_claim(dispatch_id, root)
        log.debug("remove_dispatch_worktree: already absent: %s", wt_path)
        return

    # OI-861 hard refusal on teardown: never reap a worktree that is stamped
    # for a DIFFERENT dispatch id.  This is the "one's worktree reaped
    # mid-flight by the other's completion" half of the race.
    claim = _read_claim(dispatch_id, root)
    if claim is not None:
        _claim_belongs_to_or_raise(dispatch_id, claim, wt_path)

    # OI-873: kill processes still running inside this worktree BEFORE
    # attempting removal.  A zombie hook (bash + python child) will hold
    # the coordination DB lock and its CWD under the worktree path;
    # lsof +D catches both.
    try:
        from worktree_process_cleanup import kill_worktree_processes  # noqa: PLC0415
        kill_worktree_processes(wt_path)
    except Exception as _proc_exc:
        log.warning(
            "remove_dispatch_worktree: process cleanup failed for %s: %s — "
            "continuing with worktree removal",
            dispatch_id, _proc_exc,
        )

    # OI-877: also reap process groups recorded for this dispatch at spawn
    # time.  A dispatch process whose repo-root resolved to the MAIN checkout
    # has nothing open inside the worktree, so the lsof scan cannot see it —
    # the recorded PGID is the only handle that still reaches it (even after
    # it was reparented to launchd / PPID 1).  Keep the worktree scan above;
    # this is a second membership source, not a replacement.
    try:
        from dispatch_process_registry import (  # noqa: PLC0415
            clear_dispatch_pgids,
            kill_dispatch_pgids,
        )
        kill_dispatch_pgids(dispatch_id, repo_root=root)
        clear_dispatch_pgids(dispatch_id, repo_root=root)
    except Exception as _pgid_exc:
        log.warning(
            "remove_dispatch_worktree: pgid-based process cleanup failed for "
            "%s: %s — continuing with worktree removal",
            dispatch_id, _pgid_exc,
        )

    with _worktree_lock(root):
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
            log.info("dispatch worktree removed: %s", wt_path)
        except subprocess.CalledProcessError as exc:
            log.warning(
                "git worktree remove failed: %s; falling back to shutil.rmtree",
                (exc.stderr or "").strip(),
            )
            resolved = wt_path.resolve()
            # Safety: refuse to rmtree a path outside the project root.
            resolved.relative_to(root)
            if wt_path.is_symlink():
                raise RuntimeError(
                    f"refusing rmtree: {wt_path} is a symlink"
                )
            shutil.rmtree(str(resolved), ignore_errors=True)

        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.warning("git worktree prune failed: %s", (exc.stderr or "").strip())

        # The worktree is gone — release its identity claim so a later dispatch
        # mapping to the same safe id can claim cleanly.
        _clear_claim(dispatch_id, root)

    # Best-effort: delete the local dispatch branch (it lives on origin).
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch_name = f"dispatch/{safe_id}"
    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        log.debug("dispatch branch deleted locally: %s", branch_name)
    except Exception as exc:
        log.debug("branch deletion failed for %s: %s", branch_name, exc)
