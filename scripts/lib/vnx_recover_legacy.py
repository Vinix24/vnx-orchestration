#!/usr/bin/env python3
"""VNX Recover Legacy — Python-led legacy file-based recovery cleanup.

PR-3 deliverable: migrates the legacy recovery cleanup from recover.sh
into testable Python. This handles file-based artifacts that the canonical
runtime recovery engine (vnx_recover_runtime.py) does not manage:

  1. Stale lock detection and cleanup (PID-based, age-based, orphan)
  2. Stale PID file cleanup
  3. Incomplete dispatch file recovery (move to failed/)
  4. Terminal state reset (legacy, only when runtime core is off)
  5. Unclean-shutdown marker cleanup
  6. Stale payload temp file cleanup

Design:
  - Each cleanup step returns structured results for operator review.
  - Dry-run mode reports what would be changed without side effects.
  - Exit code semantics match recover.sh for backward compatibility:
    0 = clean or recovered, non-zero = issues remain.

Governance:
  G-R2: Legacy cleanup complements runtime recovery, never replaces it.
  A-R8: All operations are idempotent.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CleanupAction:
    """A single cleanup action taken or planned."""
    step: str           # "locks", "pids", "dispatches", "terminal_state", "marker", "payloads"
    action: str         # "clear", "kill", "move", "reset", "clean"
    target: str         # What was affected
    outcome: str        # "applied", "would_apply" (dry-run), "skipped", "error"
    detail: str = ""


@dataclass
class LegacyRecoveryReport:
    """Full legacy recovery report."""
    dry_run: bool = False
    issues_found: int = 0
    issues_resolved: int = 0
    actions: List[CleanupAction] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def summary_text(self) -> str:
        if self.dry_run:
            return f"Legacy cleanup (dry-run): {self.issues_found} issue(s) found"
        if self.issues_resolved > 0:
            return f"Legacy cleanup: {self.issues_resolved} issue(s) resolved"
        return "Legacy cleanup: session state is clean"


# ---------------------------------------------------------------------------
# Process utilities
# ---------------------------------------------------------------------------

def _is_process_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_process(pid: int, graceful_timeout: float = 1.0) -> bool:
    """Attempt to kill a process gracefully (TERM), then forcefully (KILL)."""
    if not _is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return True
    time.sleep(graceful_timeout)
    if not _is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    return not _is_process_alive(pid)


def _read_file_int(path: str, default: int = 0) -> int:
    """Read an integer from a file, returning default on any error."""
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Step 1: Stale lock cleanup
# ---------------------------------------------------------------------------

def cleanup_stale_locks(
    locks_dir: str,
    pids_dir: str,
    max_age: int = 3600,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Detect and clear stale locks.

    A lock is stale if any of:
      - PID is not running (process_dead)
      - Lock age exceeds max_age (expired)
      - No PID file exists (orphan_lock)
    """
    actions: List[CleanupAction] = []
    locks_path = Path(locks_dir)
    if not locks_path.is_dir():
        return actions

    for lock_dir in sorted(locks_path.glob("*.lock")):
        if not lock_dir.is_dir():
            continue

        lock_name = lock_dir.stem
        pid_file = lock_dir / "pid"
        lock_pid = _read_file_int(str(pid_file)) if pid_file.is_file() else 0

        stale_reason = ""

        # Check 1: PID not running
        if lock_pid > 0 and not _is_process_alive(lock_pid):
            stale_reason = "process_dead"

        # Check 2: Lock age exceeds max
        if not stale_reason:
            heartbeat = lock_dir / "heartbeat"
            created_at = lock_dir / "created_at"
            lock_ts = 0
            if heartbeat.is_file():
                lock_ts = _read_file_int(str(heartbeat))
            if lock_ts == 0 and created_at.is_file():
                lock_ts = _read_file_int(str(created_at))
            if lock_ts > 0:
                age = int(time.time()) - lock_ts
                if age >= max_age:
                    stale_reason = f"expired (age={age}s, max={max_age}s)"

        # Check 3: No PID file (orphan)
        if not stale_reason and lock_pid == 0:
            stale_reason = "orphan_lock (no PID)"

        if not stale_reason:
            continue

        if dry_run:
            actions.append(CleanupAction(
                "locks", "clear", lock_name, "would_apply",
                f"{stale_reason}, PID: {lock_pid or 'none'}",
            ))
        else:
            # Kill process if still running (expired case)
            if lock_pid > 0 and _is_process_alive(lock_pid):
                _kill_process(lock_pid)

            # Remove lock dir and associated PID files
            _rmtree_safe(lock_dir)
            pids_path = Path(pids_dir)
            for suffix in (".pid", ".pid.fingerprint"):
                pid_artifact = pids_path / f"{lock_name}{suffix}"
                pid_artifact.unlink(missing_ok=True)

            actions.append(CleanupAction(
                "locks", "clear", lock_name, "applied",
                f"{stale_reason}, PID: {lock_pid or 'none'}",
            ))

    return actions


