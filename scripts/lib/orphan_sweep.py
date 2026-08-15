"""orphan_sweep.py — idempotent sweep for teardown leftovers (OI-1192).

Teardown in VNX is IN-PROCESS: the orchestrating wrapper kills the worker's tmux
session, reaps its worktree, and promotes its ``dispatches/active/`` manifest —
only while the wrapper is still alive. A wrapper killed mid-dispatch (a terminal
crash, an OOM-kill, a ``kill -9``) leaves that teardown unrun, so three kinds of
runtime objects persist forever:

1. **tmux sessions** ``vnx-<dispatch_id>`` — the tmux-spawn lane's ephemeral
   sessions. An orphan is a session whose worker pane is PROVABLY dead
   (``#{pane_dead}`` == 1, or ``#{pane_pid}`` no longer alive). Cleanup:
   ``tmux kill-session`` (idempotent — a missing session is already gone).

2. **git worktrees** ``<repo>/.vnx-data/worktrees/dispatch-<dispatch_id>`` — the
   same lane's ephemeral trees. An orphan is a worktree whose tmux session is
   gone (the session is the worktree's lifecycle authority: only its teardown
   reaps the tree, so a surviving session means the wrapper may still be about
   to reap it). Cleanup REUSES the exact in-process teardown path
   (``tmux_worktree.classify_path`` + ``reap``): a DIRTY worktree is MARKED
   (``git worktree lock`` with a reason) and NEVER deleted; clean/committed/
   pushed are removed exactly as teardown would, including process-group and
   leftover-process cleanup. Uncommitted work is never silently deleted.

3. **``dispatches/active/<id>/manifest.json``** — the subprocess lane's active
   manifests. Delegated to :mod:`crash_recovery_sweep`, which already recovers
   dead-orchestrator orphans (dead_letter promotion + failed receipt) with a
   flood cap, idempotency, and PID liveness.

Safety (both requirements from the dispatch):
  * **Idempotent** — every action is a no-op on a second run: kill-session on a
    missing session is a no-op; reap on a missing worktree is a no-op; the
    crash-recovery path dedups by manifest promotion + receipt content hash.
  * **Safe concurrent with a live dispatch** — every liveness probe is
    fail-OPEN ("cannot measure" is never read as "dead"), and the sweep never
    touches the dispatch that invoked it. ``VNX_CURRENT_DISPATCH_ID`` (or
    ``--current-dispatch``) fences that id off from all three kinds, because a
    live dispatch can be driven under a liveness signal this module cannot see
    (e.g. a harness that named its tmux session differently from ``vnx-<id>``).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Route every tmux subcommand through the runtime adapter's canonical runner.
# The DirectCouplingFreeze (tests/test_runtime_adapter_certification.py) pins
# direct ``subprocess``→tmux coupling to the adapter files; this module must
# delegate to ``tmux_adapter._run_tmux`` rather than run ``tmux`` itself.
from tmux_adapter import _run_tmux as _adapter_run_tmux  # noqa: E402

# The three object kinds share one dispatch-id alphabet. Both regexes pin the
# name to the exact dispatch-id charset so a stray ``vnx-...`` / ``dispatch-...``
# name that is not a VNX dispatch id is never mistaken for one.
_DISPATCH_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_SESSION_RE = re.compile(rf"^vnx-(?P<id>{_DISPATCH_ID})$")
_WORKTREE_DIR_RE = re.compile(rf"^dispatch-(?P<id>{_DISPATCH_ID})$")

# Reuse the crash-recovery flood cap: the unified sweep inherits the same
# flood-safety guarantee for the active-manifest kind.
DEFAULT_MAX_ORPHANS = 10


def _session_name(dispatch_id: str) -> str:
    """The tmux session name for a dispatch id (mirrors tmux_interactive_dispatch).

    Dispatch ids cannot contain ``.`` or ``:`` (the alphabet above), so
    ``_sanitize_session_name`` is the identity here.
    """
    return "vnx-" + dispatch_id


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class OrphanSweepResult:
    """Outcome of one unified orphan-sweep run across the three kinds."""

    dry_run: bool = False
    # kind 1 — tmux sessions
    tmux_sessions_scanned: list = field(default_factory=list)
    tmux_killed: list = field(default_factory=list)          # sessions killed
    tmux_skipped_alive: list = field(default_factory=list)   # alive / unknown / kill-failed
    tmux_skipped_protected: list = field(default_factory=list)
    # kind 2 — worktrees
    worktrees_scanned: list = field(default_factory=list)
    worktrees_removed: list = field(default_factory=list)    # reaped (clean/committed/pushed)
    worktrees_preserved: list = field(default_factory=list)  # dirty -> locked/marked
    worktrees_skipped_live: list = field(default_factory=list)
    # kind 3 — active manifests (delegated to crash_recovery_sweep)
    active_scanned: int = 0
    active_recovered: list = field(default_factory=list)
    active_skipped_alive: list = field(default_factory=list)
    active_skipped_no_pid: list = field(default_factory=list)
    active_skipped_protected: list = field(default_factory=list)
    active_capped: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "tmux_sessions_scanned": list(self.tmux_sessions_scanned),
            "tmux_killed": list(self.tmux_killed),
            "tmux_skipped_alive": list(self.tmux_skipped_alive),
            "tmux_skipped_protected": list(self.tmux_skipped_protected),
            "worktrees_scanned": list(self.worktrees_scanned),
            "worktrees_removed": list(self.worktrees_removed),
            "worktrees_preserved": list(self.worktrees_preserved),
            "worktrees_skipped_live": list(self.worktrees_skipped_live),
            "active_scanned": self.active_scanned,
            "active_recovered": list(self.active_recovered),
            "active_skipped_alive": list(self.active_skipped_alive),
            "active_skipped_no_pid": list(self.active_skipped_no_pid),
            "active_skipped_protected": list(self.active_skipped_protected),
            "active_capped": self.active_capped,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Real tmux backends (injectable in tests)
# ---------------------------------------------------------------------------

def _run_tmux(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a tmux subcommand; never raises. (rc, stdout, stderr)."""
    if shutil.which("tmux") is None:
        return (127, "", "tmux not found")
    try:
        r = _adapter_run_tmux(*args, timeout=timeout)
        return (r.returncode, r.stdout, r.stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return (1, "", str(exc))


def _real_list_sessions() -> list[str]:
    rc, out, _ = _run_tmux(["list-sessions", "-F", "#{session_name}"])
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _real_probe_liveness(session: str, pid_alive: Callable[[Optional[int]], bool]) -> "bool | None":
    """Tri-state liveness of a tmux session's worker pane.

    Mirrors ``tmux_interactive_dispatch._check_worker_liveness``'s fail-open
    contract: True = provably alive, False = provably dead, None = unknown.
    "Cannot measure" is never read as "dead".
    """
    rc, _, _ = _run_tmux(["has-session", "-t", session])
    if rc != 0:
        return False  # session gone -> provably dead
    rc, out, _ = _run_tmux(
        ["display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}\t#{pane_pid}"]
    )
    if rc != 0 or not out.strip():
        return False  # session exists but pane is gone
    parts = out.strip().split("\t")
    if len(parts) != 2:
        return None
    dead_flag, pid_str = parts[0].strip(), parts[1].strip()
    if dead_flag == "1":
        return False
    if not pid_str.isdigit() or int(pid_str) <= 0:
        return None
    return pid_alive(int(pid_str))


def _real_kill_session(session: str) -> bool:
    rc, _, _ = _run_tmux(["kill-session", "-t", session])
    return rc == 0


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------

def _resolve_repo_root(repo_root: Optional[Path]) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    try:
        from tmux_worktree import _resolve_repo_root as _tw_root
        return _tw_root(None)
    except Exception:
        return Path.cwd().resolve()


def sweep(
    repo_root: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    *,
    state_dir: Optional[Path] = None,
    project_id: str = "vnx-dev",
    current_dispatch_id: Optional[str] = None,
    max_orphans: int = DEFAULT_MAX_ORPHANS,
    dry_run: bool = False,
    list_sessions: Optional[Callable[[], list[str]]] = None,
    probe_liveness: Optional[Callable[[str], "bool | None"]] = None,
    kill_session: Optional[Callable[[str], bool]] = None,
    pid_alive: Optional[Callable[[Optional[int]], bool]] = None,
) -> OrphanSweepResult:
    """Clean/mark orphaned teardown leftovers across the three kinds.

    Args:
        repo_root:          Main repo root whose ``.vnx-data/worktrees/`` holds
                            the dispatch worktrees (defaults to the resolved
                            project root).
        data_dir:           ``.vnx-data`` directory for the active-manifest kind
                            (defaults to ``$VNX_DATA_DIR``). Worktrees live under
                            ``repo_root``; manifests live under ``data_dir``.
        state_dir:          Runtime state dir override for crash-recovery
                            (defaults to ``data_dir / "state"``).
        project_id:         Project id for the lease PID lookup.
        current_dispatch_id: Dispatch id to fence off — never touched by any
                            kind, even if its liveness signal reads dead.
                            Defaults to ``$VNX_CURRENT_DISPATCH_ID``.
        max_orphans:        Flood cap for the active-manifest kind.
        dry_run:            Classify everything; mutate nothing.
        list_sessions / probe_liveness / kill_session / pid_alive:
                            Injectable backends (defaults to real tmux/os).
                            Tests override them; production never does.

    Returns:
        :class:`OrphanSweepResult` — per-object failures are recorded in
        ``errors`` and never abort the run.
    """
    import crash_recovery_sweep

    root = _resolve_repo_root(repo_root)
    data_dir = data_dir if data_dir is not None else _default_data_dir(root)
    state_dir = state_dir if state_dir is not None else (data_dir / "state")
    pid_fn = pid_alive if pid_alive is not None else crash_recovery_sweep.is_pid_alive
    list_fn = list_sessions if list_sessions is not None else _real_list_sessions
    probe_fn = probe_liveness if probe_liveness is not None else (
        lambda s: _real_probe_liveness(s, pid_fn)
    )
    kill_fn = kill_session if kill_session is not None else _real_kill_session

    if current_dispatch_id is None:
        current_dispatch_id = os.environ.get("VNX_CURRENT_DISPATCH_ID", "").strip() or None

    result = OrphanSweepResult(dry_run=dry_run)
    protected: set[str] = {current_dispatch_id} if current_dispatch_id else set()

    # ── kind 1: dead-pane tmux sessions ──────────────────────────────────────
    # ``surviving_ids`` is the set of dispatch ids whose worktree is still
    # "live" for kind 2: its session is alive/unknown, OR a dead session's kill
    # failed (so it still exists and its wrapper may still be about to reap),
    # OR it is the protected invoking dispatch.
    surviving_ids: set[str] = set(protected)

    for name in sorted(list_fn()):
        m = _SESSION_RE.match(name)
        if not m:
            continue
        sid = m.group("id")
        result.tmux_sessions_scanned.append(name)
        if sid in protected:
            result.tmux_skipped_protected.append(name)
            logger.info("orphan_sweep: SKIP tmux %s — protected dispatch", name)
            continue
        verdict = probe_fn(name)
        if verdict is not False:
            # Alive (True) or unmeasurable (None): never kill what we cannot
            # prove is dead.
            surviving_ids.add(sid)
            result.tmux_skipped_alive.append(name)
            continue
        # Provably dead pane.
        if dry_run:
            result.tmux_killed.append(name)
            surviving_ids.add(sid)  # session would still exist during dry-run
            logger.info("orphan_sweep: DRY-RUN would kill tmux session %s", name)
            continue
        try:
            killed = kill_fn(name)
        except Exception as exc:
            killed = False
            result.errors.append({"kind": "tmux", "session": name, "error": str(exc)})
        if killed:
            result.tmux_killed.append(name)
            logger.info("orphan_sweep: killed dead tmux session %s", name)
        else:
            surviving_ids.add(sid)  # still exists -> keep its worktree
            result.tmux_skipped_alive.append(name)
            logger.warning("orphan_sweep: kill failed for %s; leaving session", name)

    # ── kind 2: worktrees whose tmux session is gone ─────────────────────────
    worktrees_dir = root / ".vnx-data" / "worktrees"
    if worktrees_dir.is_dir():
        from tmux_worktree import WorktreeHandle, classify_path, reap

        for entry in sorted(worktrees_dir.iterdir()):
            if not entry.is_dir():
                continue
            m = _WORKTREE_DIR_RE.match(entry.name)
            if not m:
                continue
            wid = m.group("id")
            result.worktrees_scanned.append(str(entry))
            if wid in surviving_ids:
                result.worktrees_skipped_live.append(str(entry))
                continue
            branch = f"dispatch/{wid}"
            try:
                classification = classify_path(
                    wt=entry, branch=branch, dispatch_id=wid, base_sha=None,
                )
            except Exception as exc:
                result.errors.append(
                    {"kind": "worktree", "path": str(entry), "error": f"classify: {exc}"}
                )
                continue
            if dry_run:
                bucket = (
                    result.worktrees_preserved if classification == "dirty"
                    else result.worktrees_removed
                )
                bucket.append(str(entry))
                logger.info(
                    "orphan_sweep: DRY-RUN worktree %s classified %s (no mutation)",
                    entry.name, classification,
                )
                continue
            try:
                handle = WorktreeHandle(
                    path=entry, branch=branch, base_sha="",
                    base_ref="origin/main", dispatch_id=wid,
                )
                rr = reap(handle, classification)
            except Exception as exc:
                result.errors.append({"kind": "worktree", "path": str(entry), "error": str(exc)})
                continue
            if rr.removed:
                result.worktrees_removed.append(str(entry))
                logger.info("orphan_sweep: reaped orphan worktree %s", entry.name)
            elif rr.preserved_path is not None:
                result.worktrees_preserved.append(str(entry))
                logger.info(
                    "orphan_sweep: preserved (marked) dirty worktree %s", entry.name,
                )
            for e in rr.errors:
                result.errors.append({"kind": "worktree", "path": str(entry), "error": e})

    # ── kind 3: active manifests (delegate to crash_recovery_sweep) ──────────
    active = crash_recovery_sweep.sweep(
        data_dir,
        state_dir=state_dir,
        project_id=project_id,
        max_orphans=max_orphans,
        dry_run=dry_run,
        pid_alive=pid_fn,
        exclude_ids=protected,
    )
    result.active_scanned = active.scanned
    result.active_recovered = list(active.recovered)
    result.active_skipped_alive = list(active.skipped_alive)
    result.active_skipped_no_pid = list(active.skipped_no_pid)
    result.active_skipped_protected = list(active.skipped_protected)
    result.active_capped = active.capped
    for e in active.errors:
        result.errors.append({"kind": "active", **e})

    logger.info(
        "orphan_sweep: complete — tmux killed=%d skipped=%d protected=%d; "
        "worktrees removed=%d preserved=%d skipped_live=%d; active recovered=%d "
        "scanned=%d capped=%s; dry_run=%s; errors=%d",
        len(result.tmux_killed), len(result.tmux_skipped_alive),
        len(result.tmux_skipped_protected), len(result.worktrees_removed),
        len(result.worktrees_preserved), len(result.worktrees_skipped_live),
        len(result.active_recovered), result.active_scanned, result.active_capped,
        result.dry_run, len(result.errors),
    )
    return result


def _default_data_dir(repo_root: Path) -> Path:
    """Resolve the ``.vnx-data`` dir for the active-manifest kind (issue #225)."""
    env = os.environ.get("VNX_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return repo_root / ".vnx-data"
