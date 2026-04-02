#!/usr/bin/env python3
"""VNX tmux Adapter — RuntimeAdapter implementation for tmux-based terminals.

Decouples dispatch identity from tmux mechanics. Primary path sends
`load-dispatch <dispatch_id>` via send-keys. Legacy fallback uses paste-buffer.
Feature flags: VNX_TMUX_ADAPTER_ENABLED, VNX_ADAPTER_PRIMARY.
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


@dataclass
class SpawnResult:
    """Result of spawning an execution surface."""
    success: bool
    transport_ref: str = ""
    error: Optional[str] = None


@dataclass
class StopResult:
    """Result of stopping an execution surface."""
    success: bool
    was_running: bool = False
    error: Optional[str] = None


@dataclass
class AttachResult:
    """Result of switching operator focus."""
    success: bool
    error: Optional[str] = None


@dataclass
class ObservationResult:
    """Read-only state probe result."""
    exists: bool
    responsive: bool = False
    transport_state: Dict[str, Any] = field(default_factory=dict)
    last_output_fragment: Optional[str] = None
    error: Optional[str] = None


@dataclass
class InspectionResult:
    """Deep diagnostic inspection result."""
    exists: bool
    transport_ref: str = ""
    transport_details: Dict[str, Any] = field(default_factory=dict)
    pane_content: Optional[str] = None
    environment: Optional[Dict[str, str]] = None
    error: Optional[str] = None


@dataclass
class HealthResult:
    """Fast health check result."""
    healthy: bool
    surface_exists: bool = False
    process_alive: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class SessionHealthResult:
    """Aggregate health check result."""
    session_exists: bool
    terminals: Dict[str, HealthResult] = field(default_factory=dict)
    degraded_terminals: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RehealResult:
    """Transport drift recovery result."""
    rehealed: bool
    old_ref: Optional[str] = None
    new_ref: Optional[str] = None
    strategy: str = ""
    error: Optional[str] = None


# Capability constants
CAPABILITY_SPAWN = "SPAWN"
CAPABILITY_STOP = "STOP"
CAPABILITY_DELIVER = "DELIVER"
CAPABILITY_ATTACH = "ATTACH"
CAPABILITY_OBSERVE = "OBSERVE"
CAPABILITY_INSPECT = "INSPECT"
CAPABILITY_HEALTH = "HEALTH"
CAPABILITY_SESSION_HEALTH = "SESSION_HEALTH"
CAPABILITY_REHEAL = "REHEAL"

TMUX_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN, CAPABILITY_STOP, CAPABILITY_DELIVER, CAPABILITY_ATTACH,
    CAPABILITY_OBSERVE, CAPABILITY_INSPECT, CAPABILITY_HEALTH,
    CAPABILITY_SESSION_HEALTH, CAPABILITY_REHEAL,
})


class RuntimeAdapterError(Exception):
    """Base error for all runtime adapter failures."""
    def __init__(self, message: str, adapter_type: str = "tmux", operation: str = ""):
        self.adapter_type = adapter_type
        self.operation = operation
        super().__init__(message)


class AdapterError(RuntimeAdapterError):
    """Base error for tmux adapter failures."""


class AdapterConfigError(RuntimeAdapterError):
    """Invalid configuration at init."""


class AdapterTransportError(RuntimeAdapterError):
    """Transport-level failure (tmux command failed)."""
    def __init__(self, message: str, transport_detail: str = "", **kwargs: Any):
        self.transport_detail = transport_detail
        super().__init__(message, **kwargs)


class UnsupportedCapability(RuntimeAdapterError):
    """Raised when an operation is invoked on an adapter that does not support it."""
    def __init__(self, operation: str, adapter_type: str = "tmux", reason: str = ""):
        self.reason = reason or f"{adapter_type} adapter does not support {operation}"
        super().__init__(self.reason, adapter_type=adapter_type, operation=operation)


class AdapterDisabledError(AdapterError):
    """Raised when the adapter is accessed while VNX_TMUX_ADAPTER_ENABLED=0."""


class PaneNotFoundError(AdapterError):
    """Raised when panes.json does not contain the requested terminal_id."""


class LeaseNotActiveError(AdapterError):
    """Raised when the target terminal does not hold an active lease for the dispatch."""


def _read_panes_json(panes_path: Path) -> Dict[str, Any]:
    """Return parsed panes.json content or empty dict."""
    if not panes_path.exists():
        return {}
    try:
        return json.loads(panes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_pane(terminal_id: str, panes_path: Path) -> PaneTarget:
    """Resolve terminal_id to PaneTarget using panes.json."""
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
    """Load content into tmux buffer and paste to pane."""
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
        rc = subprocess.run(["tmux", "load-buffer", "-"], input=content,
            capture_output=True, text=True, timeout=10).returncode
    if rc != 0:
        return rc
    return _run_tmux("paste-buffer", "-t", pane_id).returncode


class TmuxAdapter:
    """RuntimeAdapter implementation for tmux-based worker terminal delivery."""

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

    def resolve_target(self, terminal_id: str) -> PaneTarget:
        """Resolve terminal_id to PaneTarget via panes.json."""
        return resolve_pane(terminal_id, self._panes_path)

    def validate_lease(self, terminal_id: str, dispatch_id: str) -> None:
        """Soft pre-delivery lease check. No-ops if DB unavailable."""
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

    def adapter_type(self) -> str:
        return "tmux"

    def capabilities(self) -> frozenset:
        """Return supported capabilities for TmuxAdapter."""
        if not adapter_enabled():
            return frozenset()
        return TMUX_CAPABILITIES

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Create tmux pane for terminal. Idempotent."""
        try:
            target = self.resolve_target(terminal_id)
            return SpawnResult(success=True, transport_ref=target.pane_id)
        except PaneNotFoundError:
            pass
        session = config.get("session_name", "")
        work_dir = config.get("work_dir", "")
        if not session:
            return SpawnResult(success=False, error="session_name required in config")
        cmd = ["tmux", "split-window", "-t", session, "-c", work_dir or "."]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return SpawnResult(success=False, error=result.stderr.strip())
            return SpawnResult(success=True, transport_ref=result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return SpawnResult(success=False, error=str(e))

    def stop(self, terminal_id: str) -> StopResult:
        """Terminate tmux pane. Idempotent — stopping absent pane succeeds."""
        if not _tmux_available():
            return StopResult(success=True, was_running=False, error="tmux not available")
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError:
            return StopResult(success=True, was_running=False)
        result = _run_tmux("kill-pane", "-t", target.pane_id)
        return StopResult(success=True, was_running=result.returncode == 0)

    def attach(self, terminal_id: str) -> AttachResult:
        """Switch operator focus to terminal's pane."""
        if not _tmux_available():
            return AttachResult(success=False, error="tmux not available")
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError:
            return AttachResult(success=False, error=f"Terminal {terminal_id} not found")
        result = _run_tmux("select-pane", "-t", target.pane_id)
        if result.returncode != 0:
            return AttachResult(success=False, error=result.stderr.strip())
        return AttachResult(success=True)

    def observe(self, terminal_id: str) -> ObservationResult:
        """Read-only state probe without side effects."""
        if not _tmux_available():
            return ObservationResult(exists=False, error="tmux not available")
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError:
            return ObservationResult(exists=False)
        result = _run_tmux("display-message", "-t", target.pane_id, "-p", "#{pane_pid}")
        if result.returncode != 0:
            return ObservationResult(exists=False)
        pid = result.stdout.strip()
        return ObservationResult(
            exists=True, responsive=True,
            transport_state={"surface_exists": True, "process_alive": bool(pid), "pane_id": target.pane_id},
        )

    def inspect(self, terminal_id: str) -> InspectionResult:
        """Deep diagnostic inspection of terminal pane."""
        if not _tmux_available():
            return InspectionResult(exists=False, error="tmux not available")
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError:
            return InspectionResult(exists=False)
        content_result = _run_tmux("capture-pane", "-t", target.pane_id, "-p")
        pane_content = content_result.stdout if content_result.returncode == 0 else None
        pid_result = _run_tmux("display-message", "-t", target.pane_id, "-p", "#{pane_pid}")
        return InspectionResult(
            exists=True, transport_ref=target.pane_id,
            transport_details={"pane_id": target.pane_id, "pid": pid_result.stdout.strip()},
            pane_content=pane_content,
        )

    def health(self, terminal_id: str) -> HealthResult:
        """Fast health check (< 2s)."""
        if not _tmux_available():
            return HealthResult(healthy=False, surface_exists=False, error="tmux not available")
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError:
            return HealthResult(healthy=False, surface_exists=False)
        result = _run_tmux("display-message", "-t", target.pane_id, "-p", "#{pane_pid}")
        exists = result.returncode == 0
        pid = result.stdout.strip() if exists else ""
        alive = exists and bool(pid)
        return HealthResult(
            healthy=exists and alive, surface_exists=exists, process_alive=alive,
            details={"pane_id": target.pane_id, "pid": pid},
        )

    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult:
        """Aggregate health check (< 5s)."""
        terminals: Dict[str, HealthResult] = {}
        degraded: List[str] = []
        for tid in terminal_ids:
            h = self.health(tid)
            terminals[tid] = h
            if not h.healthy:
                degraded.append(tid)
        session_exists = any(h.surface_exists for h in terminals.values())
        return SessionHealthResult(
            session_exists=session_exists, terminals=terminals,
            degraded_terminals=degraded,
        )

    def reheal(self, terminal_id: str) -> RehealResult:
        """Re-establish pane mapping after drift using work_dir anchor."""
        try:
            target = self.resolve_target(terminal_id)
            old_ref = target.pane_id
        except PaneNotFoundError:
            old_ref = None
        panes = _read_panes_json(self._panes_path)
        entry = panes.get(terminal_id) or panes.get(terminal_id.upper()) or {}
        work_dir = entry.get("work_dir", "") if isinstance(entry, dict) else ""
        if not work_dir:
            return RehealResult(rehealed=False, old_ref=old_ref, strategy="work_dir",
                                error="No work_dir in panes.json for reheal")
        ok = remap_pane(terminal_id, "", self._panes_path, state_dir=self._state_dir)
        if ok:
            try:
                new_target = self.resolve_target(terminal_id)
                return RehealResult(rehealed=True, old_ref=old_ref, new_ref=new_target.pane_id, strategy="work_dir")
            except PaneNotFoundError:
                pass
        return RehealResult(rehealed=False, old_ref=old_ref, strategy="work_dir", error="Reheal failed")

    def shutdown(self, graceful: bool = True) -> None:
        """Clean up resources. No-op for TmuxAdapter (tmux session persists)."""
        pass

    def deliver(
        self,
        terminal_id: str,
        dispatch_id: str,
        attempt_id: Optional[str] = None,
        *,
        skill_command: Optional[str] = None,
        prompt: Optional[str] = None,
        actor: str = "adapter",
    ) -> DeliveryResult:
        """Deliver dispatch to terminal_id via primary or legacy path."""
        if not _tmux_available():
            return self._record_and_return(
                DeliveryResult(success=False, terminal_id=terminal_id,
                    dispatch_id=dispatch_id, pane_id=None, path_used="none",
                    failure_reason="tmux binary not found in PATH"),
                attempt_id=attempt_id, actor=actor,
            )
        try:
            target = self.resolve_target(terminal_id)
        except PaneNotFoundError as exc:
            return self._handle_pane_not_found(terminal_id, dispatch_id, attempt_id, exc, actor)

        if self._primary_path:
            return self._deliver_primary(target, dispatch_id, attempt_id, actor=actor)
        return self._deliver_legacy(target, dispatch_id, attempt_id,
            skill_command=skill_command or "", prompt=prompt or "", actor=actor)

    def _handle_pane_not_found(
        self, terminal_id: str, dispatch_id: str,
        attempt_id: Optional[str], exc: PaneNotFoundError, actor: str,
    ) -> DeliveryResult:
        self._emit_event("adapter_pane_not_found", dispatch_id=dispatch_id,
            terminal_id=terminal_id, attempt_id=attempt_id, reason=str(exc), actor=actor)
        return DeliveryResult(success=False, terminal_id=terminal_id,
            dispatch_id=dispatch_id, pane_id=None, path_used="none", failure_reason=str(exc))

    def _deliver_primary(
        self, target: PaneTarget, dispatch_id: str,
        attempt_id: Optional[str], *, actor: str,
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

    def _deliver_legacy(
        self, target: PaneTarget, dispatch_id: str,
        attempt_id: Optional[str], *, skill_command: str, prompt: str, actor: str,
    ) -> DeliveryResult:
        """Legacy: send skill via send-keys, prompt via paste-buffer."""
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

    def _emit_event(
        self, event_type: str, *, dispatch_id: str, terminal_id: str,
        attempt_id: Optional[str], reason: Optional[str] = None,
        actor: str = "adapter", metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta = {"terminal_id": terminal_id}
        if attempt_id:
            meta["attempt_id"] = attempt_id
        if metadata:
            meta.update(metadata)
        try:
            with get_connection(self._state_dir) as conn:
                _append_event(conn, event_type=event_type, entity_type="dispatch",
                    entity_id=dispatch_id, actor=actor, reason=reason, metadata=meta)
                conn.commit()
        except Exception:
            pass

    def _record_success(
        self, target: PaneTarget, dispatch_id: str,
        attempt_id: Optional[str], *, path: str, actor: str,
    ) -> DeliveryResult:
        self._emit_event("adapter_deliver_success", dispatch_id=dispatch_id,
            terminal_id=target.terminal_id, attempt_id=attempt_id,
            reason=f"delivery succeeded via {path} path", actor=actor,
            metadata={"path": path, "pane_id": target.pane_id})
        return DeliveryResult(success=True, terminal_id=target.terminal_id,
            dispatch_id=dispatch_id, pane_id=target.pane_id, path_used=path)

    def _record_failure(
        self, target: PaneTarget, dispatch_id: str,
        attempt_id: Optional[str], *, reason: str, path: str,
        actor: str, rc: Optional[int] = None,
    ) -> DeliveryResult:
        self._emit_event("adapter_deliver_failure", dispatch_id=dispatch_id,
            terminal_id=target.terminal_id, attempt_id=attempt_id,
            reason=reason, actor=actor, metadata={"path": path, "pane_id": target.pane_id})
        return DeliveryResult(success=False, terminal_id=target.terminal_id,
            dispatch_id=dispatch_id, pane_id=target.pane_id, path_used=path,
            failure_reason=reason, tmux_returncode=rc)

    def _record_and_return(
        self, result: DeliveryResult, *, attempt_id: Optional[str], actor: str,
    ) -> DeliveryResult:
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


@dataclass
class RemapResult:
    """Result of a remap_pane or reheal_panes operation."""
    remapped: List[str]    # terminal IDs that were remapped
    missing: List[str]     # terminal IDs not found in live tmux
    unchanged: List[str]   # terminal IDs whose pane_id was already correct
    panes_json_updated: bool = False


def _emit_remap_event(state_dir: Path, terminal_id: str, old_pane_id: str, new_pane_id: str) -> None:
    """Emit adapter_pane_remap coordination event. Non-fatal on failure."""
    try:
        from runtime_coordination import _append_event, get_connection
        with get_connection(state_dir) as conn:
            _append_event(conn, event_type="adapter_pane_remap", entity_type="terminal",
                entity_id=terminal_id, actor="tmux_adapter",
                reason=f"pane_id remapped from {old_pane_id!r} to {new_pane_id!r}",
                metadata={"old_pane_id": old_pane_id, "new_pane_id": new_pane_id})
            conn.commit()
    except Exception:
        pass


def remap_pane(
    terminal_id: str, new_pane_id: str, panes_path: Path,
    state_dir: Optional[Path] = None,
) -> bool:
    """Update panes.json with a new pane_id for terminal_id.

    Does NOT touch dispatch registry or lease state (A-R3, A-R4).
    Returns True if panes.json was updated; False if terminal was not found.
    """
    panes = _read_panes_json(panes_path)
    if not panes:
        return False

    updated = False
    old_pane_id = ""
    for key in (terminal_id, terminal_id.upper(), terminal_id.lower()):
        if key in panes:
            if not updated:
                old_pane_id = panes[key].get("pane_id", "")
            panes[key]["pane_id"] = new_pane_id
            updated = True

    for entry in panes.values():
        if isinstance(entry, dict) and entry.get("pane_id") == old_pane_id:
            entry["pane_id"] = new_pane_id
    for track_entry in panes.get("tracks", {}).values():
        if isinstance(track_entry, dict) and track_entry.get("pane_id") == old_pane_id:
            track_entry["pane_id"] = new_pane_id

    if not updated:
        return False
    try:
        panes_path.write_text(json.dumps(panes, indent=2), encoding="utf-8")
    except OSError:
        return False

    if state_dir is not None:
        _emit_remap_event(state_dir, terminal_id, old_pane_id, new_pane_id)
    return True


def _discover_live_panes(session_name: str) -> Dict[str, str]:
    """Query tmux for live pane_id -> work_dir mapping."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", session_name,
             "-F", "#{pane_id} #{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}
        panes: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                panes[parts[0]] = parts[1]
        return panes
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}


def reheal_panes(
    state_dir: Path, session_name: str, project_root: str = "",
) -> RemapResult:
    """Reconcile panes.json with live tmux state using work_dir anchor.

    Identity invariant (G-R4, A-R4): work_dir is the stable anchor.
    """
    panes_path = state_dir / "panes.json"
    panes = _read_panes_json(panes_path)
    if not panes:
        return RemapResult(remapped=[], missing=[], unchanged=[])

    live_panes = _discover_live_panes(session_name)
    live_by_dir: Dict[str, str] = {wdir: pid for pid, wdir in live_panes.items() if wdir}
    terms_base = str(Path(project_root) / ".claude" / "terminals") if project_root else ""

    remapped: List[str] = []
    missing: List[str] = []
    unchanged: List[str] = []
    panes_json_updated = False

    for tid in ("T0", "T1", "T2", "T3"):
        entry = panes.get(tid) or panes.get(tid.lower()) or {}
        if not isinstance(entry, dict):
            continue
        declared_pane_id = entry.get("pane_id", "")
        work_dir = entry.get("work_dir", "")
        if not work_dir and terms_base:
            work_dir = str(Path(terms_base) / tid)

        if declared_pane_id and declared_pane_id in live_panes:
            unchanged.append(tid)
            continue

        candidate = live_by_dir.get(work_dir, "")
        if candidate and remap_pane(tid, candidate, panes_path, state_dir=state_dir):
            remapped.append(tid)
            panes_json_updated = True
            panes = _read_panes_json(panes_path)
        else:
            missing.append(tid)

    return RemapResult(remapped=remapped, missing=missing,
        unchanged=unchanged, panes_json_updated=panes_json_updated)


def load_adapter(state_dir: str | Path) -> Optional["TmuxAdapter"]:
    """Return TmuxAdapter if enabled (default), else None."""
    if not adapter_enabled():
        return None
    return TmuxAdapter(state_dir)
