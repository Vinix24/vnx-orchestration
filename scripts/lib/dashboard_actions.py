#!/usr/bin/env python3
"""
VNX Dashboard Control Actions — Safe operator actions for the Coding Operator Dashboard.

Implements the safe action model from the Dashboard Contract
(docs/core/140_DASHBOARD_READ_MODEL_CONTRACT.md §4):

  A1: Start Session — invoke vnx start for a project
  A2: Attach Terminal — resolve tmux pane from session profile
  A3: Refresh Projections — re-project from canonical sources
  A4: Run Reconciliation — detect and report mismatches
  A5: Inspect Open Item — return structured open item detail
  A6: Stop Session — invoke vnx stop for a project

Every action returns a structured ActionOutcome per §4.4:
  - AO-1: Every action produces exactly one outcome. No silent failures.
  - AO-2: 'failed' outcomes include human-readable message and error_code.
  - AO-3: 'already_active' is a valid success variant, not an error.
  - AO-4: 'degraded' means partially succeeded but result cannot be fully verified.

The action layer sits between the dashboard UI and the runtime system.
Actions read from the read-model (PR-1) and invoke shell commands only
for session lifecycle — never for data reads.
"""

from __future__ import annotations

import json
import subprocess
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Action Outcome Model (§4.4)
# ---------------------------------------------------------------------------

@dataclass
class ActionOutcome:
    """Structured result from every dashboard action.

    Invariants:
      AO-1: Every action produces exactly one outcome.
      AO-2: 'failed' includes message and error_code.
      AO-3: 'already_active' is a valid success variant.
      AO-4: 'degraded' means partially succeeded.
    """
    action: str
    project: str
    status: str  # "success" | "failed" | "already_active" | "degraded"
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    timestamp: str = field(default_factory=lambda: _now_iso())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d["error_code"] is None:
            del d["error_code"]
        return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# A1: Start Session (§4.2)
# ---------------------------------------------------------------------------