# ---------------------------------------------------------------------------
# Step 2: Stale PID file cleanup (non-aggressive)
# ---------------------------------------------------------------------------

def cleanup_stale_pids(
    pids_dir: str,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Clean up PID files whose processes are no longer running."""
    actions: List[CleanupAction] = []
    pids_path = Path(pids_dir)
    if not pids_path.is_dir():
        return actions

    for pid_file in sorted(pids_path.glob("*.pid")):
        if not pid_file.is_file():
            continue
        pid = _read_file_int(str(pid_file))
        proc_name = pid_file.stem

        if pid > 0 and not _is_process_alive(pid):
            if dry_run:
                actions.append(CleanupAction(
                    "pids", "clean", proc_name, "would_apply",
                    f"stale PID file (PID: {pid}, not running)",
                ))
            else:
                pid_file.unlink(missing_ok=True)
                fingerprint = pid_file.with_suffix(".pid.fingerprint")
                fingerprint.unlink(missing_ok=True)
                actions.append(CleanupAction(
                    "pids", "clean", proc_name, "applied",
                    f"stale PID file (PID: {pid})",
                ))

    return actions


# ---------------------------------------------------------------------------
# Step 3: Incomplete dispatch files
# ---------------------------------------------------------------------------

def cleanup_incomplete_dispatches(
    dispatch_dir: str,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Move incomplete dispatch files from active/ to failed/."""
    actions: List[CleanupAction] = []
    active_dir = Path(dispatch_dir) / "active"
    failed_dir = Path(dispatch_dir) / "failed"

    if not active_dir.is_dir():
        return actions

    for dispatch_file in sorted(active_dir.glob("*.md")):
        if not dispatch_file.is_file():
            continue
        dispatch_name = dispatch_file.name

        if dry_run:
            actions.append(CleanupAction(
                "dispatches", "move", dispatch_name, "would_apply",
                "incomplete → failed/",
            ))
        else:
            failed_dir.mkdir(parents=True, exist_ok=True)
            dest = failed_dir / f"{dispatch_file.stem}.recovered.md"
            dispatch_file.rename(dest)
            actions.append(CleanupAction(
                "dispatches", "move", dispatch_name, "applied",
                "incomplete → failed/",
            ))

    return actions


# ---------------------------------------------------------------------------
# Step 4: Reset stale terminal claims (legacy only)
# ---------------------------------------------------------------------------

def reset_stale_terminal_claims(
    state_dir: str,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Reset terminals stuck in 'working' status to 'idle'."""
    actions: List[CleanupAction] = []
    ts_file = Path(state_dir) / "terminal_state.json"

    if not ts_file.is_file():
        return actions

    try:
        data = json.loads(ts_file.read_text())
    except (json.JSONDecodeError, OSError):
        return actions

    terminals = data.get("terminals", data)
    stale_tids = [
        tid for tid, info in terminals.items()
        if isinstance(info, dict) and info.get("status") == "working"
    ]

    for tid in stale_tids:
        if dry_run:
            actions.append(CleanupAction(
                "terminal_state", "reset", tid, "would_apply",
                "working → idle",
            ))
        else:
            terminals[tid]["status"] = "idle"
            terminals[tid]["claimed_by"] = None
            actions.append(CleanupAction(
                "terminal_state", "reset", tid, "applied",
                "working → idle",
            ))

    if stale_tids and not dry_run:
        ts_file.write_text(json.dumps(data, indent=2) + "\n")

    return actions


# ---------------------------------------------------------------------------
# Step 5: Unclean-shutdown marker
# ---------------------------------------------------------------------------

def cleanup_unclean_marker(
    locks_dir: str,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Clear the unclean-shutdown marker file."""
    marker = Path(locks_dir) / ".unclean_shutdown"
    if not marker.is_file():
        return []

    if dry_run:
        return [CleanupAction("marker", "clear", ".unclean_shutdown", "would_apply")]
    marker.unlink(missing_ok=True)
    return [CleanupAction("marker", "clear", ".unclean_shutdown", "applied")]


# ---------------------------------------------------------------------------
# Step 6: Stale payload temp files
# ---------------------------------------------------------------------------

def cleanup_stale_payloads(
    data_dir: str,
    max_age_minutes: int = 60,
    dry_run: bool = False,
) -> List[CleanupAction]:
    """Clean up payload temp files older than max_age_minutes."""
    payload_dir = Path(data_dir) / "dispatch_payloads"
    if not payload_dir.is_dir():
        return []

    cutoff = time.time() - (max_age_minutes * 60)
    stale_files = [
        f for f in payload_dir.glob("payload_*.txt")
        if f.is_file() and f.stat().st_mtime < cutoff
    ]

    if not stale_files:
        return []

    count = len(stale_files)
    if dry_run:
        return [CleanupAction(
            "payloads", "clean", f"{count} file(s)", "would_apply",
            f"stale payload temp files (>{max_age_minutes}m old)",
        )]

    for f in stale_files:
        f.unlink(missing_ok=True)

    return [CleanupAction(
        "payloads", "clean", f"{count} file(s)", "applied",
        f"stale payload temp files (>{max_age_minutes}m old)",
    )]


# ---------------------------------------------------------------------------
# Full legacy recovery
# ---------------------------------------------------------------------------

def run_legacy_recovery(
    locks_dir: str,
    pids_dir: str,
    dispatch_dir: str,
    state_dir: str,
    data_dir: str,
    dry_run: bool = False,
    max_lock_age: int = 3600,
    runtime_primary: bool = True,
    legacy_only: bool = False,
) -> LegacyRecoveryReport:
    """Run the full legacy recovery cleanup.

    This is the Python equivalent of the legacy cleanup portion of
    recover.sh (lines 112-322). It always runs as a complement to
    runtime recovery.
    """
    report = LegacyRecoveryReport(dry_run=dry_run)

    # Step 1: Stale locks
    lock_actions = cleanup_stale_locks(locks_dir, pids_dir, max_lock_age, dry_run)
    report.actions.extend(lock_actions)

    # Step 2: Stale PID files (non-aggressive mode only — aggressive is shell-level)
    pid_actions = cleanup_stale_pids(pids_dir, dry_run)
    report.actions.extend(pid_actions)

    # Step 3: Incomplete dispatches
    dispatch_actions = cleanup_incomplete_dispatches(dispatch_dir, dry_run)
    report.actions.extend(dispatch_actions)

    # Step 4: Terminal state reset (only when runtime core is off)
    if not runtime_primary or legacy_only:
        terminal_actions = reset_stale_terminal_claims(state_dir, dry_run)
        report.actions.extend(terminal_actions)

    # Step 5: Unclean-shutdown marker
    marker_actions = cleanup_unclean_marker(locks_dir, dry_run)
    report.actions.extend(marker_actions)

    # Step 6: Stale payload temp files
    payload_actions = cleanup_stale_payloads(data_dir, dry_run=dry_run)
    report.actions.extend(payload_actions)

    # Tally
    for action in report.actions:
        if action.outcome in ("applied", "would_apply"):
            report.issues_found += 1
        if action.outcome == "applied":
            report.issues_resolved += 1
        if action.outcome == "error":
            report.errors.append(f"{action.step}/{action.target}: {action.detail}")

    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rmtree_safe(path: Path) -> None:
    """Remove a directory tree, ignoring errors."""
    import shutil
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VNX Legacy Recovery Cleanup")
    parser.add_argument("--locks-dir", default=os.environ.get("VNX_LOCKS_DIR", ""))
    parser.add_argument("--pids-dir", default=os.environ.get("VNX_PIDS_DIR", ""))
    parser.add_argument("--dispatch-dir", default=os.environ.get("VNX_DISPATCH_DIR", ""))
    parser.add_argument("--state-dir", default=os.environ.get("VNX_STATE_DIR", ""))
    parser.add_argument("--data-dir", default=os.environ.get("VNX_DATA_DIR", ""))
    parser.add_argument("--max-lock-age", type=int, default=int(os.environ.get("VNX_LOCK_MAX_AGE", "3600")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--runtime-primary", action="store_true", default=True)
    parser.add_argument("--legacy-only", action="store_true")

    args = parser.parse_args()

    report = run_legacy_recovery(
        locks_dir=args.locks_dir,
        pids_dir=args.pids_dir,
        dispatch_dir=args.dispatch_dir,
        state_dir=args.state_dir,
        data_dir=args.data_dir,
        dry_run=args.dry_run,
        max_lock_age=args.max_lock_age,
        runtime_primary=args.runtime_primary,
        legacy_only=args.legacy_only,
    )

    if args.json:
        output = {
            "dry_run": report.dry_run,
            "issues_found": report.issues_found,
            "issues_resolved": report.issues_resolved,
            "actions": [
                {
                    "step": a.step,
                    "action": a.action,
                    "target": a.target,
                    "outcome": a.outcome,
                    "detail": a.detail,
                }
                for a in report.actions
            ],
            "summary": report.summary_text(),
        }
        print(json.dumps(output, indent=2))
    else:
        for a in report.actions:
            prefix = "WOULD" if a.outcome == "would_apply" else ""
            label = f"  {prefix} {a.action.upper()}: {a.target}"
            if a.detail:
                label += f" ({a.detail})"
            print(label)
        print()
        print(report.summary_text())

    sys.exit(0 if report.issues_found == 0 or report.issues_resolved > 0 else 1)
