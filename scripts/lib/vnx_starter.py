#!/usr/bin/env python3
"""VNX Starter Mode — single-terminal, no-tmux execution runtime.

Provides a simplified first-run experience that does not require tmux or a
full 4-terminal operator grid. Dispatches execute sequentially in the current
terminal. Receipts, provenance, and audit trails are preserved.

Contracts:
  G-R2: Receipts and runtime state emitted in starter mode.
  A-R1: Shares the same canonical runtime model as operator mode.
  Productization §2.1: Starter mode = single terminal, one provider, sequential.
  Productization §7.2: Receipt completeness — every dispatch produces a receipt.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnx_mode import VNXMode, read_mode, write_mode, check_mode_feature_enabled
from vnx_paths import ensure_env, resolve_paths


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STARTER_PROVIDER = "claude_code"
STARTER_TERMINAL_ID = "T0"
STARTER_TRACK = "A"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StarterConfig:
    """Runtime configuration for starter mode."""
    project_root: str
    data_dir: str
    state_dir: str
    dispatch_dir: str
    receipts_dir: str
    provider: str = STARTER_PROVIDER

    @classmethod
    def from_env(cls) -> "StarterConfig":
        paths = resolve_paths()
        return cls(
            project_root=paths["PROJECT_ROOT"],
            data_dir=paths["VNX_DATA_DIR"],
            state_dir=paths["VNX_STATE_DIR"],
            dispatch_dir=paths["VNX_DISPATCH_DIR"],
            receipts_dir=str(Path(paths["VNX_DATA_DIR"]) / "receipts"),
        )


@dataclass
class StarterDispatchResult:
    """Result of a starter-mode dispatch execution."""
    dispatch_id: str
    status: str  # success | error | timeout
    terminal_id: str = STARTER_TERMINAL_ID
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    receipt_path: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Atomic I/O (consistent with codebase pattern)
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically via temp-file-then-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Starter mode initialization
# ---------------------------------------------------------------------------

def init_starter(data_dir: Optional[str] = None) -> StarterConfig:
    """Initialize starter mode: write mode.json, create minimal state.

    This is called by ``vnx init --starter``. It sets up the runtime layout
    for single-terminal operation.
    """
    if not check_mode_feature_enabled(VNXMode.STARTER):
        raise RuntimeError(
            "Starter mode is disabled (VNX_STARTER_MODE_ENABLED=0). "
            "Set VNX_STARTER_MODE_ENABLED=1 or use 'vnx init --operator'."
        )

    paths = ensure_env()
    if data_dir:
        paths["VNX_DATA_DIR"] = data_dir

    config = StarterConfig(
        project_root=paths["PROJECT_ROOT"],
        data_dir=paths["VNX_DATA_DIR"],
        state_dir=paths["VNX_STATE_DIR"],
        dispatch_dir=paths["VNX_DISPATCH_DIR"],
        receipts_dir=str(Path(paths["VNX_DATA_DIR"]) / "receipts"),
    )

    # Ensure directories
    for d in [config.state_dir, config.dispatch_dir, config.receipts_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)
    for sub in ["pending", "active", "completed", "rejected", "failed"]:
        (Path(config.dispatch_dir) / sub).mkdir(parents=True, exist_ok=True)

    # Write mode
    write_mode(VNXMode.STARTER, config.data_dir)

    # Write minimal terminal state (single terminal)
    _write_starter_terminal_state(config)

    # Write starter panes.json (no tmux panes, just metadata)
    _write_starter_panes(config)

    return config


def _write_starter_terminal_state(config: StarterConfig) -> None:
    """Write terminal_state.json for starter mode (T0 only, idle)."""
    state = {
        "schema_version": 1,
        "mode": "starter",
        "terminals": {
            STARTER_TERMINAL_ID: {
                "terminal_id": STARTER_TERMINAL_ID,
                "status": "idle",
                "last_activity": _now_utc(),
                "version": 1,
            }
        },
    }
    path = Path(config.state_dir) / "terminal_state.json"
    _atomic_write_json(path, state)


def _write_starter_panes(config: StarterConfig) -> None:
    """Write panes.json for starter mode (no tmux session)."""
    panes = {
        "session": None,
        "mode": "starter",
        "t0": {
            "pane_id": None,
            "role": "orchestrator",
            "model": "default",
            "provider": config.provider,
            "track": STARTER_TRACK,
        },
    }
    path = Path(config.state_dir) / "panes.json"
    _atomic_write_json(path, panes)


# ---------------------------------------------------------------------------
# Receipt generation (starter mode)
# ---------------------------------------------------------------------------

def write_starter_receipt(
    config: StarterConfig,
    dispatch_id: str,
    status: str,
    work_type: str = "coding",
    files_changed: Optional[List[str]] = None,
    commit_sha: Optional[str] = None,
    duration_seconds: float = 0.0,
    error: Optional[str] = None,
) -> Path:
    """Write an NDJSON receipt line for a starter-mode dispatch.

    Returns the path to the receipt file.
    """
    receipt = {
        "dispatch_id": dispatch_id,
        "receipt_id": f"receipt:{dispatch_id}",
        "terminal_id": STARTER_TERMINAL_ID,
        "trace_token": f"Dispatch-ID: {dispatch_id}",
        "timestamp": _now_utc(),
        "status": status,
        "work_type": work_type,
        "files_changed": files_changed or [],
        "commit_sha": commit_sha or "",
        "provenance": "CLEAN",
        "in_worktree": False,
        "mode": "starter",
        "duration_seconds": duration_seconds,
        "context_used_pct": 0,
        "rotation_triggered": False,
    }
    if error:
        receipt["error"] = error

    receipts_dir = Path(config.receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = receipts_dir / "t0_receipts.ndjson"

    with open(receipt_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, sort_keys=True) + "\n")

    # Also write per-dispatch receipt for backward compat
    dispatch_receipt_dir = Path(config.dispatch_dir) / "completed"
    dispatch_receipt_dir.mkdir(parents=True, exist_ok=True)
    per_dispatch = dispatch_receipt_dir / f"{dispatch_id}.receipt.json"
    _atomic_write_json(per_dispatch, receipt)

    return receipt_file


# ---------------------------------------------------------------------------
# Dispatch lifecycle (starter mode)
# ---------------------------------------------------------------------------

def create_starter_dispatch(
    config: StarterConfig,
    dispatch_id: str,
    description: str,
    pr: str = "",
    track: str = STARTER_TRACK,
    skill: str = "",
    prompt: str = "",
) -> Path:
    """Create a dispatch bundle in pending/ for starter mode.

    Returns the bundle directory path.
    """
    bundle_dir = Path(config.dispatch_dir) / "pending" / dispatch_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    bundle_data = {
        "dispatch_id": dispatch_id,
        "created_at": _now_utc(),
        "mode": "starter",
        "terminal_id": STARTER_TERMINAL_ID,
        "track": track,
        "pr": pr,
        "skill": skill,
        "description": description,
        "status": "pending",
    }

    _atomic_write_json(bundle_dir / "bundle.json", bundle_data)

    if prompt:
        (bundle_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    return bundle_dir


def promote_starter_dispatch(config: StarterConfig, dispatch_id: str) -> Path:
    """Move a dispatch from pending/ to active/ (starter mode promotion)."""
    pending = Path(config.dispatch_dir) / "pending" / dispatch_id
    active = Path(config.dispatch_dir) / "active" / dispatch_id

    if not pending.exists():
        raise FileNotFoundError(f"No pending dispatch: {dispatch_id}")

    active.parent.mkdir(parents=True, exist_ok=True)
    pending.rename(active)

    # Update bundle status
    bundle_path = active / "bundle.json"
    if bundle_path.exists():
        with open(bundle_path, "r") as f:
            bundle = json.load(f)
        bundle["status"] = "active"
        bundle["promoted_at"] = _now_utc()
        _atomic_write_json(bundle_path, bundle)

    return active


def complete_starter_dispatch(
    config: StarterConfig,
    dispatch_id: str,
    status: str = "success",
    error: Optional[str] = None,
) -> Path:
    """Move a dispatch from active/ to completed/."""
    active = Path(config.dispatch_dir) / "active" / dispatch_id
    completed = Path(config.dispatch_dir) / "completed" / dispatch_id

    if not active.exists():
        raise FileNotFoundError(f"No active dispatch: {dispatch_id}")

    completed.parent.mkdir(parents=True, exist_ok=True)
    active.rename(completed)

    # Update bundle status
    bundle_path = completed / "bundle.json"
    if bundle_path.exists():
        with open(bundle_path, "r") as f:
            bundle = json.load(f)
        bundle["status"] = status
        bundle["completed_at"] = _now_utc()
        if error:
            bundle["error"] = error
        _atomic_write_json(bundle_path, bundle)

    return completed


# ---------------------------------------------------------------------------
# Starter status
# ---------------------------------------------------------------------------

def get_starter_status(config: StarterConfig) -> Dict[str, Any]:
    """Return current starter mode status for display."""
    dispatch_dir = Path(config.dispatch_dir)
    pending = list((dispatch_dir / "pending").glob("*/bundle.json"))
    active = list((dispatch_dir / "active").glob("*/bundle.json"))
    completed = list((dispatch_dir / "completed").glob("*/bundle.json"))

    # Count receipts
    receipt_file = Path(config.receipts_dir) / "t0_receipts.ndjson"
    receipt_count = 0
    if receipt_file.exists():
        with open(receipt_file, "r") as f:
            receipt_count = sum(1 for line in f if line.strip())

    return {
        "mode": "starter",
        "provider": config.provider,
        "terminal": STARTER_TERMINAL_ID,
        "dispatches": {
            "pending": len(pending),
            "active": len(active),
            "completed": len(completed),
        },
        "receipts": receipt_count,
        "data_dir": config.data_dir,
    }


# ---------------------------------------------------------------------------
# Starter mode limits documentation
# ---------------------------------------------------------------------------

STARTER_LIMITS = """
VNX Starter Mode Limits
========================

Available:
  - Single-terminal dispatch creation and execution
  - Receipt generation and audit trail
  - Quality intelligence database
  - Doctor health checks
  - Status reporting
  - Recovery from failures

Not available (requires operator mode):
  - Multi-terminal orchestration (T0-T3 grid)
  - tmux session management (start/stop/jump)
  - Parallel multi-track dispatch
  - Provider profiles and presets
  - Worktree operations
  - Dashboard
  - Merge preflight checks

To upgrade: vnx init --operator
"""


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    paths = ensure_env()
    config = StarterConfig.from_env()
    status = get_starter_status(config)
    print(json.dumps(status, indent=2))
    sys.exit(0)