def start_session(
    project_path: str,
    *,
    vnx_bin: Optional[str] = None,
    dry_run: bool = False,
) -> ActionOutcome:
    """Start a VNX session for the given project.

    Safe — creates tmux session, initializes state files. Idempotent if session exists.
    Uses `vnx start` which handles cleanup of existing sessions.

    Args:
        project_path: Absolute path to the project directory.
        vnx_bin: Path to vnx binary. Auto-detected if None.
        dry_run: If True, validate without executing. For testing.
    """
    proj = Path(project_path)
    if not proj.is_dir():
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status="failed",
            message=f"Project directory does not exist: {project_path}",
            error_code="project_not_found",
        )

    # Locate vnx binary
    if vnx_bin is None:
        vnx_bin = _find_vnx_bin(proj)
    if vnx_bin is None:
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status="failed",
            message="Cannot find vnx binary in project or PATH",
            error_code="vnx_not_found",
        )

    # Check for existing session
    session_name = f"vnx-{proj.name}"
    session_active = _tmux_session_exists(session_name)

    if dry_run:
        status = "already_active" if session_active else "success"
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status=status,
            message=f"Dry run: session {'already active' if session_active else 'would be started'}",
            details={"session_name": session_name, "dry_run": True},
        )

    # Check if session is already running — vnx start is idempotent
    # (it kills and recreates), but we report already_active for clarity
    if session_active:
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status="already_active",
            message=f"Session '{session_name}' is already active. vnx start would recreate it.",
            details={"session_name": session_name},
        )

    # Execute vnx start
    try:
        result = subprocess.run(
            [vnx_bin, "start"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return ActionOutcome(
                action="start_session",
                project=project_path,
                status="success",
                message=f"Session '{session_name}' started successfully",
                details={"session_name": session_name, "exit_code": 0},
            )
        else:
            return ActionOutcome(
                action="start_session",
                project=project_path,
                status="failed",
                message=f"vnx start failed: {result.stderr.strip() or 'unknown error'}",
                error_code="start_failed",
                details={"exit_code": result.returncode, "stderr": result.stderr[:500]},
            )
    except subprocess.TimeoutExpired:
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status="degraded",
            message="vnx start timed out after 120s — session may be partially initialized",
            error_code="timeout",
            details={"session_name": session_name},
        )
    except OSError as e:
        return ActionOutcome(
            action="start_session",
            project=project_path,
            status="failed",
            message=f"Failed to execute vnx: {e}",
            error_code="exec_error",
        )


# ---------------------------------------------------------------------------
# A2: Attach Terminal (§4.2)
# ---------------------------------------------------------------------------

def attach_terminal(
    project_path: str,
    terminal_id: str,
    *,
    dry_run: bool = False,
) -> ActionOutcome:
    """Resolve the tmux pane for a terminal and return attach instructions.

    Safe — read-only intent. Returns the tmux command the operator should run.
    Does not change state.
    """
    proj = Path(project_path)
    state_dir = proj / ".vnx-data" / "state"
    session_name = f"vnx-{proj.name}"

    # Verify session exists
    if not _tmux_session_exists(session_name) and not dry_run:
        return ActionOutcome(
            action="attach_terminal",
            project=project_path,
            status="failed",
            message=f"No active session '{session_name}'. Start a session first.",
            error_code="no_session",
            details={"terminal_id": terminal_id},
        )

    # Resolve pane from session profile
    profile = _load_json(state_dir / "session_profile.json")
    pane_id = None
    if profile:
        for window in [profile.get("home_window", {})] + profile.get("extra_windows", []):
            for pane in window.get("panes", []):
                if pane.get("terminal_id") == terminal_id:
                    pane_id = pane.get("pane_id")
                    break

    if pane_id is None:
        # Fallback to panes.json
        panes = _load_json(state_dir / "panes.json")
        if panes:
            pane_id = panes.get(terminal_id, {}).get("pane_id") if isinstance(panes.get(terminal_id), dict) else panes.get(terminal_id)

    if pane_id is None:
        return ActionOutcome(
            action="attach_terminal",
            project=project_path,
            status="failed",
            message=f"Cannot resolve pane for terminal {terminal_id}. Session profile may be stale.",
            error_code="pane_not_found",
            details={"terminal_id": terminal_id},
        )

    attach_cmd = f"tmux select-pane -t {pane_id}"

    return ActionOutcome(
        action="attach_terminal",
        project=project_path,
        status="success",
        message=f"Terminal {terminal_id} is at pane {pane_id}",
        details={
            "terminal_id": terminal_id,
            "pane_id": pane_id,
            "session_name": session_name,
            "attach_command": attach_cmd,
        },
    )


# ---------------------------------------------------------------------------
# A3: Refresh Projections (§4.2)
# ---------------------------------------------------------------------------

def refresh_projections(
    project_path: str,
    *,
    dry_run: bool = False,
) -> ActionOutcome:
    """Re-project terminal_state.json from canonical lease state.

    Safe — read-only from DB, write to projection files. Idempotent.
    """
    proj = Path(project_path)
    state_dir = proj / ".vnx-data" / "state"

    if not state_dir.exists():
        return ActionOutcome(
            action="refresh_projections",
            project=project_path,
            status="failed",
            message="State directory does not exist. Has this project been initialized?",
            error_code="no_state_dir",
        )

    if dry_run:
        return ActionOutcome(
            action="refresh_projections",
            project=project_path,
            status="success",
            message="Dry run: would refresh terminal_state.json from canonical sources",
            details={"dry_run": True},
        )

    try:
        from lease_manager import LeaseManager
        mgr = LeaseManager(state_dir)
        out_path = mgr.project_to_file()
        return ActionOutcome(
            action="refresh_projections",
            project=project_path,
            status="success",
            message=f"Refreshed terminal_state.json from canonical lease state",
            details={"output_path": str(out_path)},
        )
    except Exception as e:
        return ActionOutcome(
            action="refresh_projections",
            project=project_path,
            status="failed",
            message=f"Projection refresh failed: {e}",
            error_code="projection_error",
        )


# ---------------------------------------------------------------------------
# A4: Run Reconciliation (§4.2)
# ---------------------------------------------------------------------------

def run_reconciliation(
    project_path: str,
    *,
    dry_run: bool = False,
) -> ActionOutcome:
    """Invoke runtime state reconciler to detect and report mismatches.

    Safe — read-only detection, writes audit records. Does not change state.
    """
    proj = Path(project_path)
    state_dir = proj / ".vnx-data" / "state"

    if not state_dir.exists():
        return ActionOutcome(
            action="run_reconciliation",
            project=project_path,
            status="failed",
            message="State directory does not exist.",
            error_code="no_state_dir",
        )

    if dry_run:
        return ActionOutcome(
            action="run_reconciliation",
            project=project_path,
            status="success",
            message="Dry run: would run reconciliation sweep",
            details={"dry_run": True},
        )

    try:
        from runtime_supervisor import RuntimeSupervisor
        supervisor = RuntimeSupervisor(state_dir)
        anomalies = supervisor.supervise_all()

        anomaly_summary = [
            {"type": a.anomaly_type, "severity": a.severity, "terminal": a.terminal_id}
            for a in anomalies
        ]

        if not anomalies:
            return ActionOutcome(
                action="run_reconciliation",
                project=project_path,
                status="success",
                message="Reconciliation complete. No anomalies detected.",
                details={"anomaly_count": 0},
            )
        else:
            return ActionOutcome(
                action="run_reconciliation",
                project=project_path,
                status="success",
                message=f"Reconciliation found {len(anomalies)} anomaly(ies)",
                details={"anomaly_count": len(anomalies), "anomalies": anomaly_summary},
            )
    except Exception as e:
        return ActionOutcome(
            action="run_reconciliation",
            project=project_path,
            status="failed",
            message=f"Reconciliation failed: {e}",
            error_code="reconciliation_error",
        )


# ---------------------------------------------------------------------------
# A5: Inspect Open Item (§4.2)
# ---------------------------------------------------------------------------

def inspect_open_item(
    project_path: str,
    item_id: str,
) -> ActionOutcome:
    """Navigate to open item detail with origin dispatch and evidence.

    Safe — pure read, no state change.
    """
    proj = Path(project_path)
    state_dir = proj / ".vnx-data" / "state"
    oi_path = state_dir / "open_items.json"

    data = _load_json(oi_path)
    if data is None:
        return ActionOutcome(
            action="inspect_open_item",
            project=project_path,
            status="failed",
            message="open_items.json not found or unreadable",
            error_code="file_not_found",
            details={"item_id": item_id},
        )

    for item in data.get("items", []):
        if item.get("id") == item_id:
            return ActionOutcome(
                action="inspect_open_item",
                project=project_path,
                status="success",
                message=f"Open item {item_id} found",
                details={"item": item},
            )

    return ActionOutcome(
        action="inspect_open_item",
        project=project_path,
        status="failed",
        message=f"Open item {item_id} not found",
        error_code="item_not_found",
        details={"item_id": item_id},
    )


# ---------------------------------------------------------------------------
# A6: Stop Session (§4.2)
# ---------------------------------------------------------------------------

def stop_session(
    project_path: str,
    *,
    vnx_bin: Optional[str] = None,
    dry_run: bool = False,
) -> ActionOutcome:
    """Stop a VNX session for the given project.

    Safe with confirmation — kills tmux session, releases leases. Non-idempotent.
    """
    proj = Path(project_path)
    session_name = f"vnx-{proj.name}"

    if not _tmux_session_exists(session_name):
        return ActionOutcome(
            action="stop_session",
            project=project_path,
            status="already_active",
            message=f"No active session '{session_name}' to stop.",
            details={"session_name": session_name},
        )

    if vnx_bin is None:
        vnx_bin = _find_vnx_bin(proj)
    if vnx_bin is None:
        return ActionOutcome(
            action="stop_session",
            project=project_path,
            status="failed",
            message="Cannot find vnx binary",
            error_code="vnx_not_found",
        )

    if dry_run:
        return ActionOutcome(
            action="stop_session",
            project=project_path,
            status="success",
            message=f"Dry run: would stop session '{session_name}'",
            details={"session_name": session_name, "dry_run": True},
        )

    try:
        result = subprocess.run(
            [vnx_bin, "stop"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return ActionOutcome(
                action="stop_session",
                project=project_path,
                status="success",
                message=f"Session '{session_name}' stopped",
                details={"session_name": session_name, "exit_code": 0},
            )
        else:
            return ActionOutcome(
                action="stop_session",
                project=project_path,
                status="failed",
                message=f"vnx stop failed: {result.stderr.strip() or 'unknown error'}",
                error_code="stop_failed",
                details={"exit_code": result.returncode},
            )
    except subprocess.TimeoutExpired:
        return ActionOutcome(
            action="stop_session",
            project=project_path,
            status="degraded",
            message="vnx stop timed out — session may still be partially running",
            error_code="timeout",
        )
    except OSError as e:
        return ActionOutcome(
            action="stop_session",
            project=project_path,
            status="failed",
            message=f"Failed to execute vnx: {e}",
            error_code="exec_error",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_vnx_bin(project_path: Path) -> Optional[str]:
    """Locate the vnx binary: project-local first, then PATH."""
    local = project_path / "bin" / "vnx"
    if local.exists() and local.is_file():
        return str(local)
    found = shutil.which("vnx")
    return found


def _tmux_session_exists(session_name: str) -> bool:
    """Check if a tmux session exists. Returns False if tmux is not available."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
