#!/usr/bin/env python3
"""adapters/claude_adapter.py — ClaudeAdapter wrapping subprocess_dispatch.

Refactors deliver_with_recovery() into the ProviderAdapter interface.
All existing behavior is preserved: event streaming, heartbeat, receipt writing,
auto-commit, skill injection, permission profile injection.

BILLING SAFETY: No Anthropic SDK. Only subprocess.Popen(["claude", ...]).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_adapter import AdapterResult, Capability, ProviderAdapter

logger = logging.getLogger(__name__)


class ClaudeAdapter(ProviderAdapter):
    """Provider adapter for Claude CLI (subprocess delivery).

    Wraps the existing subprocess_dispatch.deliver_with_recovery() so all
    existing dispatch behavior is preserved unchanged.  Caller passes context
    dict with optional keys documented on ProviderAdapter.execute().
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id

    # ------------------------------------------------------------------
    # ProviderAdapter interface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return "claude"

    def capabilities(self) -> set[Capability]:
        return {Capability.CODE, Capability.REVIEW, Capability.DECISION, Capability.DIGEST}

    def is_available(self) -> bool:
        """Return True when 'claude' binary is found on PATH."""
        return shutil.which("claude") is not None

    def execute(self, instruction: str, context: dict) -> AdapterResult:
        """Deliver instruction via subprocess_dispatch.deliver_with_recovery().

        Maps context dict keys to deliver_with_recovery() parameters.
        Returns AdapterResult with status, event counts, and commit info.
        """
        from subprocess_dispatch import deliver_with_recovery  # noqa: PLC0415

        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        model = context.get("model", os.environ.get("VNX_DISPATCH_MODEL", "sonnet"))
        role = context.get("role")
        lease_generation = context.get("lease_generation")
        heartbeat_interval = float(context.get("heartbeat_interval", 300.0))
        chunk_timeout = float(context.get("chunk_timeout", 120.0))
        total_deadline = float(context.get("total_deadline", 600.0))
        auto_commit = bool(context.get("auto_commit", True))
        gate = context.get("gate", "")
        max_retries = int(context.get("max_retries", 1))

        t0 = time.monotonic()
        success = deliver_with_recovery(
            terminal_id=terminal_id,
            instruction=instruction,
            model=model,
            dispatch_id=dispatch_id,
            role=role,
            max_retries=max_retries,
            lease_generation=lease_generation,
            heartbeat_interval=heartbeat_interval,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
            auto_commit=auto_commit,
            gate=gate,
        )
        duration = time.monotonic() - t0

        return AdapterResult(
            status="done" if success else "failed",
            output="",          # subprocess_dispatch drains stdout internally
            events=[],          # events are persisted to EventStore internally
            event_count=0,      # not tracked at this layer
            duration_seconds=duration,
            committed=False,    # commit state tracked inside deliver_with_recovery
            commit_hash=None,
            report_path=None,
            provider="claude",
            model=model,
        )

    def stream_events(self, instruction: str, context: dict) -> Iterator[dict]:
        """Stream events directly via SubprocessAdapter (bypasses recovery loop).

        Useful for callers that want raw event access.  Does not write receipts
        or handle retries — use execute() for full lifecycle management.
        """
        from subprocess_adapter import SubprocessAdapter  # noqa: PLC0415
        from subprocess_dispatch import (  # noqa: PLC0415
            _inject_skill_context,
            _inject_permission_profile,
            _resolve_agent_cwd,
        )

        terminal_id = context.get("terminal_id", self._terminal_id)
        dispatch_id = context.get("dispatch_id", "unknown")
        model = context.get("model", os.environ.get("VNX_DISPATCH_MODEL", "sonnet"))
        role = context.get("role")
        chunk_timeout = float(context.get("chunk_timeout", 120.0))
        total_deadline = float(context.get("total_deadline", 600.0))

        full_instruction = _inject_skill_context(
            terminal_id,
            instruction,
            role=role,
            dispatch_metadata={"dispatch_id": dispatch_id, "model": model},
        )
        full_instruction = _inject_permission_profile(terminal_id, role, full_instruction)
        agent_cwd = _resolve_agent_cwd(role)

        adapter = SubprocessAdapter()
        result = adapter.deliver(
            terminal_id,
            dispatch_id,
            instruction=full_instruction,
            model=model,
            cwd=agent_cwd,
        )
        if not result.success:
            return

        for event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            yield {"type": event.type, "data": event.data}
