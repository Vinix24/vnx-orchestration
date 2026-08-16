"""dispatch_worktree_isolation.py — per-dispatch ephemeral git worktree.

Always active (default-on since OI-1090, 2026-08-10).
Each dispatch gets a fresh worktree, rooted at the caller's explicit base_ref
(``origin/main`` when none is given), under .vnx-data/worktrees/dispatch-{safe_id}/.
The worktree is removed (success OR failure) so no state leaks between dispatches.

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
``vnx_paths.resolve_data_root(project_root) / "state"`` — the ADR-026 SSOT, not
a per-checkout ``<repo>/.vnx-data/state``.  A claim map that must serialize two
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
import sys
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


class WorktreeOccupied(WorktreeClaimError):
    """A SIBLING live process already holds the occupancy lock for this worktree.

    OI-1232 (2026-08-15): three dispatches collided in ONE worktree and a
    sibling's operation reset another's branch pointer, wiping committed files
    from disk.  The identity-claim system above already refuses two DIFFERENT
    dispatch ids racing for one slot (``WorktreeIdentityConflict``).  The gap
    it leaves open is the SAME dispatch id claimed twice by two live
    processes at once: ``create_dispatch_worktree``'s idempotent re-entry
    ("same id → hand back the existing path") cannot tell a legitimate
    sequential retry (the earlier call already finished) apart from a second
    live process claiming the same worktree WHILE the first is still running
    — both get the same path with no signal, and whichever runs a
    destructive git operation first (e.g. a "clean start" reset on a
    presumed-dead retry) can wipe the other's work.  See ``_acquire_occupancy``
    for how the lock is held and released.
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
    base_sha: str = "",
    base_ref: str = "",
    branch: str = "",
    main_head_sha: str = "",
    fabric_version: str = "",
    unpushed_local_commits: "dict | None" = None,
) -> dict:
    """Claim *worktree_path* for *dispatch_id* with an O_EXCL write.

    This is the load-bearing atomicity of the identity binding: if the claim
    file already exists the write fails and the caller must decide whether the
    existing claim is the same dispatch (idempotent re-entry) or a different
    one (OI-861 crossing → WorktreeIdentityConflict).

    *base_sha*, *base_ref*, and *branch* are stored for teardown classification
    (L3 provider-lane reap): the claim carries enough metadata to construct a
    ``WorktreeHandle`` for ``tmux_worktree.classify()`` without re-deriving
    values that may have shifted since the worktree was created.

    *main_head_sha* is the main checkout's HEAD captured before worktree
    creation (OI-975): compared against the current HEAD at teardown to detect
    unexpected checkout jumps during a dispatch.

    *fabric_version* freezes the ``~/.vnx-system/current`` symlink target at
    creation time (OI-1149/OI-1061): compared against the live marker at
    teardown to detect a ``vnx update`` that ran mid-dispatch.

    *unpushed_local_commits* records local-vs-``origin/main`` divergence
    detected at creation time (OI-1149/OI-1061), or None when there was
    nothing to warn about — see ``_check_unpushed_local_commits``.
    """
    safe_id = _sanitize_dispatch_id(dispatch_id)
    path = _claim_path(safe_id, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "dispatch_id": dispatch_id,
        "safe_id": safe_id,
        "worktree_path": str(Path(worktree_path).resolve()),
        "claimed_at": time.time(),
        "base_sha": base_sha,
        "base_ref": base_ref,
        "branch": branch,
        "main_head_sha": main_head_sha,
        "fabric_version": fabric_version,
        "unpushed_local_commits": unpushed_local_commits,
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


def read_worktree_base_sha(
    dispatch_id: str, *, project_root: Optional[Path] = None
) -> "tuple[str | None, str]":
    """Return the (base_sha, base_ref) ``create_dispatch_worktree`` actually used.

    This is the ONE authoritative source for "the commit the dispatch worktree
    was based on" — read from the O_EXCL claim the worktree allocator writes,
    NOT re-derived from a lane's own ``plan.base_ref`` (which can name a
    different ref than the allocator's ``origin/main`` / ``VNX_BENCH_WORKTREE_BASE_REF``,
    e.g. a stale local ``main`` behind ``origin/main``, or a PR merge-commit
    checkout). Re-deriving is the root cause of OI-1106: the envelope lanes
    resolved ``base_sha`` from ``plan.base_ref`` while the worktree was based
    on ``origin/main``; when the two refs disagreed, a commit-less worktree was
    misclassified ``committed``/``pushed`` and the push+PR guard rejected a
    real success with ``status="failure"`` — a guard that flips a different
    test each run teaches the operator to ignore red CI.

    Mirrors ``remove_dispatch_worktree``'s own claim read (the L3 reap path),
    so the allocator's recorded base is the single classification input for
    every lane, never a second independently-drifting resolution.

    Returns ``(None, "<reason>")`` when no claim exists (the allocator never
    wrote one, or it predates the base_sha field): the caller degrades to a
    conservative ``classify_path`` result (clean-safe when base_sha is None)
    and the degradation is surfaced, never guessed into a false ``committed``.
    Never raises.
    """
    try:
        root = _resolve_project_root(project_root)
        claim = _read_claim(dispatch_id, root)
    except Exception as exc:  # noqa: BLE001 — claim read must never break enforcement
        return None, f"claim read raised: {exc}"
    if claim is None:
        return None, "no claim (worktree not created via create_dispatch_worktree)"
    base_sha = (claim.get("base_sha") or "").strip() or None
    base_ref = (claim.get("base_ref") or "").strip() or "origin/main"
    if base_sha is None:
        return None, f"claim has no base_sha (base_ref={base_ref!r})"
    return base_sha, base_ref


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


# ---------------------------------------------------------------------------
# Occupancy lock (OI-1232) — held for the dispatch's FULL create -> ... ->
# remove lifetime, not just the brief git-porcelain window _worktree_lock
# covers above.  Scoped per safe-id, one lock file per worktree slot, kept
# under the same canonical claim registry as the identity claims.
#
# Held via an flock on an OPEN file description, so a crashed holder's lock
# is released by the KERNEL the instant its process exits — no manual
# cleanup, no stale-lock timeout logic, and a lock nobody ever heals is
# impossible by construction (the task's own warning: "een lock die niet
# vrijgegeven wordt bij een crash is erger dan geen lock").
#
# Same-PROCESS re-entry (the idempotent-retry contract already exercised by
# test_dispatch_worktree_identity_race.py::test_same_dispatch_second_create_is_idempotent)
# is preserved by the _OCCUPANCY_LOCKS short-circuit below: POSIX advisory
# locks are per-OPEN-FILE-DESCRIPTION, not per-process, so a second open() +
# flock() from the very same process that already holds the lock would
# itself be treated as a conflicting request and must never be attempted.
# ---------------------------------------------------------------------------

_OCCUPANCY_LOCKS: "dict[str, object]" = {}


def _occupancy_lock_path(safe_id: str, project_root: Path) -> Path:
    return _claim_dir(project_root) / f"{safe_id}.occupancy"


def _acquire_occupancy(dispatch_id: str, wt_path: Path, project_root: Path) -> None:
    """Claim exclusive occupancy of *wt_path* for the calling process.

    No-op when this SAME process already holds it (idempotent re-entry).
    Raises WorktreeOccupied when a DIFFERENT live process holds it — the
    OI-1232 shape: a sibling dispatch must never be handed a worktree another
    dispatch is still actively using.
    """
    key = str(Path(wt_path).resolve())
    if key in _OCCUPANCY_LOCKS:
        return
    safe_id = _sanitize_dispatch_id(dispatch_id)
    lock_path = _occupancy_lock_path(safe_id, project_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise WorktreeOccupied(
            f"worktree for dispatch {dispatch_id!r} ({wt_path}) is already "
            f"occupied by another live process; refusing to hand out the same "
            f"worktree to a second concurrent claim (OI-1232) — a sibling "
            f"dispatch must never mutate a branch that another dispatch is "
            f"still using"
        ) from None
    _OCCUPANCY_LOCKS[key] = fh


def _release_occupancy(wt_path: Path) -> None:
    """Release this process's occupancy lock on *wt_path*.  Idempotent, never raises.

    A no-op when this process never held it — e.g. teardown running in a
    different process than the one that created the worktree.  That is safe:
    if the creator process is still alive it keeps its own hold; if it is
    dead the kernel already released the lock when it exited.
    """
    key = str(Path(wt_path).resolve())
    fh = _OCCUPANCY_LOCKS.pop(key, None)
    if fh is None:
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError as exc:
        log.debug("dispatch_worktree_isolation: occupancy unlock failed for %s: %s", wt_path, exc)
    finally:
        fh.close()


def _resolve_fabric_version_marker() -> str:
    """Return the fabric version the ``~/.vnx-system/current`` symlink names.

    Empty string when there is no central install (a from-source dev
    checkout, or the marker is unreadable) — callers must treat that as "not
    applicable", never as evidence of a version change.
    """
    marker = _CENTRAL_INSTALL_ROOT / "current"
    try:
        target = marker.resolve(strict=True)
    except OSError:
        return ""
    return target.name


def _check_unpushed_local_commits(
    root: Path, resolved_base_ref: str, base_sha: str
) -> "dict | None":
    """Warn loudly when the local tracking branch is ahead of *base_sha*.

    OI-1149/OI-1061: the worktree always branches from *resolved_base_ref*
    (``origin/main`` by default), silently ignoring commits the operator has
    made locally but not yet pushed — the worker then builds on a base the
    operator does not see in their own checkout.  This never blocks (a
    best-effort git probe must not gate a dispatch on its own failure), but a
    real divergence must be LOUD: printed to stderr (the dispatch's own
    output) and returned so the caller can stamp it on the claim — the one
    place that survives into the receipt.

    Returns None when there is nothing to warn about (no local branch of
    that name, local matches the remote, or local has DIVERGED rather than
    simply being ahead — a divergence is a rebase/merge situation this probe
    must not guess about).
    """
    if not resolved_base_ref.startswith("origin/"):
        return None
    local_branch = resolved_base_ref[len("origin/") :]
    local_sha_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "-q", local_branch],
        capture_output=True, text=True,
    )
    if local_sha_result.returncode != 0:
        return None
    local_sha = local_sha_result.stdout.strip()
    if not local_sha or local_sha == base_sha:
        return None
    is_ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base_sha, local_sha],
        capture_output=True, text=True,
    )
    if is_ancestor.returncode != 0:
        return None
    ahead_result = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", f"{base_sha}..{local_sha}"],
        capture_output=True, text=True,
    )
    if ahead_result.returncode != 0:
        return None
    try:
        ahead_count = int(ahead_result.stdout.strip())
    except ValueError:
        return None
    if ahead_count <= 0:
        return None
    warning = (
        f"WARN: worktree branches from {resolved_base_ref!r} (base {base_sha[:8]}) "
        f"but local {local_branch!r} is {ahead_count} commit(s) ahead "
        f"({local_sha[:8]}) — those unpushed commits are NOT in this worktree "
        f"(OI-1149/OI-1061)"
    )
    print(warning, file=sys.stderr)
    log.warning("create_dispatch_worktree: %s", warning)
    return {
        "local_branch": local_branch,
        "local_sha": local_sha,
        "ahead_count": ahead_count,
    }


