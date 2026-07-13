"""Gate lock file management for F39 headless T0.

Lock files live at $VNX_STATE_DIR/gate_locks/
Format: {pr_id}.{gate_name}.lock  (e.g. PR-204.codex.lock)

A lock file's presence means the gate is pending.
Only the gate completion process removes the lock.

Usage:
    from scripts.f39.gate_locks import create_lock, release_lock, has_pending_locks

    # When gate is requested:
    create_lock("PR-204", "codex_gate")

    # When gate result arrives:
    released = release_lock("PR-204", "codex_gate")

    # Before dispatching:
    if has_pending_locks("PR-204"):
        return "WAIT"
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
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


def create_lock(pr_id: str, gate_name: str) -> Path:
    """Create a gate lock. Called when gate is requested."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{pr_id}.{gate_name}.lock"
    lock.write_text(
        json.dumps({
            "pr_id": pr_id,
            "gate_name": gate_name,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "requested_by": "t0_prefilter",
        }),
        encoding="utf-8",
    )
    return lock


def release_lock(pr_id: str, gate_name: str) -> bool:
    """Release a gate lock. Called when gate result arrives.

    Returns True if lock existed and was removed, False if not found.
    """
    lock = LOCK_DIR / f"{pr_id}.{gate_name}.lock"
    if lock.exists():
        lock.unlink()
        return True
    return False


def get_pending_locks(pr_id: str | None = None) -> list[dict]:
    """List all pending locks, optionally filtered by PR.

    Returns a list of lock metadata dicts (pr_id, gate_name, requested_at, requested_by).
    """
    if not LOCK_DIR.exists():
        return []
    locks = []
    for f in sorted(LOCK_DIR.glob("*.lock")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if pr_id is None or data.get("pr_id") == pr_id:
            locks.append(data)
    return locks


def has_pending_locks(pr_id: str) -> bool:
    """Check if any gate locks exist for this PR."""
    if not LOCK_DIR.exists():
        return False
    return any(LOCK_DIR.glob(f"{pr_id}.*.lock"))
