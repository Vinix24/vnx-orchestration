#!/usr/bin/env python3
"""VNX Mode — detection, storage, and command gating.

Manages the three VNX execution modes (starter, operator, demo) as defined
in the productization contract (PR-0). Mode is persisted in
``.vnx-data/mode.json`` and checked at command dispatch time.

Contracts:
  G-R2: Receipts and runtime state in all modes.
  A-R1: Starter, operator, and demo share the same canonical runtime model.
  Productization §2.4: mode.json is source of truth for current mode.
  Productization §7.5: No silent degradation — unavailable commands fail explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional


# ---------------------------------------------------------------------------
# Mode enum
# ---------------------------------------------------------------------------

class VNXMode(str, Enum):
    STARTER = "starter"
    OPERATOR = "operator"
    DEMO = "demo"

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Command tiers (productization contract §3.2)
# ---------------------------------------------------------------------------

TIER_UNIVERSAL: FrozenSet[str] = frozenset({
    "init", "doctor", "status", "recover", "help", "update",
    "setup", "install-check", "install-validate",
})

TIER_STARTER_OPERATOR: FrozenSet[str] = frozenset({
    "staging-list", "promote", "queue-status", "gate-check", "suggest",
    "cost-report", "analyze-sessions", "intelligence-export",
    "intelligence-import", "init-feature", "bootstrap-skills",
    "bootstrap-terminals", "bootstrap-hooks", "regen-settings",
    "patch-agent-files", "register", "list-projects", "unregister",
    "roadmap",
    "install-git-hooks", "uninstall-git-hooks", "install-shell-helper",
    "init-db",
})

TIER_OPERATOR_ONLY: FrozenSet[str] = frozenset({
    "start", "stop", "restart", "jump", "ps", "cleanup",
    "new-worktree", "finish-worktree", "worktree-start", "worktree-stop",
    "worktree-refresh", "worktree-status", "merge-preflight",
    "smoke", "package-check",
    "dispatch", "gate",
})

TIER_DEMO_ONLY: FrozenSet[str] = frozenset({
    "demo",
})

# Mode -> allowed command sets
MODE_COMMANDS: Dict[VNXMode, FrozenSet[str]] = {
    VNXMode.STARTER: TIER_UNIVERSAL | TIER_STARTER_OPERATOR,
    VNXMode.OPERATOR: TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY,
    VNXMode.DEMO: TIER_UNIVERSAL | TIER_DEMO_ONLY,
}


# ---------------------------------------------------------------------------
# Mode file I/O
# ---------------------------------------------------------------------------

MODE_FILENAME = "mode.json"


def _mode_file_path(data_dir: Optional[str] = None) -> Path:
    """Return path to mode.json, deriving from environment if needed."""
    if data_dir is None:
        data_dir = os.environ.get("VNX_DATA_DIR")
    if not data_dir:
        raise RuntimeError(
            "VNX_DATA_DIR not set. Run 'vnx init' first."
        )
    return Path(data_dir) / MODE_FILENAME


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


def read_mode(data_dir: Optional[str] = None) -> Optional[VNXMode]:
    """Read current mode from mode.json. Returns None if not initialized."""
    try:
        path = _mode_file_path(data_dir)
    except RuntimeError:
        return None
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return VNXMode(data["mode"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def write_mode(mode: VNXMode, data_dir: Optional[str] = None) -> Path:
    """Write mode to mode.json atomically. Returns the path written."""
    path = _mode_file_path(data_dir)
    payload = {
        "mode": str(mode),
        "set_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
    }
    _atomic_write_json(path, payload)
    return path


def read_mode_raw(data_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the full mode.json document (for status display)."""
    try:
        path = _mode_file_path(data_dir)
    except RuntimeError:
        return None
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Command gating
# ---------------------------------------------------------------------------

class ModeGateError(Exception):
    """Raised when a command is not available in the current mode."""

    def __init__(self, command: str, current_mode: VNXMode):
        self.command = command
        self.current_mode = current_mode
        if current_mode == VNXMode.STARTER:
            upgrade = "Run 'vnx init --operator' to upgrade."
        elif current_mode == VNXMode.DEMO:
            upgrade = "Run 'vnx init' to set up a real project."
        else:
            upgrade = ""
        super().__init__(
            f"'{command}' requires a different mode (current: {current_mode}). {upgrade}".strip()
        )


def check_command_allowed(command: str, mode: Optional[VNXMode] = None,
                          data_dir: Optional[str] = None) -> None:
    """Check if command is allowed in current mode. Raises ModeGateError if not.

    If mode is None, reads from mode.json. If mode.json doesn't exist,
    all commands are allowed (pre-init backward compatibility).
    """
    if mode is None:
        mode = read_mode(data_dir)
    if mode is None:
        # Not initialized yet — allow everything (backward compat)
        return
    allowed = MODE_COMMANDS.get(mode, frozenset())
    if command not in allowed:
        raise ModeGateError(command, mode)


def get_available_commands(mode: Optional[VNXMode] = None,
                           data_dir: Optional[str] = None) -> FrozenSet[str]:
    """Return the set of commands available in the current or given mode."""
    if mode is None:
        mode = read_mode(data_dir)
    if mode is None:
        # Pre-init: all commands
        return TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY | TIER_DEMO_ONLY
    return MODE_COMMANDS.get(mode, frozenset())


def get_mode_description(mode: VNXMode) -> str:
    """Return a human-readable description of the mode."""
    descriptions = {
        VNXMode.STARTER: "Single terminal, one AI provider, sequential dispatch. No tmux required.",
        VNXMode.OPERATOR: "Full multi-agent orchestration with tmux grid, multiple providers, and all governance controls.",
        VNXMode.DEMO: "Showcase VNX capabilities with dry-run dispatches and sample state. No API keys required.",
    }
    return descriptions.get(mode, "Unknown mode")


# ---------------------------------------------------------------------------
# Feature flags for rollback control
# ---------------------------------------------------------------------------

FEATURE_FLAGS = {
    "VNX_STARTER_MODE_ENABLED": ("1", "Enable starter mode (set '0' to disable)"),
    "VNX_DEMO_MODE_ENABLED": ("1", "Enable demo mode (set '0' to disable)"),
    "VNX_MODE_GATING_ENABLED": ("1", "Enable command gating by mode (set '0' for backward compat)"),
}


def is_feature_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled. Defaults from FEATURE_FLAGS."""
    default, _ = FEATURE_FLAGS.get(flag_name, ("0", ""))
    return os.environ.get(flag_name, default) == "1"


def check_mode_feature_enabled(mode: VNXMode) -> bool:
    """Check if the given mode is enabled via feature flags."""
    flag_map = {
        VNXMode.STARTER: "VNX_STARTER_MODE_ENABLED",
        VNXMode.DEMO: "VNX_DEMO_MODE_ENABLED",
    }
    flag = flag_map.get(mode)
    if flag is None:
        return True  # Operator mode always enabled
    return is_feature_enabled(flag)


# ---------------------------------------------------------------------------
# CLI entrypoint (for direct testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    from vnx_paths import ensure_env
    ensure_env()

    mode = read_mode()
    if mode:
        print(f"Current mode: {mode}")
        print(f"Description: {get_mode_description(mode)}")
        raw = read_mode_raw()
        if raw:
            print(f"Set at: {raw.get('set_at', 'unknown')}")
        print(f"Available commands: {len(get_available_commands(mode))}")
    else:
        print("No mode set (pre-init state)")
    sys.exit(0)
