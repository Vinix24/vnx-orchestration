"""Shared throttled state-rebuild trigger.

Used by both Python (append_receipt.py) and bash (dispatch_lifecycle.sh) callers
to fire build_t0_state.py rebuild without storming the throttle file.

Throttle marker: $VNX_STATE_DIR/.last_state_rebuild_ts (integer epoch seconds).
Default throttle window: 30 seconds.

Off switch: ``VNX_STATE_REBUILD_TRIGGER=0`` makes ``maybe_trigger_state_rebuild``
return ``False`` without spawning anything. Any other value, or unset, keeps the
normal behaviour. ``tests/conftest.py`` sets it for the whole suite so no lifecycle
line starts a detached ``build_t0_state.py`` that races a test's store; a test that
exercises the spawn itself removes it with ``monkeypatch.delenv``.
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

from event_outcome_semantics import COMPLETION_EVENT_TYPES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_THROTTLE_SECONDS = 30
STATE_REBUILD_TRIGGER_ENV = "VNX_STATE_REBUILD_TRIGGER"
DEFAULT_PINNED_PYTHON = "/opt/homebrew/opt/python@3.12/bin/python3.12"

# The completion family (ADR-045 §2: dispatch_finished next to task_complete)
# plus the legacy literals this trigger has always honoured and the two failure
# types. A start or report line is not here: neither ends a dispatch.
CRITICAL_EVENTS = set(COMPLETION_EVENT_TYPES) | {
    "completion", "complete", "task_failed", "task_timeout",
}


def _executable_file(path: str) -> bool:
    return bool(path) and os.access(path, os.X_OK) and not os.path.isdir(path)


def _resolve_interpreter() -> str:
    """Pick the interpreter for the rebuild child (OI-3060).

    Same precedence as ``vnx_hook_python`` in scripts/hooks/lib_hook_python.sh:
    VNX_PYTHON > <repo>/.venv/bin/python > VNX_HOOK_PINNED_PYTHON (default
    homebrew 3.12) > fallback. The bare ``python3`` on PATH can be a build
    without PyYAML, so the last fallback is this process's own interpreter,
    never the literal ``python3``.
    """
    env_python = os.environ.get("VNX_PYTHON", "")
    if _executable_file(env_python):
        return env_python
    venv_python = str(_REPO_ROOT / ".venv" / "bin" / "python")
    if _executable_file(venv_python):
        return venv_python
    pinned = os.environ.get("VNX_HOOK_PINNED_PYTHON") or DEFAULT_PINNED_PYTHON
    if _executable_file(pinned):
        return pinned
    return sys.executable


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
    # D1 split (same as the hook had): rc=1 means the build succeeded and wrote
    # t0_state.json but system_health is degraded/failed; any other non-zero rc
    # means the build raised before a state file could be produced.
    if rc == 1:
        outcome = "built, t0_state.json refreshed, system_health degraded/failed"
    else:
        outcome = "failed build, t0_state.json not refreshed"
    try:
        log_path = state_dir.parent / "logs" / "build_t0_state.err"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(
                "===== %s (rc=%s, fired async via state_rebuild_trigger) %s =====\n"
                % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rc, outcome)
            )
    except OSError:
        pass


def maybe_trigger_state_rebuild(
    throttle_seconds: int = _DEFAULT_THROTTLE_SECONDS,
    event_type: str = "",
    reaper_out: Optional[list] = None,
) -> bool:
    """Fire build_t0_state.py if throttle expired.

    Critical events (CRITICAL_EVENTS) bypass throttle and BLOCK on lock
    acquisition so they never get silently dropped when a non-critical rebuild
    is in flight (e.g. dispatch_promoted holds the lock while task_complete
    arrives within the same 30s window).

    Non-critical events use LOCK_NB — they skip if another rebuild is in flight.

    Returns True if rebuild was triggered, False if throttled or on failure.

    ``reaper_out``: when a list is passed, the reaper thread of a fired rebuild
    is appended to it so the caller (the CLI with ``--wait``) can join it.

    Throttle contract:
    - Marker file holds INTEGER epoch seconds (no float — bash arithmetic compat)
    - Marker is written ONLY after Popen succeeds (no failure-suppression bug)
    - Atomic write via .tmp + rename
    """
    if os.environ.get(STATE_REBUILD_TRIGGER_ENV) == "0":
        return False

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
                    [_resolve_interpreter(), str(_REPO_ROOT / "scripts" / "build_t0_state.py")],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # D1 (poort E): reap in the background so a crash on this
                # fire-and-forget path leaves the same evidence a hook-driven
                # failure would, without blocking this throttle-guarded call.
                reaper = threading.Thread(
                    target=_reap_and_log_on_failure,
                    args=(proc, state_dir),
                    daemon=True,
                )
                reaper.start()
                if reaper_out is not None:
                    reaper_out.append(reaper)
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
#   python3 scripts/lib/state_rebuild_trigger.py [--event <type>] [--wait]
# Without --wait the CLI returns as soon as the build is started (the reaper is
# a daemon thread that dies with this process). With --wait the process joins
# the reaper before it exits, so a failed build still leaves its incident in
# build_t0_state.err; the SessionStart hook runs it detached for that reason.
def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Throttled t0_state rebuild trigger")
    parser.add_argument("--event", default="", help="event type (passed as event_type)")
    parser.add_argument("--wait", action="store_true",
                        help="join the reaper so a failed build is logged")
    args = parser.parse_args(argv)
    reapers: list = []
    maybe_trigger_state_rebuild(event_type=args.event, reaper_out=reapers)
    if args.wait:
        for reaper in reapers:
            reaper.join()
    # Always 0, regardless of outcome: a throttled, fired-and-succeeded and
    # fired-and-failed rebuild are not told apart by this exit code. A failed
    # build is logged by _reap_and_log_on_failure (only with --wait does the
    # CLI process live long enough for that).
    return 0


if __name__ == "__main__":
    sys.exit(_main())
