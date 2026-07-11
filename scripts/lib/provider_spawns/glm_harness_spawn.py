"""glm_harness_spawn.py — Governed GLM-harness execution lane (GLM in Claude Code).

Runs GLM through the FULL `claude` CLI harness (tools, agentic loop, system prompt) —
the same harness the claude/deepseek-harness lanes get — instead of the simpler one-shot
litellm tool-loop. This lets the benchmark measure "same model, harness vs simple API
call". Mirrors deepseek_harness_spawn (claude CLI + ANTHROPIC_BASE_URL redirect + key-auth),
but the endpoint is a LOCAL litellm proxy that fronts OpenRouter GLM:

  ANTHROPIC_BASE_URL=http://localhost:4141            (local litellm proxy /v1/messages)
  ANTHROPIC_AUTH_TOKEN=<proxy master key>             (local-only bearer; NOT OAuth)
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  --strict-mcp-config --mcp-config '{"mcpServers":{}}'

The proxy routes to openrouter/z-ai/glm-5.2 etc., so inference flows through OpenRouter:
`zai-via-openrouter-only` stays intact (no direct z.ai/Zhipu account), and it's the CLI
not the SDK (`no-anthropic-sdk` intact). Account-safe: ANTHROPIC_API_KEY is scrubbed and
the key-auth bearer overrides the OAuth subscription, so api.anthropic.com is never hit.

FAIL-CLOSED: refuses to spawn if the local proxy is not reachable (a clear error beats a
confusing claude-CLI DNF against a dead endpoint).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns.claude_spawn import spawn_claude  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_GLM_PROXY_URL = "http://localhost:4141"
DEFAULT_GLM_PROXY_KEY = "sk-glm-harness-local"
DEFAULT_GLM_HARNESS_MODEL = "glm-5.2"
MCP_OFF_CONFIG = '{"mcpServers":{}}'
# CLAUDE_CODE_OAUTH_TOKEN scrubbed alongside ANTHROPIC_API_KEY (audit S3) — mirrors
# deepseek_harness_spawn's _HARNESS_SCRUB_KEYS: an inherited OAuth token must not survive
# into this redirected CLI either.
_HARNESS_SCRUB_KEYS: frozenset = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})


def resolve_harness_model(model: Optional[str] = None) -> str:
    """GLM model id for the proxy. Explicit arg → VNX_GLM_HARNESS_MODEL → glm-5.2."""
    explicit = (model or "").strip()
    if explicit and explicit != "sonnet":
        return explicit
    env_model = (os.environ.get("VNX_GLM_HARNESS_MODEL", "") or "").strip()
    return env_model or DEFAULT_GLM_HARNESS_MODEL


def _proxy_url() -> str:
    return (os.environ.get("VNX_GLM_PROXY_URL", "") or "").strip() or DEFAULT_GLM_PROXY_URL


def _proxy_key() -> str:
    return (os.environ.get("VNX_GLM_PROXY_KEY", "") or "").strip() or DEFAULT_GLM_PROXY_KEY


def build_harness_env() -> Dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": _proxy_url(),
        "ANTHROPIC_AUTH_TOKEN": _proxy_key(),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def build_harness_cli_args() -> List[str]:
    return ["--mcp-config", MCP_OFF_CONFIG, "--strict-mcp-config"]


def _proxy_reachable(url: str, timeout: float = 3.0) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class GLMHarnessSpawnResult:
    returncode: int
    completion: Dict[str, Any]
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    model: str = DEFAULT_GLM_HARNESS_MODEL
    stopped_early: bool = False
    error: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None
    _adapter: Any = field(default=None, repr=False)

    @property
    def completion_text(self) -> str:
        if not isinstance(self.completion, dict):
            return ""
        return self.completion.get("text", "") or ""

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        return {
            "provider": "glm-harness",
            "sub_provider": "zai",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input_tokens", usage.get("input", 0)) or 0),
                "output": int(usage.get("output_tokens", usage.get("output", 0)) or 0),
                "cache_read": int(
                    usage.get("cache_read_input_tokens", usage.get("cache_read", 0)) or 0
                ),
            },
        }


def spawn_glm_harness(
    prompt: str,
    model: Optional[str],
    dispatch_id: str,
    terminal_id: str,
    *,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    **kwargs: Any,
) -> GLMHarnessSpawnResult:
    """Spawn the claude CLI driving GLM via the local litellm→OpenRouter proxy."""
    resolved_model = resolve_harness_model(model)
    url = _proxy_url()
    if not _proxy_reachable(url):
        logger.error("spawn_glm_harness: litellm proxy not reachable at %s — refusing to spawn.", url)
        return GLMHarnessSpawnResult(
            returncode=1, completion={}, events_written=0, session_id=None,
            timed_out=False, model=resolved_model,
            error=f"glm-harness proxy unreachable at {url} (start the litellm proxy first)",
        )

    # Mandatory harness env wins over caller extra_env so the redirect/auth cannot be overridden.
    merged_env: Dict[str, str] = dict(extra_env or {})
    merged_env.update(build_harness_env())

    claude_result = spawn_claude(
        prompt=prompt,
        model=resolved_model,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        event_writer=event_writer,
        health_monitor=health_monitor,
        on_event=on_event,
        extra_env=merged_env,
        extra_cli_args=build_harness_cli_args(),
        cwd=cwd,
        scrub_env_keys=_HARNESS_SCRUB_KEYS,
        **kwargs,
    )
    # Harden the flaky OpenRouter→GLM lane: a returncode-0 spawn that produced no
    # assistant text is an empty/garbled response (the recurrent glm-harness flake),
    # not a success. Surface it as a RETRYABLE, loud failure so the adapter retries
    # and we never silently emit an empty report. Timeouts keep their own signal.
    rc, err = _coerce_empty_completion_to_retryable(
        claude_result.returncode, claude_result.timed_out,
        claude_result.completion, claude_result.error, "glm-harness",
    )
    return GLMHarnessSpawnResult(
        returncode=rc,
        completion=claude_result.completion,
        events_written=claude_result.events_written,
        session_id=claude_result.session_id,
        timed_out=claude_result.timed_out,
        model=resolved_model,
        stopped_early=claude_result.stopped_early,
        error=err,
        token_usage=claude_result.token_usage,
        _adapter=getattr(claude_result, "_adapter", None),
    )


def _coerce_empty_completion_to_retryable(
    returncode: int,
    timed_out: bool,
    completion: Any,
    error: Optional[str],
    lane: str,
) -> "tuple[int, Optional[str]]":
    """Turn an rc=0 spawn with an empty completion into a retryable, loud failure.

    The harness lanes (claude CLI → litellm/OpenRouter) occasionally return a
    successful exit with no assistant text. Left as-is that becomes a silent
    empty report with no retry. Returning (1, error) makes the flake visible and
    lets the dispatch adapter's retry budget re-attempt. Timeouts are left alone
    (they already carry their own returncode/timed_out signal).
    """
    if returncode != 0 or timed_out:
        return returncode, error
    text = ""
    if isinstance(completion, dict):
        text = completion.get("text", "") or ""
    if text.strip():
        return returncode, error
    logger.error("%s: empty completion on a returncode-0 spawn — marking retryable", lane)
    return 1, error or f"{lane} returned an empty completion (retryable; provider flake)"
