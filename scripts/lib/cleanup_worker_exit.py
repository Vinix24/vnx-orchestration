#!/usr/bin/env python3
"""cleanup_worker_exit.py — Single-owner post-worker-exit cleanup helper.

Foundation refactor for the unified supervisor pack (SUP-PR1).  Both the
interactive (tmux/dispatch_lifecycle.sh) and headless (subprocess_dispatch.py)
worker exit paths converge on `cleanup_worker_exit()` so the lease release,
worker state transition, dispatch file disposition, and audit event are
performed exactly once and in the same order.

Idempotent and never raises — cleanup must not block worker exit.  All errors
are caught and surfaced via CleanupResult.errors plus a structured stderr log.

CLI surface (used from bash callers):

    python3 cleanup_worker_exit.py \\
        --terminal-id T1 \\
        --dispatch-id 20260429-foo \\
        --exit-status success \\
        [--lease-generation 3] \\
        [--dispatch-file /path/to/active/foo.md]

Always exits 0 — best-effort cleanup, errors land in stderr/log.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:
    from project_root import resolve_data_dir, resolve_state_dir
except ImportError:  # pragma: no cover - bootstrap failure
    resolve_data_dir = None  # type: ignore[assignment]
    resolve_state_dir = None  # type: ignore[assignment]


VALID_EXIT_STATUSES = ("success", "failure", "timeout", "killed", "stuck")

EXIT_STATUS_TO_WORKER_STATE = {
    "success": "exited_clean",
    "failure": "exited_bad",
    "timeout": "exited_bad",
    "killed": "exited_bad",
    "stuck": "exited_bad",
}

EXIT_STATUS_TO_DISPOSITION = {
    "success": ("completed", None),
    "failure": ("rejected", "failure"),
    "timeout": ("rejected", "timeout"),
    "killed": ("rejected", "killed"),
    "stuck": ("rejected", "stuck"),
}


@dataclass
class CleanupResult:
    """Outcome of a cleanup_worker_exit() invocation.

    All fields default to "did not happen" so a partial cleanup (e.g. lease
    already released, no dispatch_file given) reports truthfully.
    """

    lease_released: bool = False
    worker_transitioned: bool = False
    dispatch_moved: Optional[Path] = None
    errors: List[str] = field(default_factory=list)


def _emit(level: str, code: str, **fields: Any) -> None:
    """Structured stderr log mirroring scripts/append_receipt.py:_emit."""
    payload = {
        "level": level,
        "code": code,
        "timestamp": int(time.time()),
    }
    payload.update(fields)
    try:
        print(
            json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
            file=sys.stderr,
        )
    except Exception:  # pragma: no cover - logging must never raise
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_state_dir() -> Path:
    """Resolve VNX state dir using project_root with explicit-env fallbacks."""
    if resolve_state_dir is not None:
        try:
            return resolve_state_dir(__file__)
        except Exception as exc:
            _emit("WARN", "state_dir_resolution_failed", error=str(exc))

    state_env = os.environ.get("VNX_STATE_DIR")
    if state_env:
        return Path(state_env)
    data_env = os.environ.get("VNX_DATA_DIR")
    if data_env:
        return Path(data_env) / "state"
    return _THIS_DIR.parent.parent / ".vnx-data" / "state"


def _resolve_dispatch_register_path() -> Path:
    """Resolve dispatch_register.ndjson path matching gate_register_emit.py."""
    state_env = os.environ.get("VNX_STATE_DIR")
    if state_env:
        return Path(state_env) / "dispatch_register.ndjson"
    if (
        os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
        and os.environ.get("VNX_DATA_DIR")
    ):
        return Path(os.environ["VNX_DATA_DIR"]) / "state" / "dispatch_register.ndjson"
    if resolve_state_dir is not None:
        try:
            return resolve_state_dir(__file__) / "dispatch_register.ndjson"
        except Exception:
            pass
    return _THIS_DIR.parent.parent / ".vnx-data" / "state" / "dispatch_register.ndjson"


def _release_lease_step(
    *,
    terminal_id: str,
    dispatch_id: str,
    lease_generation: Optional[int],
    exit_status: str,
    state_dir: Path,
    result: CleanupResult,
) -> None:
    """Step 1: release lease via LeaseManager.release.

    Idempotent: if the lease is already idle/expired, treat as released-already
    and surface a non-fatal note in errors[].  Generation mismatch is recorded
    but does not raise.  None generation means we have nothing to release with —
    skip with a note.
    """
    if lease_generation is None:
        result.errors.append("lease_generation_missing")
        return

    try:
        from lease_manager import LeaseManager  # noqa: PLC0415
        from runtime_coordination import InvalidTransitionError  # noqa: PLC0415
    except Exception as exc:
        result.errors.append(f"lease_import_failed:{exc}")
        _emit("WARN", "lease_import_failed", error=str(exc), dispatch_id=dispatch_id)
        return

    try:
        mgr = LeaseManager(state_dir, auto_init=False)
        # Pre-check: if the lease is already idle, no-op.
        try:
            current = mgr.get(terminal_id)
        except Exception:
            current = None

        if current is not None and current.state != "leased":
            result.lease_released = True
            result.errors.append("lease_already_released")
            return

        mgr.release(
            terminal_id,
            generation=lease_generation,
            actor="cleanup_worker_exit",
            reason=f"worker exited:{exit_status}",
        )
        result.lease_released = True
    except InvalidTransitionError as exc:
        result.lease_released = True
        result.errors.append(f"lease_already_released:{exc}")
    except ValueError as exc:
        result.errors.append(f"lease_generation_mismatch:{exc}")
        _emit(
            "WARN",
            "lease_generation_mismatch",
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            generation=lease_generation,
            error=str(exc),
        )
    except Exception as exc:
        result.errors.append(f"lease_release_failed:{exc}")
        _emit(
            "WARN",
            "lease_release_failed",
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            error=str(exc),
        )


def _transition_worker_step(
    *,
    terminal_id: str,
    dispatch_id: str,
    exit_status: str,
    state_dir: Path,
    result: CleanupResult,
) -> None:
    """Step 2: transition worker state to exited_clean / exited_bad.

    Idempotent: if the worker state row is already in a terminal state or
    missing, no-op with a note.
    """
    target = EXIT_STATUS_TO_WORKER_STATE.get(exit_status, "exited_bad")

    try:
        from worker_state_manager import (  # noqa: PLC0415
            TERMINAL_WORKER_STATES,
            WorkerStateManager,
        )
    except Exception as exc:
        result.errors.append(f"worker_state_import_failed:{exc}")
        _emit("WARN", "worker_state_import_failed", error=str(exc))
        return

    try:
        mgr = WorkerStateManager(state_dir, auto_init=False)
        try:
            current = mgr.get(terminal_id)
        except Exception as exc:
            result.errors.append(f"worker_state_lookup_failed:{exc}")
            return

        if current is None:
            result.errors.append("worker_state_missing")
            return

        if current.state in TERMINAL_WORKER_STATES:
            result.worker_transitioned = True
            result.errors.append("worker_already_terminal")
            return

        mgr.transition(
            terminal_id,
            target,
            actor="cleanup_worker_exit",
            reason=f"worker exited:{exit_status}",
        )
        result.worker_transitioned = True
    except Exception as exc:
        result.errors.append(f"worker_transition_failed:{exc}")
        _emit(
            "WARN",
            "worker_transition_failed",
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            target=target,
            error=str(exc),
        )


def _move_dispatch_file_step(
    *,
    dispatch_file: Optional[Path],
    exit_status: str,
    result: CleanupResult,
) -> None:
    """Step 3: move dispatch file to completed/ or rejected/<reason>/.

    Resolves the destination relative to the file's grandparent (the dispatches
    dir):  active/foo.md → completed/foo.md  or  rejected/<reason>/foo.md.
    No-op when dispatch_file is None or already moved.
    """
    if dispatch_file is None:
        return

    try:
        src = Path(dispatch_file)
        if not src.exists():
            result.errors.append("dispatch_file_missing")
            return

        bucket, reason = EXIT_STATUS_TO_DISPOSITION.get(
            exit_status, ("rejected", "failure")
        )
        dispatch_dir = src.parent.parent
        if reason:
            dest_dir = dispatch_dir / bucket / reason
        else:
            dest_dir = dispatch_dir / bucket

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        # Idempotent: if already moved (dest exists, src removed by prior call),
        # we won't reach here because src.exists() returned False above.
        shutil.move(str(src), str(dest))
        result.dispatch_moved = dest
    except Exception as exc:
        result.errors.append(f"dispatch_move_failed:{exc}")
        _emit(
            "WARN",
            "dispatch_move_failed",
            dispatch_file=str(dispatch_file),
            exit_status=exit_status,
            error=str(exc),
        )


def _append_audit_event_step(
    *,
    terminal_id: str,
    dispatch_id: str,
    exit_status: str,
    result: CleanupResult,
) -> None:
    """Step 4: append worker_exited audit event to dispatch_register.ndjson."""
    try:
        import fcntl  # noqa: PLC0415

        record = {
            "timestamp": _now_iso(),
            "event": "worker_exited",
            "dispatch_id": dispatch_id,
            "terminal_id": terminal_id,
            "exit_status": exit_status,
            "lease_released": result.lease_released,
            "worker_transitioned": result.worker_transitioned,
            "dispatch_moved": str(result.dispatch_moved) if result.dispatch_moved else None,
        }

        path = _resolve_dispatch_register_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        result.errors.append(f"audit_append_failed:{exc}")
        _emit(
            "WARN",
            "audit_append_failed",
            dispatch_id=dispatch_id,
            error=str(exc),
        )


def _archive_event_store_step(
    *,
    terminal_id: str,
    dispatch_id: str,
    result: CleanupResult,
) -> None:
    """Step 5: best-effort EventStore archive for subprocess context.

    Only runs when the EventStore module is importable AND a default store
    can be located.  Tmux callers will have nothing to archive — that's fine.
    """
    try:
        from event_store import EventStore  # noqa: PLC0415
    except Exception:
        return

    try:
        store = EventStore()
        store.clear(terminal_id, archive_dispatch_id=dispatch_id)
    except Exception as exc:
        result.errors.append(f"event_archive_failed:{exc}")
        _emit(
            "WARN",
            "event_archive_failed",
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            error=str(exc),
        )


def cleanup_worker_exit(
    *,
    terminal_id: str,
    dispatch_id: str,
    exit_status: str,
    lease_generation: Optional[int] = None,
    dispatch_file: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> CleanupResult:
    """Idempotent post-worker-exit cleanup.  Both adapters call this.

    Steps (all best-effort, errors caught and recorded):
      1. Release lease via LeaseManager.release.
      2. Transition worker state via WorkerStateManager.transition →
         exited_clean (success) or exited_bad (any other exit).
      3. Move dispatch file: success → completed/, otherwise →
         rejected/<reason>/ where <reason> is the exit_status.
      4. Append worker_exited event to dispatch_register.ndjson.
      5. Best-effort EventStore archive (no-op when not subprocess context).

    Args:
        terminal_id:      Terminal whose worker has just exited (e.g. "T1").
        dispatch_id:      Dispatch identifier the worker was processing.
        exit_status:      One of "success", "failure", "timeout", "killed",
                          "stuck".  Unknown values are normalised to "failure".
        lease_generation: Generation captured at lease acquire.  When None,
                          lease release is skipped (recorded in errors).
        dispatch_file:    Path to the dispatch file in dispatches/active/.
                          When None, file move is skipped.
        state_dir:        Optional override; defaults to project-root
                          resolution.

    Returns:
        CleanupResult — never raises.
    """
    if exit_status not in VALID_EXIT_STATUSES:
        _emit(
            "WARN",
            "unknown_exit_status",
            exit_status=exit_status,
            dispatch_id=dispatch_id,
        )
        exit_status = "failure"

    result = CleanupResult()
    resolved_state_dir = state_dir if state_dir is not None else _resolve_state_dir()

    _release_lease_step(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        lease_generation=lease_generation,
        exit_status=exit_status,
        state_dir=resolved_state_dir,
        result=result,
    )

    _transition_worker_step(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        exit_status=exit_status,
        state_dir=resolved_state_dir,
        result=result,
    )

    _move_dispatch_file_step(
        dispatch_file=dispatch_file,
        exit_status=exit_status,
        result=result,
    )

    _append_audit_event_step(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        exit_status=exit_status,
        result=result,
    )

    _archive_event_store_step(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        result=result,
    )

    return result


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cleanup_worker_exit",
        description="Single-owner post-worker-exit cleanup (lease, worker state, "
                    "dispatch file, audit event).  Always exits 0.",
    )
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument(
        "--exit-status",
        required=True,
        choices=list(VALID_EXIT_STATUSES),
    )
    parser.add_argument(
        "--lease-generation",
        type=int,
        default=None,
        help="Lease generation captured at acquire-time (optional).",
    )
    parser.add_argument(
        "--dispatch-file",
        type=Path,
        default=None,
        help="Path to dispatch file in dispatches/active/ (optional).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    result = cleanup_worker_exit(
        terminal_id=args.terminal_id,
        dispatch_id=args.dispatch_id,
        exit_status=args.exit_status,
        lease_generation=args.lease_generation,
        dispatch_file=args.dispatch_file,
    )
    payload = {
        "lease_released": result.lease_released,
        "worker_transitioned": result.worker_transitioned,
        "dispatch_moved": str(result.dispatch_moved) if result.dispatch_moved else None,
        "errors": result.errors,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
