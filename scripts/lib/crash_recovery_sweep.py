"""crash_recovery_sweep — flood-safe recovery for orphaned active dispatches.

Background
----------
``subprocess_dispatch`` runs the retry / final-failure recovery loop
(``subprocess_dispatch_internals/recovery.py``) **in-process**. That loop only
fires while the orchestrating wrapper process is alive: on the success path it
writes a "done" receipt and promotes the manifest to ``completed/``; on the
budget-exhausted failure path it writes a "failed" receipt and promotes the
manifest to ``dead_letter/``.

When the wrapper process is killed mid-dispatch (a terminal/iTerm crash, an
OOM-kill, a ``kill -9`` of the dispatcher), none of that runs. The
``.vnx-data/dispatches/active/<id>/manifest.json`` entry is left behind with no
receipt and no ``dead_letter`` promotion. T0 is never told the dispatch died,
so the active bucket slowly fills with orphans (the SEOcrawler April backlog was
exactly this class of orphan, not an in-process timeout).

PID-based death detection was impossible until PR #636 added the
``terminal_leases.worker_pid`` column, which records the orchestrator PID
(``os.getpid()`` in ``subprocess_dispatch``). This sweep is the consumer of that
column: it finds active orphans whose orchestrator PID is no longer alive and
finishes the recovery the dead wrapper could not.

Flood safety
------------
A receipt flood has happened before when a backlog of orphans was reprocessed in
bulk. This module is built to make that impossible by default:

* **Opt-in only.** Nothing calls this on the dispatch hot path. It runs only on
  manual invocation (``scripts/crash_recovery_sweep.py``) or a deliberately
  enabled supervisor tick.
* **Capped.** At most ``max_orphans`` orphans are processed per run
  (default :data:`DEFAULT_MAX_ORPHANS`). The cap counts *processed* orphans, so
  dead-PID orphans drain a few at a time instead of all at once.
* **Idempotent.** Processing promotes the manifest out of ``active/`` and the
  receipt writer dedups by content hash, so a second run over the same orphan is
  a clean no-op.
* **Dry-run.** ``--dry-run`` reports exactly what would happen and writes
  nothing — no receipt, no manifest move, no lease/state mutation.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Conservative default. The cap counts *processed* (dead-PID) orphans per run so
# a large backlog drains gradually instead of flooding T0 with receipts.
DEFAULT_MAX_ORPHANS = 10

# failure_reason stamped on the recovery receipt so T0 can distinguish a
# crash-recovered orphan from a budget-exhausted in-process failure.
ORCHESTRATOR_DEATH_REASON = "orchestrator_death"


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def is_pid_alive(pid: Optional[int]) -> bool:
    """Return True iff ``pid`` is a live process this user can signal.

    Uses ``os.kill(pid, 0)`` (no signal delivered; permission/existence probe
    only). A ``None`` or non-positive PID is treated as *not alive* so an orphan
    with no recorded PID is eligible for recovery rather than silently skipped.

    Semantics:
      * ``ProcessLookupError`` -> the PID does not exist -> dead.
      * ``PermissionError``    -> the PID exists but is owned by another user ->
        alive (cannot prove death, so fail safe by NOT recovering).
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not signal it. Treat as alive: we must never
        # recover a dispatch whose orchestrator might still be running.
        return True
    except OSError as exc:
        logger.warning("is_pid_alive: unexpected OSError for pid=%s: %s", pid, exc)
        # Fail safe: assume alive so we do not recover a possibly-live dispatch.
        return True
    return True


# ---------------------------------------------------------------------------
# Orphan discovery
# ---------------------------------------------------------------------------

@dataclass
class Orphan:
    """An active dispatch with a recoverable manifest."""

    dispatch_id: str
    manifest_path: Path
    terminal_id: Optional[str] = None
    worker_pid: Optional[int] = None
    pid_source: str = "none"  # "lease" | "manifest" | "none"


@dataclass
class SweepResult:
    """Outcome of a single sweep run."""

    scanned: int = 0
    recovered: list = field(default_factory=list)   # dispatch_ids promoted to dead_letter
    skipped_alive: list = field(default_factory=list)  # dispatch_ids with a live orchestrator
    skipped_no_pid: list = field(default_factory=list)  # eligible but no PID resolved (still recovered)
    capped: bool = False
    dry_run: bool = False
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "recovered": list(self.recovered),
            "recovered_count": len(self.recovered),
            "skipped_alive": list(self.skipped_alive),
            "skipped_no_pid": list(self.skipped_no_pid),
            "capped": self.capped,
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


def _active_dir(data_dir: Path) -> Path:
    return data_dir / "dispatches" / "active"


