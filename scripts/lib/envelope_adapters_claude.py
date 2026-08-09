"""envelope_adapters_claude.py — codex + claude-subprocess adapters for the
dispatch_envelope module family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-5 of the dispatch-monolith-split
(dispatch-monolith-split, PR-5 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional

from envelope_types import EnvelopeSpec, _AdapterResult


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Wraps spawn_codex — reuses existing spawn without reimplementation.

    event_writer is not wired in PR-1 (no EventStore setup here); the audit
    stream gap is documented in Open Items and closed in a later PR.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.codex_spawn import spawn_codex  # noqa: PLC0415

        try:
            result = spawn_codex(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"codex spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = getattr(result, "token_usage", None)
        if isinstance(raw_usage, dict):
            token_usage = {
                "input": int(
                    raw_usage.get("input_tokens", raw_usage.get("input", 0)) or 0
                ),
                "output": int(
                    raw_usage.get("output_tokens", raw_usage.get("output", 0)) or 0
                ),
                "cache_hit": int(
                    raw_usage.get(
                        "cache_read_tokens", raw_usage.get("cache_hit", 0)
                    ) or 0
                ),
            }

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
                event_writer_failures=result.event_writer_failures,
                model=spec.model,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                event_writer_failures=result.event_writer_failures,
                model=spec.model,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            event_writer_failures=result.event_writer_failures,
            model=spec.model,
        )


class ClaudeSubprocessAdapter:
    """Wraps spawn_claude — reuses existing spawn without reimplementation.

    Maps ClaudeSpawnResult fields to _AdapterResult for the envelope GOVERN
    phase.  Worker reports remain the primary audit artifact; completion_text
    is now also captured from spawn_claude for benchmark and utility callers
    that need the model's raw text output.

    event_writer is not wired in PR-2 (SubprocessAdapter handles EventStore
    internally); the audit stream gap is documented in Open Items.
    """

    def run(
        self,
        spec: EnvelopeSpec,
        event_writer: Optional[Callable] = None,
        cwd: Optional[Path] = None,
    ) -> _AdapterResult:
        from provider_spawns.claude_spawn import spawn_claude  # noqa: PLC0415

        try:
            result = spawn_claude(
                prompt=spec.instruction,
                model=spec.model,
                dispatch_id=spec.dispatch_id,
                terminal_id=spec.terminal_id,
                event_writer=event_writer,
                cwd=cwd,
                role=spec.role,
                total_deadline=float(spec.deadline_seconds),
            )
        except BrokenPipeError as exc:
            return _AdapterResult(
                returncode=1,
                completion_text="",
                status="failure",
                error=f"claude spawn BrokenPipeError: {exc}",
            )

        token_usage: Dict[str, int] = {}
        raw_usage = result.token_usage
        if isinstance(raw_usage, dict) and raw_usage:
            token_usage = {
                "input": int(raw_usage.get("input_tokens", 0) or 0),
                "output": int(raw_usage.get("output_tokens", 0) or 0),
                "cache_hit": int(
                    raw_usage.get("cache_read_input_tokens", 0) or 0
                ),
            }
        model_used = getattr(result, "model", None) or spec.model

        if result.error:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="failure",
                token_usage=token_usage,
                error=result.error,
                session_id=result.session_id,
                model=model_used,
            )
        if result.timed_out:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="timeout",
                token_usage=token_usage,
                timed_out=True,
                session_id=result.session_id,
                model=model_used,
            )
        if result.stopped_early:
            return _AdapterResult(
                returncode=result.returncode,
                completion_text=(result.completion_text or ""),
                status="success",
                token_usage=token_usage,
                session_id=result.session_id,
                model=model_used,
            )
        status = "success" if result.returncode == 0 else "failure"
        return _AdapterResult(
            returncode=result.returncode,
            completion_text=(result.completion_text or ""),
            status=status,
            token_usage=token_usage,
            session_id=result.session_id,
            model=model_used,
        )
