#!/usr/bin/env python3
"""VNX Subprocess Adapter — RuntimeAdapter implementation for headless claude CLI processes.

Spawns `claude -p --output-format stream-json` subprocesses instead of routing
through tmux panes. Each terminal_id maps to a tracked subprocess. All process
group management uses os.setsid / os.killpg for clean teardown.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

from adapter_types import (
    CAPABILITY_DELIVER,
    CAPABILITY_HEALTH,
    CAPABILITY_OBSERVE,
    CAPABILITY_SESSION_HEALTH,
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    DeliveryResult,
    HealthResult,
    ObservationResult,
    SessionHealthResult,
    SpawnResult,
    StopResult,
)



# ---------------------------------------------------------------------------
# StreamEvent dataclass
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """A single parsed event from --output-format stream-json stdout."""
    type: str          # init, thinking, tool_use, tool_result, text, result, error
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    session_id: Optional[str] = None  # extracted from init event


SUBPROCESS_CAPABILITIES = frozenset({
    CAPABILITY_SPAWN,
    CAPABILITY_STOP,
    CAPABILITY_DELIVER,
    CAPABILITY_OBSERVE,
    CAPABILITY_HEALTH,
    CAPABILITY_SESSION_HEALTH,
})


class SubprocessAdapter:
    """RuntimeAdapter implementation for headless subprocess-based terminals.

    Lifecycle:
      spawn()   — registers config, no subprocess started yet
      deliver() — spawns a claude subprocess with the dispatch instruction
      stop()    — sends SIGTERM (escalates to SIGKILL on timeout)
      observe() — non-blocking poll of process state
      health()  — fast alive check
      session_health() — aggregate across multiple terminal IDs
      shutdown() — stops all tracked processes
    """

    def __init__(self) -> None:
        # terminal_id -> subprocess.Popen
        self._processes: Dict[str, subprocess.Popen] = {}
        # terminal_id -> spawn config (preserved for re-spawn if needed)
        self._configs: Dict[str, Dict[str, Any]] = {}
        # terminal_id -> session_id extracted from init event
        self._session_ids: Dict[str, str] = {}
        # terminal_id -> dispatch_id from most recent deliver()
        self._dispatch_ids: Dict[str, str] = {}
        # Lazy-loaded EventStore (optional dependency)
        self._event_store = None
        self._event_store_loaded = False

    def _get_event_store(self):
        """Lazy-load EventStore. Returns None if not available."""
        if not self._event_store_loaded:
            self._event_store_loaded = True
            try:
                from event_store import EventStore
                self._event_store = EventStore()
                logger.info("subprocess_adapter: EventStore loaded for stream persistence")
            except ImportError:
                logger.debug("subprocess_adapter: EventStore not available (optional)")
        return self._event_store

    def adapter_type(self) -> str:
        return "subprocess"

    def capabilities(self) -> frozenset:
        return SUBPROCESS_CAPABILITIES

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    def spawn(self, terminal_id: str, config: Dict[str, Any]) -> SpawnResult:
        """Register terminal config. Does not start a subprocess yet."""
        self._configs[terminal_id] = config
        return SpawnResult(
            success=True,
            transport_ref=f"subprocess:{terminal_id}",
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop(self, terminal_id: str) -> StopResult:
        """Terminate subprocess for terminal_id. SIGTERM → SIGKILL on timeout."""
        process = self._processes.get(terminal_id)
        if process is None:
            return StopResult(success=True, was_running=False)

        was_running = process.poll() is None
        if was_running:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
            except (OSError, ProcessLookupError):
                # Process already gone — treat as success
                pass

        self._processes.pop(terminal_id, None)
        return StopResult(success=True, was_running=was_running)

    # ------------------------------------------------------------------
    # Deliver
    # ------------------------------------------------------------------

    def deliver(
        self,
        terminal_id: str,
        dispatch_id: str,
        attempt_id: Optional[str] = None,
        *,
        instruction: Optional[str] = None,
        model: Optional[str] = None,
        resume_session: Optional[str] = None,
        cwd: Optional[Any] = None,
        **kwargs: Any,
    ) -> DeliveryResult:
        """Spawn a claude subprocess with the dispatch instruction.

        instruction and model can be passed directly or pulled from the stored
        config registered via spawn(). dispatch_id is always appended to the
        instruction so the subprocess can identify its work.

        resume_session: if provided, adds --resume <session_id> to the CLI
        command for session continuity.

        cwd: if provided, the subprocess is started in that directory.  Pass
        the agent's project directory (e.g. agents/{role}/) so the headless
        process has the right working context.
        """
        config = self._configs.get(terminal_id, {})
        effective_instruction = instruction or config.get("instruction", dispatch_id)
        effective_model = model or config.get("model", "sonnet")

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", effective_model,
        ]
        if resume_session:
            cmd.extend(["--resume", resume_session])
        cmd.append(effective_instruction)

        popen_kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "preexec_fn": os.setsid,  # new process group for clean SIGKILL
        }
        if cwd is not None:
            popen_kwargs["cwd"] = str(cwd)

        try:
            process = subprocess.Popen(cmd, **popen_kwargs)
        except (FileNotFoundError, OSError) as exc:
            return DeliveryResult(
                success=False,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pane_id=None,
                path_used="none",
                failure_reason=str(exc),
            )

        # Replace any prior process tracking (stop old one first)
        if terminal_id in self._processes:
            old = self._processes[terminal_id]
            if old.poll() is None:
                try:
                    os.killpg(os.getpgid(old.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass

        self._processes[terminal_id] = process
        self._dispatch_ids[terminal_id] = dispatch_id

        # Archive previous dispatch events, then clear for new dispatch
        es = self._get_event_store()
        if es is not None:
            last = es.last_event(terminal_id)
            prev_dispatch_id = last.get("dispatch_id") if last else None
            es.clear(terminal_id, archive_dispatch_id=prev_dispatch_id or None)

        return DeliveryResult(
            success=True,
            terminal_id=terminal_id,
            dispatch_id=dispatch_id,
            pane_id=None,
            path_used="subprocess",
        )

    # ------------------------------------------------------------------
    # Stream event parsing & normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_cli_event(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Normalize a raw CLI stream-json event to dashboard-friendly format.

        The CLI emits types like ``system``, ``assistant``, ``user`` with nested
        content blocks.  The dashboard expects flat semantic types: ``init``,
        ``thinking``, ``tool_use``, ``tool_result``, ``text``, ``result``,
        ``error``.

        A single ``assistant`` event may contain multiple content blocks, so
        this method can return more than one normalized event.

        Events that are already in dashboard format (e.g. test fixtures) pass
        through unchanged.
        """
        event_type = payload.get("type", "")
        event_subtype = payload.get("subtype", "")

        # system + init → init
        if event_type == "system" and event_subtype == "init":
            return [{"type": "init", "data": {
                "session_id": payload.get("session_id"),
                "model": payload.get("model"),
            }}]

        # assistant → split by content block type
        if event_type == "assistant":
            message = payload.get("message", {})
            content_blocks = message.get("content", [])
            normalized: List[Dict[str, Any]] = []
            for block in content_blocks:
                block_type = block.get("type", "")
                if block_type == "thinking":
                    normalized.append({"type": "thinking", "data": {
                        "thinking": block.get("thinking", ""),
                    }})
                elif block_type == "tool_use":
                    normalized.append({"type": "tool_use", "data": {
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                        "id": block.get("id", ""),
                    }})
                elif block_type == "text":
                    normalized.append({"type": "text", "data": {
                        "text": block.get("text", ""),
                    }})
            return normalized

        # user → extract tool_result blocks
        if event_type == "user":
            message = payload.get("message", {})
            content_blocks = message.get("content", [])
            normalized = []
            for block in content_blocks:
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        text_parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                        content = "\n".join(text_parts)
                    normalized.append({"type": "tool_result", "data": {
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": content,
                    }})
            return normalized

        # result → result (extract useful fields)
        if event_type == "result":
            return [{"type": "result", "data": {
                "text": payload.get("result", ""),
                "subtype": event_subtype,
                "session_id": payload.get("session_id"),
            }}]

        # rate_limit_event → skip (not dashboard-relevant)
        if event_type == "rate_limit_event":
            return []

        # Already-normalized events (test fixtures, future formats) — pass through
        return [{"type": event_type, "data": payload}]

    def read_events(self, terminal_id: str) -> Iterator[StreamEvent]:
        """Yield parsed StreamEvents from subprocess stdout line-by-line.

        Reads until EOF (process exited or pipe closed). Malformed NDJSON lines
        are logged as warnings and skipped — they do not raise.

        Raw CLI events are normalized to dashboard-friendly types before
        yielding and persisting.  The first ``init`` event's session_id is
        stored and retrievable via get_session_id().
        """
        process = self._processes.get(terminal_id)
        if process is None or process.stdout is None:
            return

        for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "subprocess_adapter: malformed NDJSON line for %s (skipped): %r",
                    terminal_id,
                    line[:200],
                )
                continue

            # Extract session_id before normalization (both CLI and test formats)
            event_type = payload.get("type", "")
            event_subtype = payload.get("subtype", "")
            session_id: Optional[str] = payload.get("session_id")

            is_init = (
                (event_type == "system" and event_subtype == "init")
                or event_type == "init"
            )
            if is_init and session_id and terminal_id not in self._session_ids:
                self._session_ids[terminal_id] = session_id

            # Normalize CLI event to dashboard-friendly format
            normalized_events = self._normalize_cli_event(payload)

            dispatch_id = self._dispatch_ids.get(terminal_id, "")
            es = self._get_event_store()

            for norm in normalized_events:
                if es is not None:
                    es.append(terminal_id, norm, dispatch_id=dispatch_id)
                yield StreamEvent(
                    type=norm["type"],
                    data=norm.get("data", {}),
                    session_id=session_id if is_init else None,
                )

    def read_events_with_timeout(
        self,
        terminal_id: str,
        chunk_timeout: float = 300.0,
        total_deadline: float = 900.0,
    ) -> Iterator[StreamEvent]:
        """Like read_events() but with timeout protection.

        chunk_timeout: max seconds to wait for the next line of output.
            Default 300s. Override with VNX_CHUNK_TIMEOUT env var.
        total_deadline: max total seconds for the entire read.
            Default 900s. Override with VNX_TOTAL_DEADLINE env var.

        On timeout, the subprocess is killed via stop() and iteration ends.
        """
        try:
            chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
        except (KeyError, ValueError):
            pass
        try:
            total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
        except (KeyError, ValueError):
            pass
        process = self._processes.get(terminal_id)
        if process is None or process.stdout is None:
            return

        start_time = time.time()
        fd = process.stdout.fileno()

        while True:
            elapsed = time.time() - start_time
            if elapsed > total_deadline:
                logger.warning(
                    "read_events_with_timeout: total deadline (%.0fs) exceeded for %s",
                    total_deadline, terminal_id,
                )
                self.stop(terminal_id)
                break

            remaining = min(chunk_timeout, total_deadline - elapsed)
            ready, _, _ = select.select([fd], [], [], remaining)

            if not ready:
                logger.warning(
                    "read_events_with_timeout: chunk timeout (%.0fs) for %s",
                    chunk_timeout, terminal_id,
                )
                self.stop(terminal_id)
                break

            raw_line = process.stdout.readline()
            if not raw_line:
                break  # EOF — process exited

            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "subprocess_adapter: malformed NDJSON line for %s (skipped): %r",
                    terminal_id, line[:200],
                )
                continue

            # Extract session_id before normalization
            event_type = payload.get("type", "")
            event_subtype = payload.get("subtype", "")
            session_id: Optional[str] = payload.get("session_id")

            is_init = (
                (event_type == "system" and event_subtype == "init")
                or event_type == "init"
            )
            if is_init and session_id and terminal_id not in self._session_ids:
                self._session_ids[terminal_id] = session_id

            # Normalize and yield
            normalized_events = self._normalize_cli_event(payload)
            dispatch_id = self._dispatch_ids.get(terminal_id, "")
            es = self._get_event_store()

            for norm in normalized_events:
                if es is not None:
                    es.append(terminal_id, norm, dispatch_id=dispatch_id)
                yield StreamEvent(
                    type=norm["type"],
                    data=norm.get("data", {}),
                    session_id=session_id if is_init else None,
                )

    def get_session_id(self, terminal_id: str) -> Optional[str]:
        """Return session_id extracted from the init event, or None.

        Will be None if read_events() has not yet yielded the init event for
        this terminal_id.
        """
        return self._session_ids.get(terminal_id)

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, terminal_id: str) -> ObservationResult:
        """Non-blocking process state probe."""
        process = self._processes.get(terminal_id)
        if process is None:
            # Check if config was registered (spawned but not yet delivered)
            exists = terminal_id in self._configs
            return ObservationResult(
                exists=exists,
                responsive=False,
                transport_state={"surface_exists": exists, "process_alive": False},
            )

        alive = process.poll() is None
        return ObservationResult(
            exists=True,
            responsive=alive,
            transport_state={
                "surface_exists": True,
                "process_alive": alive,
                "pid": process.pid,
                "returncode": process.returncode,
            },
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self, terminal_id: str) -> HealthResult:
        """Fast health check — O(1), no blocking."""
        process = self._processes.get(terminal_id)
        if process is None:
            surface = terminal_id in self._configs
            return HealthResult(
                healthy=False,
                surface_exists=surface,
                process_alive=False,
                details={"terminal_id": terminal_id, "has_process": False},
            )

        alive = process.poll() is None
        return HealthResult(
            healthy=alive,
            surface_exists=True,
            process_alive=alive,
            details={
                "terminal_id": terminal_id,
                "pid": process.pid,
                "returncode": process.returncode,
            },
        )

    def session_health(self, terminal_ids: List[str]) -> SessionHealthResult:
        """Aggregate health across multiple terminal IDs."""
        terminals: Dict[str, HealthResult] = {}
        degraded: List[str] = []
        for tid in terminal_ids:
            h = self.health(tid)
            terminals[tid] = h
            if not h.healthy:
                degraded.append(tid)
        session_exists = any(h.surface_exists for h in terminals.values())
        return SessionHealthResult(
            session_exists=session_exists,
            terminals=terminals,
            degraded_terminals=degraded,
        )

    # ------------------------------------------------------------------
    # Unsupported optional operations
    # ------------------------------------------------------------------

    def attach(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import AttachResult, UnsupportedCapability
        raise UnsupportedCapability("attach", adapter_type="subprocess")

    def inspect(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import InspectionResult, UnsupportedCapability
        raise UnsupportedCapability("inspect", adapter_type="subprocess")

    def reheal(self, terminal_id: str):  # type: ignore[return]
        from adapter_types import RehealResult, UnsupportedCapability
        raise UnsupportedCapability("reheal", adapter_type="subprocess")

    # ------------------------------------------------------------------
    # Auto-Report Pipeline Trigger
    # ------------------------------------------------------------------

    def trigger_report_pipeline(
        self,
        terminal_id: str,
        dispatch_id: str,
        *,
        cwd: Optional[str] = None,
        project_root: Optional[str] = None,
        gate: str = "",
        track: str = "",
        pr_id: str = "",
    ) -> bool:
        """Write an extraction trigger file for the auto-report pipeline.

        Mirrors the stop_report_hook.sh output so subprocess completions feed
        the same pipeline as interactive session Stop hook firings.

        Gate: only runs when VNX_AUTO_REPORT=1 is set. Returns True when trigger
        file is written, False when skipped or on error.

        Non-blocking — does not invoke any subprocess or LLM.
        """
        if os.environ.get("VNX_AUTO_REPORT", "0") != "1":
            return False

        terminal_map = {"T1": "A", "T2": "B", "T3": "C"}
        if terminal_id not in terminal_map:
            logger.debug(
                "trigger_report_pipeline: %s is not a worker terminal, skipping",
                terminal_id,
            )
            return False

        # Resolve project root
        if project_root is None:
            vnx_data_env = os.environ.get("VNX_DATA_DIR", "")
            if vnx_data_env:
                project_root = str(Path(vnx_data_env).parent)
            else:
                # Walk up from this file to find .vnx-data
                search = Path(__file__).resolve()
                for _ in range(6):
                    search = search.parent
                    if (search / ".vnx-data").is_dir():
                        project_root = str(search)
                        break

        if project_root is None:
            logger.warning(
                "trigger_report_pipeline: could not resolve project root for %s",
                terminal_id,
            )
            return False

        vnx_data_dir = os.environ.get("VNX_DATA_DIR", str(Path(project_root) / ".vnx-data"))
        pipeline_dir = Path(vnx_data_dir) / "state" / "report_pipeline"
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        # Infer track from terminal if not provided
        effective_track = track or terminal_map.get(terminal_id, "")

        session_id = self._session_ids.get(terminal_id, "")
        effective_cwd = cwd or str(Path(project_root) / ".claude" / "terminals" / terminal_id)

        trigger = {
            "trigger_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dispatch_id": dispatch_id,
            "terminal": terminal_id,
            "track": effective_track,
            "gate": gate,
            "pr_id": pr_id,
            "session_id": session_id,
            "transcript_path": "",
            "cwd": effective_cwd,
            "project_root": project_root,
            "source": "subprocess",
        }

        trigger_file = pipeline_dir / f"{dispatch_id}.trigger.json"
        try:
            trigger_file.write_text(json.dumps(trigger, indent=2))
            logger.info(
                "trigger_report_pipeline: wrote trigger for %s → %s",
                dispatch_id,
                trigger_file,
            )
            return True
        except OSError as exc:
            logger.warning(
                "trigger_report_pipeline: failed to write trigger for %s: %s",
                dispatch_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self, graceful: bool = True) -> None:
        """Stop all tracked subprocesses."""
        for terminal_id in list(self._processes.keys()):
            self.stop(terminal_id)