def _read_manifest_terminal(manifest_path: Path) -> tuple[Optional[str], Optional[int]]:
    """Read (terminal_id, worker_pid) from a manifest; missing keys -> None.

    ``worker_pid`` is read defensively: older manifests never recorded a PID, so
    the lease table is the primary source and the manifest is a fallback only.
    """
    import json
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed/partial manifest from a crashed write
        logger.warning("crash_recovery: unreadable manifest %s: %s", manifest_path, exc)
        return None, None
    terminal = data.get("terminal") or data.get("terminal_id")
    raw_pid = data.get("worker_pid")
    pid: Optional[int] = None
    if raw_pid is not None:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None
    return (terminal if isinstance(terminal, str) else None), pid


def _lookup_lease_pid(
    state_dir: Path, terminal_id: str, project_id: str,
) -> Optional[int]:
    """Read ``terminal_leases.worker_pid`` for a terminal (PR #636 column).

    Returns None when the DB, table, column, or row is absent — all of which
    fall back to the manifest PID or, ultimately, to treating the orphan as
    having no resolvable PID (still eligible for recovery).
    """
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
    except sqlite3.Error as exc:
        logger.warning("crash_recovery: cannot open runtime_coordination.db: %s", exc)
        return None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(terminal_leases)")}
        if "worker_pid" not in cols:
            # Predates PR #636 self-heal; cannot resolve a lease PID here.
            return None
        row = conn.execute(
            "SELECT worker_pid FROM terminal_leases "
            "WHERE terminal_id = ? AND project_id = ?",
            (terminal_id, project_id),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("crash_recovery: lease PID query failed: %s", exc)
        return None
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def discover_orphans(data_dir: Path, state_dir: Path, project_id: str) -> list[Orphan]:
    """Scan ``dispatches/active/`` for entries carrying a ``manifest.json``.

    An orphan is any ``active/<dispatch_id>/manifest.json``. Its PID is resolved
    from the lease table first (PR #636 ``worker_pid``), then the manifest as a
    fallback. Discovery never mutates state.
    """
    active = _active_dir(data_dir)
    if not active.is_dir():
        return []
    orphans: list[Orphan] = []
    for entry in sorted(active.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        dispatch_id = entry.name
        terminal_id, manifest_pid = _read_manifest_terminal(manifest_path)
        pid: Optional[int] = None
        pid_source = "none"
        if terminal_id:
            lease_pid = _lookup_lease_pid(state_dir, terminal_id, project_id)
            if lease_pid is not None:
                pid, pid_source = lease_pid, "lease"
        if pid is None and manifest_pid is not None:
            pid, pid_source = manifest_pid, "manifest"
        orphans.append(
            Orphan(
                dispatch_id=dispatch_id,
                manifest_path=manifest_path,
                terminal_id=terminal_id,
                worker_pid=pid,
                pid_source=pid_source,
            )
        )
    return orphans


# ---------------------------------------------------------------------------
# Recovery actions
# ---------------------------------------------------------------------------

def _recover_orphan(orphan: Orphan, data_dir: Path) -> None:
    """Finish recovery for one dead-PID orphan: receipt + dead_letter + cleanup.

    Order matches the in-process final-failure path so a crash-recovered orphan
    is indistinguishable downstream from a normally-failed one (apart from the
    ``failure_reason``):

      1. Write a ``failed`` receipt (idempotent via append_receipt dedup).
      2. Promote the manifest to ``dead_letter/`` (removes the active dir).
      3. cleanup_worker_exit: release lease, transition worker state, move any
         dispatch file out of active/.
    """
    _scripts_lib = str(Path(__file__).resolve().parent)
    import sys
    if _scripts_lib not in sys.path:
        sys.path.insert(0, _scripts_lib)

    from subprocess_dispatch_internals.receipt_writer import (
        _write_receipt,
        _ensure_unified_report,
    )
    from subprocess_dispatch_internals.manifest import _promote_manifest
    from cleanup_worker_exit import cleanup_worker_exit

    terminal = orphan.terminal_id or "unknown"

    # 1. Ensure a report stub exists, then write the failure receipt. The
    # receipt writer dedups by content, so a re-run over an orphan that was
    # already recovered (manifest already gone) does not double-write.
    _ensure_unified_report(orphan.dispatch_id, terminal, "failed")
    _write_receipt(
        orphan.dispatch_id,
        terminal,
        "failed",
        failure_reason=ORCHESTRATOR_DEATH_REASON,
        manifest_path=str(orphan.manifest_path),
    )

    # 2. Promote manifest active/ -> dead_letter/ (idempotent; removes the
    # active/<id>/ dir so the orphan is no longer rediscovered).
    _promote_manifest(orphan.dispatch_id, stage="dead_letter")

    # 3. Lease release + worker-state transition + dispatch-file move. None of
    # these raise; cleanup_worker_exit returns a CleanupResult.
    cleanup_worker_exit(
        terminal_id=terminal,
        dispatch_id=orphan.dispatch_id,
        exit_status="killed",
        state_dir=data_dir / "state",
    )


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------

def sweep(
    data_dir: Path,
    *,
    state_dir: Optional[Path] = None,
    project_id: str = "vnx-dev",
    max_orphans: int = DEFAULT_MAX_ORPHANS,
    dry_run: bool = False,
    pid_alive: "Optional[Callable[[Optional[int]], bool]]" = None,
) -> SweepResult:
    """Recover up to ``max_orphans`` dead-PID orphaned active dispatches.

    Args:
        data_dir:    ``.vnx-data`` directory (contains ``dispatches/`` and
                     ``state/``).
        state_dir:   Override for the runtime state dir; defaults to
                     ``data_dir / "state"``.
        project_id:  Project id used to scope the lease PID lookup.
        max_orphans: Maximum number of orphans to *recover* in this run. The cap
                     is the flood-safety guarantee; once reached the sweep stops
                     scanning and sets ``capped=True``.
        dry_run:     When True, classify every orphan but mutate nothing.
        pid_alive:   Injectable PID-liveness predicate (defaults to
                     :func:`is_pid_alive`). Tests override it; production never
                     does.

    Returns:
        :class:`SweepResult` — never raises on a per-orphan error (recorded in
        ``errors`` and the sweep continues).
    """
    state_dir = state_dir if state_dir is not None else (data_dir / "state")
    liveness = pid_alive if pid_alive is not None else is_pid_alive
    result = SweepResult(dry_run=dry_run)

    orphans = discover_orphans(data_dir, state_dir, project_id)
    result.scanned = len(orphans)

    for orphan in orphans:
        # Liveness is checked BEFORE the cap so that a live orphan never trips
        # the cap or stops the scan — only genuinely recoverable (dead-PID)
        # orphans consume the budget.
        if liveness(orphan.worker_pid):
            result.skipped_alive.append(orphan.dispatch_id)
            logger.info(
                "crash_recovery: SKIP %s — orchestrator pid=%s (%s) still alive",
                orphan.dispatch_id, orphan.worker_pid, orphan.pid_source,
            )
            continue

        if len(result.recovered) >= max_orphans:
            result.capped = True
            logger.info(
                "crash_recovery: cap reached (%d), stopping; recoverable orphan %s "
                "left for next run",
                max_orphans, orphan.dispatch_id,
            )
            break

        if orphan.worker_pid is None:
            # No PID resolvable from lease or manifest. The orchestrator cannot
            # be proven alive, so the orphan is eligible — but flag it so the
            # operator can see the weaker evidence.
            result.skipped_no_pid.append(orphan.dispatch_id)
            logger.warning(
                "crash_recovery: %s has no resolvable orchestrator PID "
                "(lease+manifest both absent) — recovering on absence",
                orphan.dispatch_id,
            )

        if dry_run:
            logger.info(
                "crash_recovery: DRY-RUN would recover %s "
                "(terminal=%s pid=%s source=%s) -> dead_letter + failed receipt",
                orphan.dispatch_id, orphan.terminal_id,
                orphan.worker_pid, orphan.pid_source,
            )
            result.recovered.append(orphan.dispatch_id)
            continue

        try:
            _recover_orphan(orphan, data_dir)
            result.recovered.append(orphan.dispatch_id)
            logger.info(
                "crash_recovery: RECOVERED %s (terminal=%s dead pid=%s source=%s) "
                "-> dead_letter + failed receipt (%s)",
                orphan.dispatch_id, orphan.terminal_id, orphan.worker_pid,
                orphan.pid_source, ORCHESTRATOR_DEATH_REASON,
            )
        except Exception as exc:  # one bad orphan must not abort the whole run
            result.errors.append({"dispatch_id": orphan.dispatch_id, "error": str(exc)})
            logger.error(
                "crash_recovery: recovery FAILED for %s: %s", orphan.dispatch_id, exc
            )

    _emit_summary(result)
    return result


def _emit_summary(result: SweepResult) -> None:
    when = datetime.now(timezone.utc).isoformat()
    logger.info(
        "crash_recovery: sweep complete at %s — scanned=%d recovered=%d "
        "skipped_alive=%d no_pid=%d capped=%s dry_run=%s errors=%d",
        when, result.scanned, len(result.recovered), len(result.skipped_alive),
        len(result.skipped_no_pid), result.capped, result.dry_run, len(result.errors),
    )