def create_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
    base_ref: Optional[str] = None,
) -> Path:
    """Create an ephemeral git worktree for one dispatch, based on *base_ref*.

    Steps:
      1. git fetch origin <ref>  (best-effort — warns on failure; remote refs only)
      2. git worktree add <path> -b dispatch/<safe_id> <resolved base ref>

    The base ref actually used is resolved by precedence (highest wins):
      1. the explicit *base_ref* argument — the caller's real intent (e.g. the
         dispatch spec's ``plan.base_ref``, mirroring ``tmux_worktree.allocate()``).
      2. ``VNX_BENCH_WORKTREE_BASE_REF`` — a FALLBACK for callers that never pass
         an explicit ref at all (the provider-lane benchmark harness), so a bench
         run can redirect every worktree at the bench checkout's HEAD without
         threading a base_ref through that call site. It never overrides an
         explicit *base_ref*: silently redirecting a caller's own explicit
         request would reproduce the exact silent-wrong-base defect this
         parameter exists to fix, just from a different source.
      3. ``"origin/main"`` — the default when neither is given.

    An unresolvable base ref (bad ref, deleted dispatch branch, etc.) fails
    LOUD — both the ``rev-parse`` and the ``git worktree add`` step below raise
    with the requested ref named in the message. There is no fallback to
    origin/main on failure: a silent fallback is the one thing worse than a
    loud error here — it would put a worker's changes on the wrong tree with
    no signal until a human reviews an empty or conflicting diff.

    OI-1232: once this dispatch id is confirmed to own the slot (either as
    the fresh creator, or via same-id idempotent re-entry) it claims an
    OCCUPANCY lock for the resolved worktree path (see ``_acquire_occupancy``)
    that is held for the dispatch's full lifetime, released only by
    ``remove_dispatch_worktree``. A sibling process that already holds it
    gets ``WorktreeOccupied`` instead of the silent shared-worktree hand-out
    this function used to give a same-id double-fire while the original was
    still running.

    Returns the resolved worktree Path.
    Raises RuntimeError when the base ref is unresolvable or worktree creation fails.
    Raises WorktreeOccupied when a sibling live process already occupies this slot.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch_name = f"dispatch/{safe_id}"

    try:
        # VNX_BENCH_WORKTREE_BASE_REF: see precedence note in the docstring above.
        # The benchmark sets this to the bench checkout's HEAD so worktrees carry the bench
        # branch's committed task seeds (e.g. the t4_02 SWE-bench seed) without merging WIP
        # benchmark tasks to main. Default (unset) keeps origin/main — production unchanged.
        bench_override = os.environ.get("VNX_BENCH_WORKTREE_BASE_REF", "").strip()
        resolved_base_ref = (base_ref or "").strip() or bench_override or "origin/main"
        is_remote = resolved_base_ref.startswith("origin/")

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        if is_remote:
            try:
                subprocess.run(
                    ["git", "fetch", "origin", resolved_base_ref[len("origin/"):]],
                    cwd=str(root),
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                log.warning(
                    "create_dispatch_worktree: git fetch %s failed (continuing): %s",
                    resolved_base_ref, (exc.stderr or "").strip(),
                )

        # Resolve base_sha BEFORE adding the worktree — needed for teardown
        # classification (L3 provider-lane reap).  Mirrors tmux_worktree.allocate().
        try:
            sha_result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", resolved_base_ref],
                check=True, capture_output=True, text=True,
            )
            base_sha = sha_result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"create_dispatch_worktree: cannot resolve base_ref {resolved_base_ref!r} "
                f"for dispatch {dispatch_id!r}: {(exc.stderr or '').strip()}"
            ) from exc

        # OI-975: capture the main checkout HEAD BEFORE worktree creation so
        # teardown can detect unexpected checkout jumps during the dispatch.
        # Best-effort — a failed capture logs a warning and the field defaults
        # to "" in the claim, which skips the comparison at teardown.
        _main_head_sha = ""
        try:
            _main_head_result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            )
            _main_head_sha = _main_head_result.stdout.strip()
        except subprocess.CalledProcessError:
            log.warning(
                "create_dispatch_worktree: cannot resolve HEAD for %s; "
                "HEAD-jump detection skipped (OI-975)",
                dispatch_id,
            )

        # OI-1149/OI-1061: warn loudly when local main is ahead of what this
        # worktree is about to branch from, and freeze the fabric version so
        # teardown can detect a mid-dispatch `vnx update`.
        _unpushed = _check_unpushed_local_commits(root, resolved_base_ref, base_sha)
        _fabric_version = _resolve_fabric_version_marker()

        with _worktree_lock(root):
            # OI-861 identity check BEFORE creating: if this worktree already exists
            # and is claimed by a DIFFERENT dispatch id, refuse immediately.  A
            # same-id re-entry (double-fire/retry) is idempotent and returns the
            # existing, correctly-stamped worktree.
            existing = _read_claim(dispatch_id, root)
            if existing is not None:
                _claim_belongs_to_or_raise(dispatch_id, existing, wt_path)
                # OI-1232: the claim says this IS the same dispatch id, but a
                # matching claim alone cannot tell "the earlier call already
                # finished" apart from "a sibling process is still actively
                # using it right now".  Occupancy is the signal that closes
                # that gap — a live holder makes this raise WorktreeOccupied
                # instead of silently handing out a worktree two processes
                # then mutate at once.
                _acquire_occupancy(dispatch_id, wt_path, root)
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
                        resolved_base_ref,
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
                    f"create_dispatch_worktree failed for {dispatch_id!r} "
                    f"(base_ref={resolved_base_ref!r}): {(exc.stderr or '').strip()}"
                ) from exc

            # Claim the freshly-created worktree atomically (O_EXCL).  From here on
            # the worktree's identity is bound to dispatch_id and no other dispatch
            # may reuse it.  The claim carries base_sha + base_ref + branch so
            # teardown (L3 provider-lane reap) can construct a WorktreeHandle for
            # classification without re-deriving values that may have shifted.
            # main_head_sha is the OI-975 pre-dispatch HEAD snapshot; fabric_version
            # and unpushed_local_commits are the OI-1149/OI-1061 freeze + warning.
            try:
                _write_claim_atomic(
                    dispatch_id,
                    worktree_path=wt_path,
                    project_root=root,
                    base_sha=base_sha,
                    base_ref=resolved_base_ref,
                    branch=branch_name,
                    main_head_sha=_main_head_sha,
                    fabric_version=_fabric_version,
                    unpushed_local_commits=_unpushed,
                )
            except WorktreeClaimError:
                raced = _read_claim(dispatch_id, root)
                if raced is not None:
                    _claim_belongs_to_or_raise(dispatch_id, raced, wt_path)
                raise

            # We just won the O_EXCL claim write — nobody else can hold
            # occupancy on this brand-new slot yet, so this always succeeds.
            # From here on this process is the one a sibling's occupancy
            # check above will find.
            _acquire_occupancy(dispatch_id, wt_path, root)
    except Exception:
        # Creation never completed — this process must not keep holding the
        # slot occupied.  A future retry (this process or another) needs a
        # clean shot at the lock, not a phantom "still occupied".
        _release_occupancy(wt_path)
        raise

    log.info("dispatch worktree created: %s (branch %s)", wt_path, branch_name)
    return wt_path.resolve()


def remove_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
    terminal_id: str = "",
) -> None:
    """Remove the ephemeral dispatch worktree.  Idempotent.

    **L3 provider-lane reap (2026-08-08):** the worktree is CLASSIFIED before
    removal using the shared ``tmux_worktree.classify()`` — the same single
    implementation the tmux lane uses.  The classification determines whether
    the branch must be preserved (``committed`` → local branch kept, ``dirty``
    → entire worktree locked).  A failed remote check (ls-remote timeout,
    network error) is fail-closed: the classification falls back to
    ``committed`` and the branch survives.

    When *terminal_id* is provided, a ``provider_teardown_worktree`` event is
    emitted via ``EventStore`` with the same metadata fields
    (``worktree_state``, ``branch_kept_local``, ``branch_kept_remote``,
    ``preserved_path``) as the tmux lane's ``interactive_teardown_worktree``.

    OI-1232: releases this process's occupancy lock (see ``_acquire_occupancy``)
    unconditionally, before anything else — the calling process is done with
    the worktree the moment teardown starts, regardless of what classification
    or reap decide to do with it.

    Called on both success and failure paths.  Best-effort — never raises.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)
    _release_occupancy(wt_path)

    if not wt_path.exists():
        _clear_claim(dispatch_id, root)
        log.debug("remove_dispatch_worktree: already absent: %s", wt_path)
        return

    # OI-861 hard refusal on teardown: never reap a worktree that is stamped
    # for a DIFFERENT dispatch id.
    claim = _read_claim(dispatch_id, root)
    if claim is not None:
        _claim_belongs_to_or_raise(dispatch_id, claim, wt_path)

    # ── OI-975: detect HEAD jumps in the main checkout ─────────────────────
    # Compare the main checkout HEAD captured at worktree creation against the
    # current HEAD.  A difference means something (or someone) ran `git checkout`
    # in the main checkout during this dispatch — a governance break because the
    # main checkout's checked-out branch no longer matches what the dispatch was
    # launched against.  This check is DEFAULT-PATH (no flag), best-effort
    # (never raises), and namedrops the dispatch_id so the warning is traceable.
    _claim_main_head = (claim or {}).get("main_head_sha", "")
    if _claim_main_head:
        try:
            _current_head_result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            )
            _current_head = _current_head_result.stdout.strip()
            if _current_head and _current_head != _claim_main_head:
                log.warning(
                    "OI-975 HEAD-JUMP DETECTED: main checkout HEAD changed "
                    "during dispatch %s (was %s, now %s) — the checkout no longer "
                    "matches what this dispatch was launched against, so any "
                    "post-dispatch verification on this checkout may measure "
                    "different code than the dispatch actually built",
                    dispatch_id,
                    _claim_main_head[:12],
                    _current_head[:12],
                )
        except subprocess.CalledProcessError:
            log.debug(
                "remove_dispatch_worktree: cannot resolve current HEAD for %s; "
                "HEAD-jump check skipped",
                dispatch_id,
            )

    # ── OI-1149/OI-1061: detect fabric-version drift during the dispatch ───
    # The worktree's fabric version was frozen at creation time (the
    # ~/.vnx-system/current symlink target).  If it differs now, a `vnx
    # update` (or equivalent) ran WHILE this dispatch was in flight — the
    # fabric the worker actually ran under moved out from under it mid-run.
    # Best-effort, never raises: an unreadable marker on either side degrades
    # to "no comparison", never a false drift.
    _claim_fabric_version = (claim or {}).get("fabric_version", "")
    _current_fabric_version = _resolve_fabric_version_marker()
    if _claim_fabric_version and _current_fabric_version and _current_fabric_version != _claim_fabric_version:
        log.warning(
            "OI-1149/OI-1061 FABRIC VERSION DRIFT: dispatch %s was created "
            "against fabric %r but the fabric is now %r — the shared install "
            "moved (e.g. `vnx update`) while this dispatch was in flight",
            dispatch_id, _claim_fabric_version, _current_fabric_version,
        )

    # ── L3: classify before removing ──────────────────────────────────────
    # Build a WorktreeHandle from the claim so we can call the single
    # canonical classify() in tmux_worktree — no duplicate classification.
    # Fall back to safe defaults when the claim predates the L3 fields.
    safe_id = _sanitize_dispatch_id(dispatch_id)
    _claim_base_sha = (claim or {}).get("base_sha", "")
    _claim_branch = (claim or {}).get("branch", "") or f"dispatch/{safe_id}"
    _claim_base_ref = (claim or {}).get("base_ref", "") or "origin/main"

    # If the claim is missing base_sha (pre-L3 claim), derive it from the
    # base ref at teardown time.  This is a fallback: the base ref may have
    # advanced since the worktree was created, but a stale base_sha is still
    # safer than skipping classification entirely.
    if not _claim_base_sha:
        try:
            sha_result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", _claim_base_ref],
                check=True, capture_output=True, text=True,
            )
            _claim_base_sha = sha_result.stdout.strip()
        except subprocess.CalledProcessError:
            log.warning(
                "remove_dispatch_worktree: cannot resolve base_ref %r for %s; "
                "treating as dirty (fail-closed)",
                _claim_base_ref, dispatch_id,
            )
            _claim_base_sha = ""

    from tmux_worktree import WorktreeHandle, classify, reap  # noqa: PLC0415

    handle = WorktreeHandle(
        path=wt_path,
        branch=_claim_branch,
        base_sha=_claim_base_sha,
        base_ref=_claim_base_ref,
        dispatch_id=safe_id,
    )

    classification = classify(handle)
    log.info(
        "remove_dispatch_worktree: classified %s as %s (branch=%s base_sha=%s)",
        dispatch_id, classification, _claim_branch,
        _claim_base_sha[:8] if _claim_base_sha else "?",
    )

    # ── OI-873 / OI-877: process cleanup ─────────────────────────────────
    # Kill processes still running inside this worktree BEFORE removal.
    # Only needed when the worktree WILL be removed (clean/pushed/committed);
    # dirty worktrees are preserved — their processes may still be useful.
    if classification in ("clean", "pushed", "committed"):
        try:
            from worktree_process_cleanup import kill_worktree_processes  # noqa: PLC0415
            kill_worktree_processes(wt_path)
        except Exception as _proc_exc:
            log.warning(
                "remove_dispatch_worktree: process cleanup failed for %s: %s — "
                "continuing with worktree removal",
                dispatch_id, _proc_exc,
            )
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

    # ── L3: reap per classification ───────────────────────────────────────
    reap_result = reap(handle, classification)

    # Clear the claim when the worktree was removed (clean/pushed/committed).
    # A dirty worktree stays claimed so a later dispatch mapping to the same
    # safe id knows this slot is occupied.
    if reap_result.removed:
        _clear_claim(dispatch_id, root)

    # ── L3: emit worktree_state event ─────────────────────────────────────
    if terminal_id:
        try:
            from event_store import EventStore  # noqa: PLC0415
            store = EventStore()
            store.append(
                terminal_id,
                {
                    "type": "provider_teardown_worktree",
                    "dispatch_id": dispatch_id,
                    "data": {
                        "worktree_state": classification,
                        "branch_kept_local": reap_result.branch_kept_local,
                        "branch_kept_remote": reap_result.branch_kept_remote,
                        "preserved_path": str(reap_result.preserved_path)
                        if reap_result.preserved_path
                        else None,
                        "fabric_version_at_create": _claim_fabric_version or None,
                        "fabric_version_at_teardown": _current_fabric_version or None,
                        "unpushed_local_commits_at_create": (claim or {}).get(
                            "unpushed_local_commits"
                        ),
                    },
                },
            )
            if classification == "dirty":
                store.append(
                    terminal_id,
                    {
                        "type": "provider_teardown_preserved",
                        "dispatch_id": dispatch_id,
                        "data": {
                            "preserved_path": str(reap_result.preserved_path)
                            if reap_result.preserved_path
                            else None,
                        },
                    },
                )
            log.info(
                "remove_dispatch_worktree: emitted provider_teardown_worktree "
                "state=%s for %s",
                classification, dispatch_id,
            )
        except Exception as _event_exc:
            log.warning(
                "remove_dispatch_worktree: event emission failed for %s: %s",
                dispatch_id, _event_exc,
            )
