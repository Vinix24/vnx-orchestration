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

1b. **completed-dispatch tmux sessions** (OI-1353) — a session whose worker
   pane has a LIVE process (``pane_dead`` == 0, PID alive) is never touched by
   the kind-1 dead-pane check above, even when its dispatch finished days ago
   with an empty prompt sitting on it — five such zombies filling the
   ``VNX_TMUX_MAX_CONCURRENT`` slots is exactly what silently starved every
   new dispatch on 19-08. This is a SEPARATE, stricter conjunction — every
   condition below must hold; process-liveness or session-age alone is never
   sufficient:
     (a) the name matches the canonical dispatch-session pattern (``_SESSION_RE``);
     (b) the dispatch id is KNOWN to THIS project — present in the project's
         own ``dispatch_register.ndjson`` (any event). A tmux session listing
         is account-wide (one shared tmux server across every project on the
         machine), so this is what keeps a sweep invoked for project X from
         ever judging — let alone killing — a live session that belongs to
         project Y;
     (c) the dispatch is provably COMPLETED — a ``dispatch_completed`` register
         event, or a terminal (success/failure) receipt in
         ``t0_receipts.ndjson`` per ``event_outcome_semantics.classify_event_outcome``;
     (d) its worktree (kind 2, below) is already gone — a surviving worktree
         means the wrapper may still be about to reap it;
     (e) its pane does not classify as ``working`` or ``awaiting_permission``
         (``worker_pane_classifier.classify_worker_pane``) — both are
         RECOVERABLE states (OI-863) and must never be swept;
     (f) it is not the invoking dispatch (the same ``protected`` fence as
         kind 1's dead-pane check).
   Any condition that cannot be measured (register/receipts unreadable, pane
   capture fails) fails OPEN — the session is left alone, never guessed dead.
   Recorded under ``tmux_completed_orphans_killed`` / ``_preserved``, each
   entry carrying the ground it was decided on.

2. **git worktrees** ``<repo>/.vnx-data/worktrees/dispatch-<dispatch_id>`` — the
   same lane's ephemeral trees. An orphan is a worktree whose tmux session is
   gone (the session is the worktree's lifecycle authority: only its teardown
   reaps the tree, so a surviving session means the wrapper may still be about
   to reap it). Cleanup REUSES the exact in-process teardown path
   (``tmux_worktree.classify_path`` + ``reap``): a DIRTY worktree is MARKED
   (``git worktree lock`` with a reason) and NEVER deleted; clean/committed/
   pushed are removed exactly as teardown would, including process-group and
   leftover-process cleanup. Uncommitted work is never silently deleted. The
   session listing that decides which worktrees still have a live session is
   itself tri-state (OI-1286): a real empty listing (tmux running, no server
   because zero sessions exist — rc=1 with either "no server running" in
   stderr, or a connect failure against a socket that does not exist on disk,
   e.g. tmux 3.5a's "error connecting to <path> (No such file or
   directory)") is read as "zero sessions" and the sweep proceeds; an
   UNMEASURABLE listing (tmux not installed, the runner raised, a connect
   failure against a socket that DOES exist, or an unrecognized non-zero
   exit) is never read as "zero sessions" — it records an error and skips
   the worktree reap for this entire run instead of guessing.

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
    The tmux session LISTING itself is fail-open too: when it cannot be taken
    at all, no worktree is reaped this run, not just the ones whose id happens
    to appear in a (possibly empty-because-broken) listing.
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
from event_outcome_semantics import classify_event_outcome  # noqa: E402
from worker_pane_classifier import (  # noqa: E402
    STATE_AWAITING_PERMISSION,
    STATE_WORKING,
    classify_worker_pane,
)

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
    # kind 1b — completed-dispatch sessions whose pane still has a live
    # process (OI-1353). Each entry: {"session", "dispatch_id", "reason"} —
    # ``reason`` is the ground the conjunction was decided on (see module
    # docstring), so a dry-run shows WHICH condition a session was killed or
    # spared on, never a bare verdict.
    tmux_completed_orphans_killed: list = field(default_factory=list)
    tmux_completed_orphans_preserved: list = field(default_factory=list)
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
            "tmux_completed_orphans_killed": list(self.tmux_completed_orphans_killed),
            "tmux_completed_orphans_preserved": list(self.tmux_completed_orphans_preserved),
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


def _real_list_sessions() -> "list[str] | None":
    """List tmux session names; tri-state on failure (OI-1286).

    Returns the real listing on success. On a non-zero exit, two stderr
    shapes both mean "no server is listening on this socket, so there are
    genuinely zero sessions" and return ``[]``:

      * the classic phrasing, "no server running" (older/other tmux builds);
      * a connect failure against a socket that does not exist on disk —
        tmux 3.5a on macOS emits ``error connecting to <path> (No such file
        or directory)`` for this case instead of the classic phrase (that is
        exactly the OI-1286 scenario: a restarted host, or a worker using a
        different ``TMUX_TMPDIR``, where no socket was ever created).

    Every other non-zero outcome (tmux not installed, the runner raising
    OSError/TimeoutExpired, a connect failure against a socket that DOES
    exist — e.g. a permission error or a protocol mismatch — or any other
    unrecognized rc) was never actually measured and returns ``None``.
    Callers must not collapse ``None`` into ``[]``: that is exactly the
    "cannot measure" == "dead" bug this fixes. The "no such file" check is
    what keeps that distinction real: a missing socket is a measurement, a
    socket you can't reach is not.
    """
    rc, out, err = _run_tmux(["list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        return [line.strip() for line in out.splitlines() if line.strip()]
    err_lower = (err or "").lower()
    if "no server running" in err_lower:
        return []
    if "error connecting to" in err_lower and "no such file or directory" in err_lower:
        return []
    return None


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


def _real_capture_pane(session: str) -> "str | None":
    """Capture a session's worker pane text; ``None`` when unmeasurable.

    Mirrors ``_real_probe_liveness``'s pane targeting (``session:0.0``). A
    non-zero exit (session gone mid-check, tmux error) is "cannot measure" —
    never collapsed into an empty string, which ``classify_worker_pane``
    would read as a provably-dead (empty) pane.
    """
    rc, out, _ = _run_tmux(["capture-pane", "-p", "-t", f"{session}:0.0"])
    if rc != 0:
        return None
    return out


# ---------------------------------------------------------------------------
# Kind 1b — completed-dispatch orphan conjunction (OI-1353)
# ---------------------------------------------------------------------------

def _evaluate_completed_orphan(
    session: str,
    dispatch_id: str,
    *,
    worktrees_dir: Path,
    register_known_ids: "set[str] | None",
    register_completed_ids: "set[str] | None",
    receipts_index: "dict[str, list] | None",
    capture_pane: Callable[[str], "str | None"],
) -> "tuple[bool, str]":
    """Evaluate the completed-dispatch/still-alive-pane orphan conjunction.

    Every condition is REQUIRED — see the module docstring's kind-1b list for
    the full (a)-(f) rationale. Returns ``(is_orphan, reason)``: ``reason`` is
    always populated, on BOTH branches, so the caller can report the exact
    ground a session was killed or spared on. Fail-open throughout: any
    condition this function cannot measure returns ``(False, "..._unmeasurable")``
    — 'cannot measure' is never read as 'orphaned'.

    (a) is enforced by the caller (only ``_SESSION_RE``-matching names reach
    here) and (f) by the caller's ``protected`` fence — neither is re-checked.
    """
    # (b) the dispatch id must be known to THIS project's own register.
    if register_known_ids is None:
        return False, "register_unmeasurable"
    if dispatch_id not in register_known_ids:
        return False, "unknown_project"

    # (c) the dispatch must be provably COMPLETED: a dispatch_completed
    # register event, or a terminal (success/failure) receipt.
    evidence: "str | None" = None
    if register_completed_ids is not None and dispatch_id in register_completed_ids:
        evidence = "register:dispatch_completed"
    if evidence is None:
        if receipts_index is None:
            return False, "receipts_unmeasurable"
        for rec in receipts_index.get(dispatch_id) or []:
            outcome = classify_event_outcome(rec.get("event_type"), rec.get("status"))
            if outcome is not None:
                evidence = f"receipt:{outcome}"
                break
    if evidence is None:
        return False, "not_completed"

    # (d) the dispatch's worktree must already be gone.
    if (worktrees_dir / f"dispatch-{dispatch_id}").exists():
        return False, "worktree_still_exists"

    # (e) the pane must not be actively working or awaiting a permission
    # prompt — both are RECOVERABLE states (OI-863), never orphans.
    try:
        pane_text = capture_pane(session)
    except Exception:
        return False, "pane_unmeasurable"
    if pane_text is None:
        return False, "pane_unmeasurable"
    pane_state = classify_worker_pane(pane_text)
    if pane_state.state in (STATE_WORKING, STATE_AWAITING_PERMISSION):
        return False, f"pane_{pane_state.state}"

    return True, f"{evidence};pane={pane_state.state}"


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
    list_sessions: Optional[Callable[[], "list[str] | None"]] = None,
    probe_liveness: Optional[Callable[[str], "bool | None"]] = None,
    kill_session: Optional[Callable[[str], bool]] = None,
    pid_alive: Optional[Callable[[Optional[int]], bool]] = None,
    capture_pane: Optional[Callable[[str], "str | None"]] = None,
) -> OrphanSweepResult:
    """Clean/mark orphaned teardown leftovers across the three kinds.

    Args:
        repo_root:          Main repo root whose ``.vnx-data/worktrees/`` holds
                            the dispatch worktrees (defaults to the resolved
                            project root).
        data_dir:           ``.vnx-data`` directory for the register/receipts
                            (kind 1b) and active-manifest (kind 3) reads.
                            When omitted (together with ``state_dir``), this
                            is resolved via the fabric's canonical resolver
                            (``vnx_paths.resolve_paths()["VNX_DATA_DIR"]``) —
                            almost always the CENTRAL store
                            (``~/.vnx-data/<project_id>``), never a
                            repo-local ``.vnx-data/``. Worktrees (kind 2)
                            live under ``repo_root`` regardless of this value
                            — ``worktrees_dir`` below is always derived from
                            ``repo_root``, never from ``data_dir``.
        state_dir:          Runtime state dir for the register/receipts reads
                            and crash-recovery. An explicit value always wins.
                            When omitted together with ``data_dir``, resolved
                            via the same fabric resolver
                            (``vnx_paths.resolve_paths()["VNX_STATE_DIR"]``).
                            When only ``data_dir`` is given explicitly,
                            defaults to ``data_dir / "state"`` (unchanged
                            operator/test override path).
        project_id:         Project id for the lease PID lookup.
        current_dispatch_id: Dispatch id to fence off — never touched by any
                            kind, even if its liveness signal reads dead.
                            Defaults to ``$VNX_CURRENT_DISPATCH_ID``.
        max_orphans:        Flood cap for the active-manifest kind.
        dry_run:            Classify everything; mutate nothing.
        list_sessions / probe_liveness / kill_session / pid_alive / capture_pane:
                            Injectable backends (defaults to real tmux/os).
                            Tests override them; production never does.
                            ``list_sessions`` returning ``None`` means the
                            listing was not measurable this run (OI-1286):
                            kind-1 sees no sessions and kind-2 reaps nothing,
                            with an error recorded in the result instead of
                            treating "cannot measure" as "zero sessions".
                            ``capture_pane`` feeds the kind-1b completed-
                            dispatch conjunction (OI-1353) — returning
                            ``None`` means the pane text was not measurable.

    Returns:
        :class:`OrphanSweepResult` — per-object failures are recorded in
        ``errors`` and never abort the run.
    """
    import crash_recovery_sweep

    root = _resolve_repo_root(repo_root)
    data_dir_explicit = data_dir is not None
    central_resolve_error: "str | None" = None
    if not data_dir_explicit:
        # Neither data_dir nor (necessarily) state_dir was given — the CLI's
        # real no-flags invocation. Resolve BOTH via the fabric's canonical
        # resolver so the register/receipts/active-manifest reads land on the
        # CENTRAL store, never a repo-local `.vnx-data/` that may not exist
        # or may simply never have heard of this project's dispatches.
        data_dir, central_state_dir, central_resolve_error = _resolve_central_paths(root)
        if state_dir is None:
            state_dir = central_state_dir
    elif state_dir is None:
        # data_dir given explicitly, state_dir not — derive under it
        # (unchanged operator/test override path; explicit data_dir always
        # wins over central resolution).
        state_dir = data_dir / "state"
    pid_fn = pid_alive if pid_alive is not None else crash_recovery_sweep.is_pid_alive
    list_fn = list_sessions if list_sessions is not None else _real_list_sessions
    probe_fn = probe_liveness if probe_liveness is not None else (
        lambda s: _real_probe_liveness(s, pid_fn)
    )
    kill_fn = kill_session if kill_session is not None else _real_kill_session
    capture_fn = capture_pane if capture_pane is not None else _real_capture_pane

    if current_dispatch_id is None:
        current_dispatch_id = os.environ.get("VNX_CURRENT_DISPATCH_ID", "").strip() or None

    result = OrphanSweepResult(dry_run=dry_run)
    if central_resolve_error:
        # The central-store resolver failed and a fallback path was used
        # instead (see _resolve_central_paths). That fallback's register may
        # be a repo-local store that has never heard of this project's
        # dispatches — a resolver failure is a MEASUREMENT FAILURE and must
        # never be conflated with "this project's register genuinely names
        # zero dispatches" below.
        result.errors.append({"kind": "data_dir_resolution", "error": central_resolve_error})
        logger.warning("orphan_sweep: %s", central_resolve_error)
    protected: set[str] = {current_dispatch_id} if current_dispatch_id else set()
    worktrees_dir = root / ".vnx-data" / "worktrees"

    # ── kind 1b evidence, preloaded once (bounded — one file read each) ─────
    # register_known_ids / register_completed_ids: None means the register
    # itself could not be read this run (defensive — dispatch_register.
    # read_events is already documented to degrade to an empty list rather
    # than raise, so this branch is a belt-and-braces guard, not the expected
    # path). An empty (non-None) result is a real measurement — "this
    # project's register currently names zero dispatches" — PROVIDED state_dir
    # itself resolved correctly. When `central_resolve_error` is set above,
    # an empty result here is read from a fallback store instead and must be
    # cross-checked against that error, never trusted on its own as "zero
    # dispatches known" (this is exactly how the repo-local, never-populated
    # state directory next to the checkout used to read as a silent, valid
    # zero — OI-1353 follow-up).
    try:
        import dispatch_register
        register_events = dispatch_register.read_events(state_dir=state_dir)
    except Exception as exc:
        register_events = None
        result.errors.append({
            "kind": "completed_check",
            "error": f"dispatch_register read failed: {exc}",
        })
    if register_events is None:
        register_known_ids: "set[str] | None" = None
        register_completed_ids: "set[str] | None" = None
    else:
        register_known_ids = {
            ev.get("dispatch_id") for ev in register_events if ev.get("dispatch_id")
        }
        register_completed_ids = {
            ev.get("dispatch_id") for ev in register_events
            if ev.get("event") == "dispatch_completed" and ev.get("dispatch_id")
        }

    try:
        import dispatch_outcome_classifier
        receipts_index: "dict[str, list] | None" = (
            dispatch_outcome_classifier.load_receipts_index(state_dir)
        )
    except Exception as exc:
        receipts_index = None
        result.errors.append({
            "kind": "completed_check",
            "error": f"t0_receipts.ndjson read failed: {exc}",
        })

    # ── kind 1: dead-pane tmux sessions ──────────────────────────────────────
    # ``surviving_ids`` is the set of dispatch ids whose worktree is still
    # "live" for kind 2: its session is alive/unknown, OR a dead session's kill
    # failed (so it still exists and its wrapper may still be about to reap),
    # OR it is the protected invoking dispatch.
    surviving_ids: set[str] = set(protected)

    sessions = list_fn()
    listing_unmeasurable = sessions is None
    if listing_unmeasurable:
        sessions = []
        result.errors.append({
            "kind": "tmux",
            "error": (
                "tmux session listing was not measurable this run (tmux "
                "missing, the runner raised, or an unrecognized non-zero "
                "exit) — worktree reap skipped entirely so 'cannot measure' "
                "is never read as 'zero sessions'"
            ),
        })
        logger.warning(
            "orphan_sweep: tmux session listing unmeasurable — skipping "
            "worktree reap this run (fail-open)"
        )

    for name in sorted(sessions):
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

        if verdict is True:
            # Provably alive: a candidate for the kind-1b completed-dispatch
            # conjunction (OI-1353) — never evaluated for verdict is None
            # (liveness itself unmeasurable), since that would stack one
            # unmeasurable signal on top of another.
            is_orphan, reason = _evaluate_completed_orphan(
                name, sid,
                worktrees_dir=worktrees_dir,
                register_known_ids=register_known_ids,
                register_completed_ids=register_completed_ids,
                receipts_index=receipts_index,
                capture_pane=capture_fn,
            )
            entry = {"session": name, "dispatch_id": sid, "reason": reason}
            if is_orphan:
                if dry_run:
                    result.tmux_completed_orphans_killed.append(entry)
                    surviving_ids.add(sid)  # session would still exist during dry-run
                    logger.info(
                        "orphan_sweep: DRY-RUN would kill completed-orphan tmux "
                        "session %s (%s)", name, reason,
                    )
                    continue
                try:
                    killed = kill_fn(name)
                except Exception as exc:
                    killed = False
                    result.errors.append(
                        {"kind": "tmux_completed", "session": name, "error": str(exc)}
                    )
                if killed:
                    result.tmux_completed_orphans_killed.append(entry)
                    logger.info(
                        "orphan_sweep: killed completed-orphan tmux session %s (%s)",
                        name, reason,
                    )
                    continue
                surviving_ids.add(sid)  # still exists -> keep its worktree
                result.tmux_skipped_alive.append(name)
                result.tmux_completed_orphans_preserved.append(
                    {**entry, "reason": "kill_failed"}
                )
                logger.warning(
                    "orphan_sweep: kill failed for completed-orphan %s; leaving session",
                    name,
                )
                continue
            result.tmux_completed_orphans_preserved.append(entry)

        if verdict is not False:
            # Alive (True, conjunction unmet) or unmeasurable (None): never
            # kill what we cannot prove is dead.
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
            if listing_unmeasurable or wid in surviving_ids:
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
        "orphan_sweep: complete — tmux killed=%d skipped=%d protected=%d "
        "completed_orphans_killed=%d completed_orphans_preserved=%d; "
        "worktrees removed=%d preserved=%d skipped_live=%d; active recovered=%d "
        "scanned=%d capped=%s; dry_run=%s; errors=%d",
        len(result.tmux_killed), len(result.tmux_skipped_alive),
        len(result.tmux_skipped_protected),
        len(result.tmux_completed_orphans_killed),
        len(result.tmux_completed_orphans_preserved),
        len(result.worktrees_removed),
        len(result.worktrees_preserved), len(result.worktrees_skipped_live),
        len(result.active_recovered), result.active_scanned, result.active_capped,
        result.dry_run, len(result.errors),
    )
    return result


def _resolve_central_paths(repo_root: Path) -> "tuple[Path, Path, str | None]":
    """Resolve the register/receipts/active-manifest store via the fabric's
    canonical path resolver (mirrors ``dispatch_register._register_path``'s
    own resolution chain), NOT this repo's own ``.vnx-data/``.

    The kind-1b conjunction reads ``dispatch_register.ndjson`` and
    ``t0_receipts.ndjson``, and kind 3 (delegated to ``crash_recovery_sweep``)
    reads ``dispatches/active/`` — all three live in the CENTRAL store
    (``vnx_paths.resolve_paths()["VNX_DATA_DIR"]``, typically
    ``~/.vnx-data/<project_id>``), never in a repo-local ``.vnx-data/``.
    That repo-local directory holds only this repo's own dispatch
    WORKTREES — see ``worktrees_dir`` in :func:`sweep`, which is deliberately
    derived from ``repo_root`` directly and never passes through here.

    Measured on main 4b7d7b2f (OI-1353 follow-up): invoking this module
    without ``--state-dir`` read a repo-local, EMPTY register and silently
    treated every completed-dispatch session as ``unknown_project`` — the
    fabric's own resolver, run against the same repo with no flags, found
    1651 register events at the correct central path.

    Returns ``(data_dir, state_dir, error)``. ``error`` is ``None`` on a
    successful resolve. A resolver failure (no ``vnx_paths`` importable, or
    it raises) is a genuine MEASUREMENT FAILURE — the caller records it in
    ``errors`` rather than silently trusting the repo-relative fallback below
    as though it were a valid "zero dispatches known".
    """
    try:
        from vnx_paths import resolve_paths
        paths = resolve_paths()
        return Path(paths["VNX_DATA_DIR"]), Path(paths["VNX_STATE_DIR"]), None
    except Exception as exc:
        env = os.environ.get("VNX_DATA_DIR", "").strip()
        if env:
            fallback = Path(env).expanduser()
            chain = "$VNX_DATA_DIR"
        else:
            fallback = repo_root / ".vnx-data"
            chain = "repo-relative .vnx-data"
        return (
            fallback,
            fallback / "state",
            f"central path resolver (vnx_paths.resolve_paths) unavailable "
            f"({exc}); fell back to {chain} — register/receipts/"
            "active-manifest reads below may be scoped to the wrong store",
        )
