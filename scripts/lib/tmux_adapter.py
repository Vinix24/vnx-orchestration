#!/usr/bin/env python3
"""
VNX tmux Adapter — Delivery abstraction for worker terminal activation.

Design
------
The adapter decouples dispatch *identity* from tmux *mechanics*. It is the
sole place that knows how to translate a (terminal_id, dispatch_id) pair
into a tmux pane command.

Primary delivery path (VNX_ADAPTER_PRIMARY=1, default):
  Send `load-dispatch <dispatch_id>` as a short control command to the
  target pane. The worker terminal reads the dispatch bundle from disk and
  activates the skill + prompt without a full paste-buffer transfer.

Fallback delivery path (VNX_ADAPTER_PRIMARY=0):
  Legacy hybrid: type skill command via send-keys, paste full prompt via
  tmux load-buffer + paste-buffer. Kept for migration safety (A-R9).

Feature flags
-------------
  VNX_TMUX_ADAPTER_ENABLED   "1" (default) = adapter active, "0" = disabled
  VNX_ADAPTER_PRIMARY        "1" (default) = primary load-dispatch path,
                             "0" = legacy paste-buffer path

Pane resolution
---------------
Pane IDs are read from panes.json (the tmux adapter projection), which is
authoritative for pane → terminal mapping. Pane remaps only update
panes.json; they do NOT affect dispatch registry or lease state (A-R3).

Event recording
---------------
Every delivery attempt (start, success, failure, not-found) is written to
coordination_events so failures are never logs-only (G-R3, G-R5).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import _append_event, get_connection, get_lease


# ---------------------------------------------------------------------------
# Feature-flag helpers
# ---------------------------------------------------------------------------

def adapter_enabled() -> bool:
    """Return True when VNX_TMUX_ADAPTER_ENABLED != "0"."""
    return os.environ.get("VNX_TMUX_ADAPTER_ENABLED", "1").strip() != "0"


def primary_path_active() -> bool:
    """Return True when VNX_ADAPTER_PRIMARY != "0" (load-dispatch path)."""
    return os.environ.get("VNX_ADAPTER_PRIMARY", "1").strip() != "0"


def adapter_config_from_env() -> Dict[str, Any]:
    """Return adapter config dict derived from environment."""
    return {
        "enabled": adapter_enabled(),
        "primary_path": primary_path_active(),
    }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PaneTarget:
    """Resolved tmux pane target for a terminal_id."""
    terminal_id: str
    pane_id: str
    provider: str = "claude_code"


@dataclass
class DeliveryResult:
    """Result of a single delivery attempt."""
    success: bool
    terminal_id: str
    dispatch_id: str
    pane_id: Optional[str]
    path_used: str          # "primary" | "legacy" | "none"
    failure_reason: Optional[str] = None
    tmux_returncode: Optional[int] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AdapterError(Exception):
    """Base error for tmux adapter failures."""


class AdapterDisabledError(AdapterError):
    """Raised when the adapter is accessed while VNX_TMUX_ADAPTER_ENABLED=0."""


class PaneNotFoundError(AdapterError):
    """Raised when panes.json does not contain the requested terminal_id."""


class LeaseNotActiveError(AdapterError):
    """Raised when the target terminal does not hold an active lease for the dispatch."""


# ---------------------------------------------------------------------------
# Pane resolution helpers
# ---------------------------------------------------------------------------

def _read_panes_json(panes_path: Path) -> Dict[str, Any]:
    """Return parsed panes.json content or empty dict."""
    if not panes_path.exists():
        return {}
    try:
        return json.loads(panes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_pane(
    terminal_id: str,
    panes_path: Path,
) -> PaneTarget:
    """Resolve terminal_id to a PaneTarget using panes.json.

    Pane IDs are adapter state only (A-R3). A pane remap changes
    the pane_id here but does not affect dispatch or lease state.

    Raises:
        PaneNotFoundError: If the terminal is not in panes.json.
    """
    panes = _read_panes_json(panes_path)

    # Support both lowercase and uppercase keys (e.g. "t0", "T1")
    entry = panes.get(terminal_id) or panes.get(terminal_id.upper()) or panes.get(terminal_id.lower())
    if not entry:
        raise PaneNotFoundError(
            f"Terminal {terminal_id!r} not found in panes.json ({panes_path}). "
            f"Available: {list(panes.keys())}"
        )

    pane_id = entry.get("pane_id") or entry.get("id")
    if not pane_id:
        raise PaneNotFoundError(
            f"Terminal {terminal_id!r} entry in panes.json has no 'pane_id' field."
        )

    provider = entry.get("provider", "claude_code")
    return PaneTarget(terminal_id=terminal_id, pane_id=pane_id, provider=provider)


# ---------------------------------------------------------------------------
# Low-level tmux execution helpers
# ---------------------------------------------------------------------------

def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _run_tmux(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a tmux command, returning CompletedProcess. Never raises."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tmux_send_keys(pane_id: str, *keys: str, literal: bool = False) -> int:
    """Send keys to a tmux pane. Returns returncode (0 = success)."""
    cmd = ["tmux", "send-keys", "-t", pane_id]
    if literal:
        cmd.append("-l")
    cmd.extend(keys)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.returncode


def _tmux_load_and_paste(pane_id: str, content: str, max_inline: int = 50000) -> int:
    """Load content into tmux buffer and paste to pane.

    For large payloads, writes to a temp file first to avoid truncation.
    Returns returncode of the paste-buffer command (0 = success).
    """
    if len(content) > max_inline:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".vnx_buf", delete=False, encoding="utf-8") as f:
            f.write(content)
            tmp_path = f.name
        try:
            rc = subprocess.run(["tmux", "load-buffer", tmp_path], capture_output=True, timeout=10).returncode
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    else:
        proc = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=content,
            capture_output=True,
            text=True,
            timeout=10,
        )
        rc = proc.returncode

    if rc != 0:
        return rc

    result = subprocess.run(
        ["tmux", "paste-buffer", "-t", pane_id],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# TmuxAdapter
# ---------------------------------------------------------------------------

class TmuxAdapter:
    """Abstraction layer for worker terminal delivery via tmux.

    Primary path: delivers `load-dispatch <dispatch_id>` as a short
    control command, reducing tmux to a delivery edge only.

    Fallback path: legacy hybrid (send-keys skill + paste-buffer prompt).

    All delivery attempts are recorded as coordination events.

    Args:
        state_dir:    Path to .vnx-data/state/ (contains runtime_coordination.db
                      and panes.json).
        primary_path: If True, use load-dispatch path. If False, legacy path.
                      Overrides VNX_ADAPTER_PRIMARY env flag when provided.
    """

    LOAD_DISPATCH_CMD = "load-dispatch"

    def __init__(
        self,
        state_dir: str | Path,
        *,
        primary_path: Optional[bool] = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._panes_path = self._state_dir / "panes.json"
        self._primary_path = primary_path if primary_path is not None else primary_path_active()

    @property
    def primary_path(self) -> bool:
        return self._primary_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_target(self, terminal_id: str) -> PaneTarget:
        """Resolve terminal_id to a PaneTarget via panes.json.

        Pane ID is adapter state only (A-R3). The canonical terminal
        ownership lives in terminal_leases, not here.

        Raises:
            PaneNotFoundError: If terminal is absent from panes.json.
        """
        return resolve_pane(terminal_id, self._panes_path)

    def validate_lease(self, terminal_id: str, dispatch_id: str) -> None:
        """Confirm terminal_id holds an active lease for dispatch_id.

        This is a soft pre-delivery check. It does not block delivery
        if the lease manager is unavailable (shadow mode compatibility).

        Raises:
            LeaseNotActiveError: If the terminal has no active lease or
                the lease does not match dispatch_id.
        """
        try:
            with get_connection(self._state_dir) as conn:
                row = get_lease(conn, terminal_id)
        except Exception:
            # DB unavailable in shadow mode — skip validation silently.
            return

        if row is None:
            raise LeaseNotActiveError(
                f"Terminal {terminal_id!r} has no lease row. "
                f"Dispatch {dispatch_id!r} may not have been claimed."
            )

        lease = dict(row)
        if lease.get("state") not in ("leased", "recovering"):
            raise LeaseNotActiveError(
                f"Terminal {terminal_id!r} lease state is {lease.get('state')!r}, "
                f"expected 'leased'. Dispatch: {dispatch_id!r}"
            )

        if lease.get("dispatch_id") != dispatch_id:
            raise LeaseNotActiveError(
                f"Terminal {terminal_id!r} is leased to dispatch "
                f"{lease.get('dispatch_id')!r}, not {dispatch_id!r}."
            )

    def deliver(
        self,
        terminal_id: str,
        dispatch_id: str,
        attempt_id: Optional[str] = None,
        *,
        # Legacy path parameters (ignored on primary path)
        skill_command: Optional[str] = None,
        prompt: Optional[str] = None,
        actor: str = "adapter",
    ) -> DeliveryResult:
        """Deliver dispatch to terminal_id.

        Uses primary (load-dispatch) or legacy (paste-buffer) path based
        on instance configuration and env flags.

        Args:
            terminal_id:   Target terminal (e.g. "T2").
            dispatch_id:   Dispatch identifier.
            attempt_id:    Attempt ID from broker (for event linkage).
            skill_command: Skill invocation string for legacy path only.
            prompt:        Full prompt text for legacy path only.
            actor:         Actor label recorded in coordination events.

        Returns:
            DeliveryResult with success status and path used.
        """
        if not _tmux_available():
            return self._record_and_return(
                DeliveryResult(
                    success=False,
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    pane_id=None,
                    path_used="none",
                    failure_reason="tmux binary not found in PATH",
                ),
                attempt_id=attempt_id,
                actor=actor,
            )

        # Resolve pane — failure is a hard stop (cannot deliver without pane)
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError as exc:
            result = DeliveryResult(
                success=False,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pane_id=None,
                path_used="none",
                failure_reason=str(exc),
            )
            self._emit_event(
                "adapter_pane_not_found",
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                attempt_id=attempt_id,
                reason=str(exc),
                actor=actor,
            )
            return result

        if self._primary_path:
            return self._deliver_primary(target, dispatch_id, attempt_id, actor=actor)
        else:
            return self._deliver_legacy(
                target, dispatch_id, attempt_id,
                skill_command=skill_command or "",
                prompt=prompt or "",
                actor=actor,
            )

    # ------------------------------------------------------------------
    # Primary delivery path: load-dispatch <dispatch_id>
    # ------------------------------------------------------------------

    def _deliver_primary(
        self,
        target: PaneTarget,
        dispatch_id: str,
        attempt_id: Optional[str],
        *,
        actor: str,
    ) -> DeliveryResult:
        """Send `load-dispatch <dispatch_id>` to the target pane."""
        self._emit_event(
            "adapter_deliver_start",
            dispatch_id=dispatch_id,
            terminal_id=target.terminal_id,
            attempt_id=attempt_id,
            reason="primary path: load-dispatch command",
            actor=actor,
        )

        cmd = f"{self.LOAD_DISPATCH_CMD} {dispatch_id}"

        # Clear any pending input first
        _tmux_send_keys(target.pane_id, "C-u")

        # Send the load-dispatch command (literal, no shell interpretation)
        rc = _tmux_send_keys(target.pane_id, cmd, literal=True)
        if rc != 0:
            return self._record_failure(
                target, dispatch_id, attempt_id,
                reason=f"send-keys load-dispatch failed (rc={rc})",
                path="primary",
                actor=actor,
            )

        # Submit
        rc = _tmux_send_keys(target.pane_id, "Enter")
        if rc != 0:
            return self._record_failure(
                target, dispatch_id, attempt_id,
                reason=f"send-keys Enter failed after load-dispatch (rc={rc})",
                path="primary",
                actor=actor,
            )

        return self._record_success(target, dispatch_id, attempt_id, path="primary", actor=actor)

    # ------------------------------------------------------------------
    # Legacy fallback: skill send-keys + paste-buffer prompt
    # ------------------------------------------------------------------

    def _deliver_legacy(
        self,
        target: PaneTarget,
        dispatch_id: str,
        attempt_id: Optional[str],
        *,
        skill_command: str,
        prompt: str,
        actor: str,
    ) -> DeliveryResult:
        """Legacy hybrid: send skill via send-keys, prompt via paste-buffer."""
        self._emit_event(
            "adapter_deliver_start",
            dispatch_id=dispatch_id,
            terminal_id=target.terminal_id,
            attempt_id=attempt_id,
            reason="legacy path: skill send-keys + paste-buffer",
            actor=actor,
            metadata={"path": "legacy", "provider": target.provider},
        )

        # Clear pending input
        _tmux_send_keys(target.pane_id, "C-u")

        if target.provider == "codex_cli":
            # Codex: combined skill + prompt in a single paste-buffer
            combined = f"{skill_command}\n{prompt}" if skill_command else prompt
            rc = _tmux_load_and_paste(target.pane_id, combined)
            if rc != 0:
                return self._record_failure(
                    target, dispatch_id, attempt_id,
                    reason=f"codex paste-buffer failed (rc={rc})",
                    path="legacy",
                    actor=actor,
                )
        else:
            # Claude Code: type skill via send-keys, paste prompt separately
            if skill_command:
                rc = _tmux_send_keys(target.pane_id, skill_command, literal=True)
                if rc != 0:
                    return self._record_failure(
                        target, dispatch_id, attempt_id,
                        reason=f"send-keys skill command failed (rc={rc})",
                        path="legacy",
                        actor=actor,
                    )

            if prompt:
                rc = _tmux_load_and_paste(target.pane_id, prompt)
                if rc != 0:
                    return self._record_failure(
                        target, dispatch_id, attempt_id,
                        reason=f"paste-buffer prompt failed (rc={rc})",
                        path="legacy",
                        actor=actor,
                    )

        # Submit
        rc = _tmux_send_keys(target.pane_id, "Enter")
        if rc != 0:
            return self._record_failure(
                target, dispatch_id, attempt_id,
                reason=f"send-keys Enter failed (rc={rc})",
                path="legacy",
                actor=actor,
            )

        return self._record_success(target, dispatch_id, attempt_id, path="legacy", actor=actor)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        event_type: str,
        *,
        dispatch_id: str,
        terminal_id: str,
        attempt_id: Optional[str],
        reason: Optional[str] = None,
        actor: str = "adapter",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a coordination event. Silently no-ops if DB unavailable."""
        meta = {"terminal_id": terminal_id}
        if attempt_id:
            meta["attempt_id"] = attempt_id
        if metadata:
            meta.update(metadata)
        try:
            with get_connection(self._state_dir) as conn:
                _append_event(
                    conn,
                    event_type=event_type,
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    actor=actor,
                    reason=reason,
                    metadata=meta,
                )
                conn.commit()
        except Exception:
            pass  # Shadow mode: DB may not exist yet

    def _record_success(
        self,
        target: PaneTarget,
        dispatch_id: str,
        attempt_id: Optional[str],
        *,
        path: str,
        actor: str,
    ) -> DeliveryResult:
        self._emit_event(
            "adapter_deliver_success",
            dispatch_id=dispatch_id,
            terminal_id=target.terminal_id,
            attempt_id=attempt_id,
            reason=f"delivery succeeded via {path} path",
            actor=actor,
            metadata={"path": path, "pane_id": target.pane_id},
        )
        return DeliveryResult(
            success=True,
            terminal_id=target.terminal_id,
            dispatch_id=dispatch_id,
            pane_id=target.pane_id,
            path_used=path,
        )

    def _record_failure(
        self,
        target: PaneTarget,
        dispatch_id: str,
        attempt_id: Optional[str],
        *,
        reason: str,
        path: str,
        actor: str,
        rc: Optional[int] = None,
    ) -> DeliveryResult:
        self._emit_event(
            "adapter_deliver_failure",
            dispatch_id=dispatch_id,
            terminal_id=target.terminal_id,
            attempt_id=attempt_id,
            reason=reason,
            actor=actor,
            metadata={"path": path, "pane_id": target.pane_id},
        )
        return DeliveryResult(
            success=False,
            terminal_id=target.terminal_id,
            dispatch_id=dispatch_id,
            pane_id=target.pane_id,
            path_used=path,
            failure_reason=reason,
            tmux_returncode=rc,
        )

    def _record_and_return(
        self,
        result: DeliveryResult,
        *,
        attempt_id: Optional[str],
        actor: str,
    ) -> DeliveryResult:
        """Emit failure event for pre-resolution errors and return result."""
        if not result.success:
            self._emit_event(
                "adapter_deliver_failure",
                dispatch_id=result.dispatch_id,
                terminal_id=result.terminal_id,
                attempt_id=attempt_id,
                reason=result.failure_reason,
                actor=actor,
            )
        return result


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_adapter(state_dir: str | Path) -> Optional["TmuxAdapter"]:
    """Return a TmuxAdapter if VNX_TMUX_ADAPTER_ENABLED=1 (default), else None.

    Args:
        state_dir: Path to .vnx-data/state/.

    Returns:
        Configured TmuxAdapter or None if adapter is disabled.
    """
    if not adapter_enabled():
        return None
    return TmuxAdapter(state_dir)
