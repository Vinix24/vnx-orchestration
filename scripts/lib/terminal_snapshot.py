#!/usr/bin/env python3
"""Terminal snapshot collector for VNX quality advisory pipeline.

Collects current state of all terminals (T0/T1/T2/T3) for inclusion in
completion receipt quality advisories.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class TerminalState:
    """State of a single terminal."""
    terminal: str
    status: str
    claimed_by: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    last_activity: Optional[str] = None
    lease_expires_at: Optional[str] = None


@dataclass
class TerminalSnapshot:
    """Snapshot of all terminal states."""
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    terminals: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "terminals": self.terminals,
        }


def get_terminal_state_from_files(state_dir: Path) -> Dict[str, TerminalState]:
    """Read terminal state from VNX state files.

    Priority order (most reliable first):
    1. terminal_state.json — updated by receipt_processor on every receipt
    2. terminal_status.ndjson — append-only status log
    3. dashboard_status.json — generated periodically by dashboard generator
    """
    states = {}

    # 1. Primary source: terminal_state.json (receipt_processor writes this)
    terminal_state_file = state_dir / "terminal_state.json"
    if terminal_state_file.exists():
        try:
            with open(terminal_state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                terminals_data = data.get("terminals", {})
                for terminal_id, term_data in terminals_data.items():
                    # Only accept canonical terminal IDs
                    if terminal_id in ("T0", "T1", "T2", "T3"):
                        states[terminal_id] = TerminalState(
                            terminal=terminal_id,
                            status=term_data.get("status", "unknown"),
                            claimed_by=term_data.get("claimed_by"),
                            last_activity=term_data.get("last_activity"),
                            lease_expires_at=term_data.get("lease_expires_at"),
                        )
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Fallback: terminal_status.ndjson
    terminal_status_file = state_dir / "terminal_status.ndjson"
    if terminal_status_file.exists():
        try:
            with open(terminal_status_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        terminal = data.get("terminal", "")
                        if terminal in ("T0", "T1", "T2", "T3") and terminal not in states:
                            states[terminal] = TerminalState(
                                terminal=terminal,
                                status=data.get("status", "unknown"),
                                model=data.get("model"),
                                last_activity=data.get("timestamp"),
                            )
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    # 3. Enrich with dashboard_status.json (model, provider, etc.)
    dashboard_file = state_dir / "dashboard_status.json"
    if dashboard_file.exists():
        try:
            with open(dashboard_file, "r", encoding="utf-8") as f:
                dashboard = json.load(f)
                terminals_data = dashboard.get("terminals", {})

                for terminal in ("T0", "T1", "T2", "T3"):
                    if terminal in terminals_data:
                        term_data = terminals_data[terminal]
                        if terminal not in states:
                            states[terminal] = TerminalState(
                                terminal=terminal,
                                status=term_data.get("status", "unknown"),
                            )

                        # Enrich with model/provider (don't overwrite status from more reliable sources)
                        states[terminal].provider = term_data.get("provider") or states[terminal].provider
                        states[terminal].model = term_data.get("model") or states[terminal].model
                        if not states[terminal].last_activity or states[terminal].last_activity == "never":
                            states[terminal].last_activity = term_data.get("last_update", states[terminal].last_activity)
        except (OSError, json.JSONDecodeError):
            pass

    # Enrich with model info from panes.json (authoritative for model assignment)
    panes_file = state_dir / "panes.json"
    if panes_file.exists():
        try:
            with open(panes_file, "r", encoding="utf-8") as f:
                panes = json.load(f)
                for terminal in ("T0", "T1", "T2", "T3"):
                    if terminal in panes and terminal in states:
                        states[terminal].model = panes[terminal].get("model") or states[terminal].model
        except (OSError, json.JSONDecodeError):
            pass

    # Ensure all terminals are present (even if no data)
    for terminal in ("T0", "T1", "T2", "T3"):
        if terminal not in states:
            states[terminal] = TerminalState(
                terminal=terminal,
                status="unknown",
            )

    return states


def get_terminal_state_from_tmux() -> Dict[str, TerminalState]:
    """Get terminal state from tmux panes (fallback method)."""
    states = {}

    try:
        # Get tmux pane info
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_title}:#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue

            # Parse tmux output
            parts = line.split(":", 1)
            if len(parts) != 2:
                continue

            pane_title, command = parts

            # Detect terminal from pane title (assuming format like "VNX-T0", "T1", etc.)
            terminal = None
            for t in ("T0", "T1", "T2", "T3"):
                if t in pane_title:
                    terminal = t
                    break

            if not terminal:
                continue

            # Determine status from command
            status = "active" if command not in ("zsh", "bash", "sh") else "idle"

            states[terminal] = TerminalState(
                terminal=terminal,
                status=status,
                last_activity=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass  # tmux not available or command failed

    # Ensure all terminals are present
    for terminal in ("T0", "T1", "T2", "T3"):
        if terminal not in states:
            states[terminal] = TerminalState(
                terminal=terminal,
                status="unknown",
            )

    return states


def collect_terminal_snapshot(state_dir: Optional[Path] = None) -> TerminalSnapshot:
    """Collect current snapshot of all terminal states.

    Args:
        state_dir: Path to VNX state directory. If None, tries to detect from environment.

    Returns:
        TerminalSnapshot with current state of all terminals
    """
    if state_dir is None:
        # Try to detect from environment or common locations
        vnx_state = Path.home() / ".claude" / "vnx-system" / "state"
        if vnx_state.exists():
            state_dir = vnx_state
        else:
            # Fallback to current working directory
            state_dir = Path.cwd() / ".claude" / "vnx-system" / "state"

    snapshot = TerminalSnapshot()

    # Try to get state from files first (more reliable)
    states = {}
    if state_dir and state_dir.exists():
        states = get_terminal_state_from_files(state_dir)

    # If no state files, try tmux as fallback
    if not states or all(s.status == "unknown" for s in states.values()):
        tmux_states = get_terminal_state_from_tmux()
        # Merge tmux data with file data
        for terminal, tmux_state in tmux_states.items():
            if terminal not in states or states[terminal].status == "unknown":
                states[terminal] = tmux_state

    # Convert to dict format for snapshot
    for terminal, state in states.items():
        snapshot.terminals[terminal] = {
            "status": state.status,
            "claimed_by": state.claimed_by,
            "provider": state.provider,
            "model": state.model,
            "last_activity": state.last_activity,
            "lease_expires_at": state.lease_expires_at,
        }

    return snapshot
