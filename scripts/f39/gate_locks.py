"""Gate lock file management for F39 headless T0.

Lock files live at $VNX_STATE_DIR/gate_locks/
Format: {pr_id}.{gate_name}.lock  (e.g. PR-204.codex.lock)

A lock file's presence means the gate is pending.
Only the gate completion process removes the lock.

Usage:
    from scripts.f39.gate_locks import release_lock

    # When gate result arrives:
    released = release_lock("PR-204", "codex_gate")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_state_dir() -> Path:
    """Resolve VNX_STATE_DIR via canonical vnx_paths, falling back to CWD-relative.

    A bare ``.vnx-data/state`` default resolves relative to CWD, which is only
    correct when CWD happens to be the project root with a legacy project-local
    layout; it ignores the central ``~/.vnx-data/<project>/state`` location.
    """
    env = os.environ.get("VNX_STATE_DIR")
    if env:
        return Path(env)
    try:
        repo_root = str(Path(__file__).resolve().parents[2] / "scripts" / "lib")
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from vnx_paths import resolve_paths
        return Path(resolve_paths()["VNX_STATE_DIR"])
    except Exception:
        return Path(".vnx-data/state")


LOCK_DIR = _resolve_state_dir() / "gate_locks"


def release_lock(pr_id: str, gate_name: str) -> bool:
    """Release a gate lock. Called when gate result arrives.

    Returns True if lock existed and was removed, False if not found.
    """
    lock = LOCK_DIR / f"{pr_id}.{gate_name}.lock"
    if lock.exists():
        lock.unlink()
        return True
    return False

