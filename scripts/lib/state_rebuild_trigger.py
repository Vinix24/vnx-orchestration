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
import threading
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


def _reap_and_log_on_failure(proc: "subprocess.Popen[bytes]", state_dir: Path) -> None:
    """Wait for the fired rebuild in the background; leave evidence if it failed.

    D1 (poort E): before this, a rebuild fired here had exactly two outcomes
    in the code — throttled, or "fired" (Popen didn't raise) — while a third,
    real outcome existed in practice: fired-and-then-crashed. Popen was
    started with stdout/stderr=DEVNULL and never wait()ed, so a crash inside
    build_t0_state.py from this path left no trace anywhere. Runs in a daemon
    thread so the caller stays non-blocking (the whole point of firing this
    under a throttle instead of calling build_t0_state directly) while still
    appending a timestamped incident to the same central log
    build_t0_state_hook.sh uses, once the child actually exits.
    """
    try:
        rc = proc.wait()
    except Exception:
        return
    if rc == 0:
        return
    try:
        log_path = state_dir.parent / "logs" / "build_t0_state.err"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                "===== %s (rc=%s, fired async via state_rebuild_trigger) =====\n"
                % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rc)
            )
    except OSError:
        pass


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
                proc = subprocess.Popen(
                    ["python3", str(_REPO_ROOT / "scripts" / "build_t0_state.py")],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # D1 (poort E): reap in the background so a crash on this
                # fire-and-forget path leaves the same evidence a hook-driven
                # failure would, without blocking this throttle-guarded call.
                threading.Thread(
                    target=_reap_and_log_on_failure,
                    args=(proc, state_dir),
                    daemon=True,
                ).start()
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
    # Always 0, regardless of outcome. There are three outcomes here —
    # throttled, fired-and-succeeded, fired-and-failed — and this exit code
    # cannot distinguish them: the call above only fires Popen and returns,
    # it never wait()s for the child (that would turn a throttle meant to
    # protect a hot path into a blocking call). A fired-and-failed rebuild
    # is not silent though — see _reap_and_log_on_failure, which appends to
    # the shared build_t0_state.err once the child actually exits.
    sys.exit(0)
