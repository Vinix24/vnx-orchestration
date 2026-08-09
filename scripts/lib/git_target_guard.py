"""git_target_guard.py — refuse dispatch-context git operations that target the main checkout (OI-975).

A dispatch must operate exclusively inside its own ephemeral worktree. A git
call that resolves its target to the MAIN checkout (the operator's checkout)
instead of the dispatch worktree can move the operator's HEAD onto a
``dispatch/<id>`` / PR branch without the operator asking. A branch switch
under the operator's hands invalidates every measurement running against the
main checkout, and the uncommitted work the main checkout carries is at risk
of being carried onto the wrong branch.

Two failure modes are caught here:

1. A dispatch-context git call with NO explicit target path inherits the
   dispatcher process's cwd, which is the main checkout. (The OI-1008 pattern
   that ``dispatch_govern._run_worktree_git`` already guards locally in one
   module; this module generalises it so every dispatch-context git call can
   enforce the same guarantee.)
2. A dispatch-context git call given a target path that resolves to the main
   checkout rather than to the dispatch's own worktree.

The main-checkout / linked-worktree distinction is the gitdir shape: in a
linked worktree ``.git`` is a FILE pointing at ``<common-dir>/worktrees/<name>``;
in the main checkout ``.git`` is a DIRECTORY.  ``git rev-parse --show-toplevel``
on any path inside either resolves to that checkout's own top-level, and the
``.git`` shape at that top-level classifies it.

This module is fail-open for everything except the exact refusal it is built
for: an unresolvable path, a non-git path, or a path inside a linked worktree
never triggers ``DispatchTargetsMainCheckoutError``. Only a path that provably
resolves to the MAIN checkout of a git repo, while a dispatch context is
active, refuses.

``VNX_GIT_TARGET_GUARD=0`` (or ``off``/``false``/``no``) disables the guard for
the current process. The test suite sets this so tmp repos (which are
structurally main checkouts of their own repo) are not refused when a dispatch
worker runs the suite; the guard's own tests re-enable it explicitly. A real
dispatch lane never sets the toggle, so the guard stays armed there.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class DispatchTargetsMainCheckoutError(RuntimeError):
    """Raised when a dispatch-context git operation would target the main checkout.

    A dispatch must not move the operator's checkout. Callers MUST treat this
    as a hard failure of the operation being guarded — never catch-and-continue
    with the main checkout as the git target.
    """


_DISPATCH_ID_ENV_VARS = ("VNX_CURRENT_DISPATCH_ID", "VNX_DISPATCH_ID")


def is_dispatch_context() -> bool:
    """True when the current process is a dispatch worker/lane.

    Detected from the env vars the lanes export at spawn time:
    ``VNX_CURRENT_DISPATCH_ID`` (tmux pane env + subprocess adapter) and
    ``VNX_DISPATCH_ID`` (tmux launch-line prefix). Outside a dispatch these
    are unset, and the guard is a no-op so operator/tooling git operations on
    the main checkout keep working.
    """
    return any(
        (os.environ.get(var) or "").strip() for var in _DISPATCH_ID_ENV_VARS
    )


def guard_enabled() -> bool:
    """False when ``VNX_GIT_TARGET_GUARD`` explicitly disables the guard.

    The guard functions consult this in addition to ``is_dispatch_context``.
    The test suite sets ``VNX_GIT_TARGET_GUARD=0`` so isolated tmp repos (which
    are structurally main checkouts of their own repo) are not refused when the
    suite runs inside a dispatch worker; the guard's own tests set it back to 1.
    """
    return (os.environ.get("VNX_GIT_TARGET_GUARD", "") or "").strip().lower() not in (
        "0", "off", "false", "no",
    )


def resolves_to_main_checkout(path: "str | Path") -> bool:
    """True when *path* resolves to the MAIN checkout of a git repo.

    ``git rev-parse --show-toplevel`` from *path* yields the checkout's own
    top-level. A linked worktree's top-level has ``.git`` as a FILE; the main
    checkout's top-level has ``.git`` as a DIRECTORY. Any path inside the main
    checkout (including a subdirectory) resolves to the main top-level and is
    therefore classified as the main checkout.

    Fail-open: a path that is not inside a git repo, or from which git cannot
    be run, returns False.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    top_level = proc.stdout.strip()
    if not top_level:
        return False
    return (Path(top_level).resolve() / ".git").is_dir()


def guard_git_target(path: "str | Path", *, dispatch_id: "str | None" = None) -> Path:
    """Guard a git target path against the main checkout in a dispatch context.

    When a dispatch context is active (``VNX_CURRENT_DISPATCH_ID`` /
    ``VNX_DISPATCH_ID`` set, or *dispatch_id* passed) and *path* resolves to
    the main checkout of a git repo, raises
    ``DispatchTargetsMainCheckoutError``. Otherwise returns the resolved path.

    Outside a dispatch context this is a no-op: the operator may legitimately
    run git against the main checkout.
    """
    resolved = Path(path).resolve()
    active_id = dispatch_id or (os.environ.get("VNX_CURRENT_DISPATCH_ID") or "").strip() \
        or (os.environ.get("VNX_DISPATCH_ID") or "").strip()
    if not active_id or not guard_enabled():
        return resolved
    if resolves_to_main_checkout(resolved):
        raise DispatchTargetsMainCheckoutError(
            f"dispatch {active_id!r}: git target {resolved} resolves to the MAIN "
            "checkout, not a dispatch worktree. A dispatch must operate inside its "
            "own ephemeral worktree; operating on the main checkout would move the "
            "operator's HEAD onto a PR branch. Refusing (OI-975)."
        )
    return resolved


def guard_git_cwd(cwd: "str | Path | None" = None, *, dispatch_id: "str | None" = None) -> Path:
    """Guard the cwd for a dispatch-context git call.

    A git call with no explicit cwd inherits the dispatcher process's own
    working directory — the main checkout — and would operate on the operator's
    checkout instead of the dispatch worktree (the OI-1008 pattern). In a
    dispatch context with no explicit cwd this raises; with an explicit cwd it
    delegates to :func:`guard_git_target`.
    """
    if cwd is None:
        active_id = dispatch_id or (os.environ.get("VNX_CURRENT_DISPATCH_ID") or "").strip() \
            or (os.environ.get("VNX_DISPATCH_ID") or "").strip()
        if active_id and guard_enabled():
            raise DispatchTargetsMainCheckoutError(
                f"dispatch {active_id!r}: git call has no explicit cwd — it would "
                "inherit the main checkout as its target. Pass the dispatch "
                "worktree path explicitly (OI-975/OI-1008). Refusing."
            )
        return Path.cwd().resolve()
    return guard_git_target(cwd, dispatch_id=dispatch_id)
