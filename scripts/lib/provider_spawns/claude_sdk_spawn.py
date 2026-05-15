"""claude_sdk_spawn.py — Anthropic Agent SDK direct-API spawn handler (opt-in, non-governed).

Wave 4.6 PR-4.6.7. This is the ONLY module in VNX that imports `anthropic`.
ADR-003 amended: API-key + SDK permitted for opt-in non-governed use cases.

BILLING SAFETY: SDK is API-key billed (per-token), NOT OAuth subscription.
This module REFUSES OAuth credentials at runtime.

Use cases: sandbox experiments, fast iteration without audit-isolation requirements.
NOT a replacement for claude -p subprocess on the governed worker dispatch pad.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

# ADR-003 amendment explicitly permits SDK import IN THIS MODULE ONLY.
# CI gate scripts/check_adr_003_no_sdk_imports.py allows this path.
try:
    # vnx:allow-anthropic-sdk-import-with-justification
    import anthropic
except ImportError:
    anthropic = None  # graceful fallback; spawn returns error result

from canonical_event import CanonicalEvent  # noqa: E402 (path-setup above)

logger = logging.getLogger(__name__)

_OAUTH_TOKEN_PREFIX = "sk-ant-oat"


@dataclass
class ClaudeSDKSpawnResult:
    """Return value from spawn_claude_sdk(); carries spawn outcome back to the caller."""

    returncode: int
    completion_text: str
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    event_writer_failures: int = 0
    error: Optional[str] = None


def spawn_claude_sdk(
    prompt: str,
    model: str,
    dispatch_id: str,
    terminal_id: str,
    *,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
    **kwargs: Any,
) -> ClaudeSDKSpawnResult:
    """Spawn Anthropic Agent SDK direct-API call and consume streamed text.

    REQUIRES: ANTHROPIC_API_KEY env var. REFUSES OAuth credential (sk-ant-oat prefix).
    Returns ClaudeSDKSpawnResult. Caller manages lease/manifest/receipt.

    Parameters mirror spawn_claude() for provider-agnostic parity. Not all are
    used (no subprocess == no cwd/resume_session/skip_permissions).
    """
    if anthropic is None:
        return ClaudeSDKSpawnResult(
            returncode=127,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error="anthropic SDK not installed (pip install anthropic)",
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ClaudeSDKSpawnResult(
            returncode=78,  # EX_CONFIG
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error="ANTHROPIC_API_KEY required for claude_sdk provider (OAuth refused per ADR-003)",
        )
    if (api_key or "").startswith(_OAUTH_TOKEN_PREFIX):
        return ClaudeSDKSpawnResult(
            returncode=78,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error=(
                "OAuth credential detected — ADR-003 forbids OAuth + SDK combination. "
                "Use an API key."
            ),
        )

    client = anthropic.Anthropic(api_key=api_key)
    events_count = 0
    completion_chunks: list[str] = []
    event_writer_failures = 0
    start_time = time.monotonic()
    usage: Optional[Dict[str, Any]] = None

    try:
        with client.messages.stream(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text_chunk in stream.text_stream:
                completion_chunks.append(text_chunk)
                events_count += 1

                event = CanonicalEvent(
                    dispatch_id=dispatch_id,
                    terminal_id=terminal_id,
                    provider="claude_sdk",
                    event_type="text",
                    data={"text": text_chunk},
                    sequence=events_count,
                    model=model,
                )

                if event_writer is not None:
                    try:
                        event_writer(terminal_id, event, dispatch_id=dispatch_id)
                    except Exception as exc:
                        logger.error("spawn_claude_sdk: event_writer failure: %s", exc)
                        event_writer_failures += 1

                if on_event is not None and on_event(event) is False:
                    return ClaudeSDKSpawnResult(
                        returncode=0,
                        completion_text="".join(completion_chunks),
                        events_written=events_count,
                        session_id=None,
                        timed_out=False,
                        stopped_early=True,
                        event_writer_failures=event_writer_failures,
                    )

                if (time.monotonic() - start_time) > total_deadline:
                    return ClaudeSDKSpawnResult(
                        returncode=124,  # timeout exit code
                        completion_text="".join(completion_chunks),
                        events_written=events_count,
                        session_id=None,
                        timed_out=True,
                        event_writer_failures=event_writer_failures,
                    )

            final_message = stream.get_final_message()
            usage = {
                "input_tokens": final_message.usage.input_tokens,
                "output_tokens": final_message.usage.output_tokens,
            }

            complete_event = CanonicalEvent(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="claude_sdk",
                event_type="complete",
                data={
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                },
                sequence=events_count + 1,
                model=model,
                tokens_input=usage["input_tokens"],
                tokens_output=usage["output_tokens"],
            )
            if event_writer is not None:
                try:
                    event_writer(terminal_id, complete_event, dispatch_id=dispatch_id)
                except Exception as exc:
                    logger.error("spawn_claude_sdk: event_writer failure on complete: %s", exc)
                    event_writer_failures += 1

    except anthropic.APIError as exc:
        return ClaudeSDKSpawnResult(
            returncode=1,
            completion_text="".join(completion_chunks),
            events_written=events_count,
            session_id=None,
            timed_out=False,
            event_writer_failures=event_writer_failures,
            error=f"anthropic API error: {exc}",
        )

    return ClaudeSDKSpawnResult(
        returncode=0,
        completion_text="".join(completion_chunks),
        events_written=events_count,
        session_id=None,
        timed_out=False,
        token_usage=usage,
        event_writer_failures=event_writer_failures,
    )
