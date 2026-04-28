"""Shared throttle-based rebuild trigger for build_t0_state.py.

Called by append_receipt._maybe_trigger_state_rebuild and gate hook emitters
so the throttle contract is consistent across all producers.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _LIB_DIR.parent.parent
_REBUILD_THROTTLE_SECONDS = 30


def _resolve_state_dir() -> Path:
    """Resolve state dir using canonical vnx_paths; fall back to env vars."""
    try:
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_STATE_DIR"])
    except Exception:
        state_dir_env = os.environ.get("VNX_STATE_DIR")
        if state_dir_env:
            return Path(state_dir_env)
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            return Path(os.environ["VNX_DATA_DIR"]) / "state"
        return _REPO_ROOT / ".vnx-data" / "state"


def maybe_trigger_state_rebuild() -> bool:
    """Fire build_t0_state.py if throttle expired. Best-effort, non-blocking.

    Returns True if Popen was called, False if throttled or on any error.
    """
    try:
        state_dir = _resolve_state_dir()
        throttle_file = state_dir / ".last_state_rebuild_ts"
        now = int(time.time())
        try:
            last = int(throttle_file.read_text(encoding="utf-8").strip()) if throttle_file.exists() else 0
        except (ValueError, OSError):
            last = 0
        if now - last < _REBUILD_THROTTLE_SECONDS:
            return False
        subprocess.Popen(
            ["python3", str(_REPO_ROOT / "scripts" / "build_t0_state.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Atomic throttle update — only after Popen succeeds
        tmp = throttle_file.with_suffix(".tmp")
        tmp.write_text(str(now), encoding="utf-8")
        tmp.replace(throttle_file)
        return True
    except Exception:
        return False
