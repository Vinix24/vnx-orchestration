"""Shared throttled state-rebuild trigger.

Used by both Python (append_receipt.py) and bash (dispatch_lifecycle.sh) callers
to fire build_t0_state.py rebuild without storming the throttle file.

Throttle marker: $VNX_STATE_DIR/.last_state_rebuild_ts (integer epoch seconds).
Default throttle window: 30 seconds.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_THROTTLE_SECONDS = 30

CRITICAL_EVENTS = {
    "task_complete", "task_completed", "completion", "complete",
    "task_failed", "task_timeout",
}


def _resolve_state_dir() -> Path:
    """Resolve state dir via canonical vnx_paths, with fallback chain."""
    try:
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_STATE_DIR"])
    except Exception:
        # Fallback chain: VNX_STATE_DIR > VNX_DATA_DIR (with EXPLICIT) > repo-relative
        state_dir_env = os.environ.get("VNX_STATE_DIR")
        if state_dir_env:
            return Path(state_dir_env)
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            return Path(os.environ["VNX_DATA_DIR"]) / "state"
        return _REPO_ROOT / ".vnx-data" / "state"


def maybe_trigger_state_rebuild(
    throttle_seconds: int = _DEFAULT_THROTTLE_SECONDS,
    event_type: str = "",
) -> bool:
    """Fire build_t0_state.py if throttle expired.

    Critical events (CRITICAL_EVENTS) bypass throttle and BLOCK on lock
    acquisition so they never get silently dropped when a non-critical rebuild
    is in flight (e.g. dispatch_promoted holds the lock while task_complete
    arrives within the same 30s window).

    Non-critical events use LOCK_NB — they skip if another rebuild is in flight.

    Returns True if rebuild was triggered, False if throttled or on failure.

    Throttle contract:
    - Marker file holds INTEGER epoch seconds (no float — bash arithmetic compat)
    - Marker is written ONLY after Popen succeeds (no failure-suppression bug)
    - Atomic write via .tmp + rename
    """
    bypass_throttle = event_type in CRITICAL_EVENTS

    state_dir = _resolve_state_dir()
    throttle = state_dir / ".last_state_rebuild_ts"
    lock_path = state_dir / ".last_state_rebuild_ts.lock"
    state_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time())

    try:
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            if bypass_throttle:
                # CRITICAL events: block until lock is free so the rebuild fires
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            else:
                # Non-critical: skip if another rebuild is already in flight
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False

            if not bypass_throttle:
                last = 0
                try:
                    if throttle.exists():
                        content = throttle.read_text(encoding="utf-8").strip()
                        # Tolerate float (legacy main writers) — strip decimal portion
                        last = int(float(content)) if content else 0
                except (ValueError, OSError):
                    last = 0
                if now - last < throttle_seconds:
                    return False

            try:
                subprocess.Popen(
                    ["python3", str(_REPO_ROOT / "scripts" / "build_t0_state.py")],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # Atomic throttle marker — write ONLY after Popen succeeded
                tmp = throttle.with_suffix(".tmp")
                tmp.write_text(str(now), encoding="utf-8")
                tmp.replace(throttle)
                return True
            except Exception:
                return False
            # fcntl.flock released on with-exit
    except Exception:
        return False


__all__ = ["maybe_trigger_state_rebuild", "CRITICAL_EVENTS"]


# CLI entry for bash hooks (e.g., dispatch_lifecycle.sh):
#   python3 scripts/lib/state_rebuild_trigger.py
if __name__ == "__main__":
    maybe_trigger_state_rebuild()
    sys.exit(0)  # Always 0: throttled-as-expected and fired-successfully are both valid outcomes
