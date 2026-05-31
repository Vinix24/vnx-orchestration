"""deepseek_harness_spawn.py — Governed DeepSeek-harness execution lane.

This is a PROVIDER LANE (execution/transport), not a review lane.  The
"harness" is the ``claude`` CLI itself, driving DeepSeek's
Anthropic-compatible endpoint with full tool-use / file-access.  Once this lane
exists, any role/task (review, implement, etc.) can be dispatched to it.

It reuses the existing governed claude spawn path
(``provider_spawns.claude_spawn.spawn_claude`` -> ``SubprocessAdapter``) so the
dispatch still flows through ``provider_dispatch._emit_governance`` and EMITS A
RECEIPT.  Because it goes through the sanctioned spawn handler (not a raw
``claude -p`` invocation), it is not the receipt-bypass that the GOV-1 guard
targets.

ACCOUNT PROTECTION (constraint deepseek-harness-subscription-blocked +
no-anthropic-sdk):  the lane authenticates with the OWN DeepSeek key in
KEY-AUTH mode, never the production OAuth subscription.  The measured-safe
recipe (claude 2.1.150 -> 0 calls to api.anthropic.com):

  ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
  ANTHROPIC_AUTH_TOKEN=$DEEPSEEK_API_KEY        (key-auth bearer; NOT OAuth)
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1    (kills telemetry/non-essential)
  --strict-mcp-config --mcp-config '{"mcpServers":{}}'  (MCP fully off)

FAIL-CLOSED: if no own DeepSeek key is available, the lane refuses to spawn.
It NEVER falls back to spawning ``claude`` against the DeepSeek redirect while
relying on the cached OAuth session — that would ride the production account
and is exactly the banned path.

BILLING SAFETY: only ``subprocess.Popen(["claude", ...])`` via
SubprocessAdapter (inherited from claude_spawn).  No Anthropic SDK is imported.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Ensure scripts/lib/ is importable whether loaded via provider_dispatch or tests.
_LIB_DIR = str(Path(__file__).resolve().parents[1])
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from provider_spawns.claude_spawn import spawn_claude  # noqa: E402 (path-setup above)

logger = logging.getLogger(__name__)

# DeepSeek's Anthropic-compatible Messages endpoint base.  The ``claude`` CLI
# appends ``/v1/messages`` itself, so the base URL stops at ``/anthropic``.
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Resolved 2026-05-31 by probing the endpoint and reading the returned ``model``
# field.  The endpoint's own error is authoritative: "The supported API model
# names are deepseek-v4-pro or deepseek-v4-flash".
#   deepseek-v4-pro    -> deepseek-v4-pro    (reasoning/pro grade — lane default)
#   deepseek-chat      -> deepseek-v4-flash  (fast lane)
#   deepseek-reasoner  -> deepseek-v4-flash  (fast lane)
DEFAULT_DEEPSEEK_HARNESS_MODEL = "deepseek-v4-pro"

# Force MCP fully off — no servers, strict config (ignore any inherited config).
MCP_OFF_CONFIG = '{"mcpServers":{}}'

# Environment variable that holds the operator's own DeepSeek API key.
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


def resolve_harness_model(model: Optional[str] = None) -> str:
    """Return the DeepSeek model id for the harness lane.

    Honours an explicit ``model`` argument, then the ``VNX_DEEPSEEK_HARNESS_MODEL``
    env override, then the v4-pro default.  Empty/whitespace values are ignored.
    """
    explicit = (model or "").strip()
    if explicit:
        return explicit
    env_model = (os.environ.get("VNX_DEEPSEEK_HARNESS_MODEL", "") or "").strip()
    if env_model:
        return env_model
    return DEFAULT_DEEPSEEK_HARNESS_MODEL


def build_harness_env(api_key: str) -> Dict[str, str]:
    """Build the measured-safe key-auth environment for the harness lane.

    ``api_key`` MUST be the operator's own DeepSeek key; callers fail closed
    before reaching here when it is absent.
    """
    return {
        "ANTHROPIC_BASE_URL": DEEPSEEK_ANTHROPIC_BASE_URL,
        # Key-auth bearer — NOT the cached OAuth session.
        "ANTHROPIC_AUTH_TOKEN": api_key,
        # Suppress telemetry / non-essential traffic to api.anthropic.com.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def build_harness_cli_args() -> List[str]:
    """Return the claude CLI flags that force MCP fully off for this lane.

    Order matters: ``--mcp-config`` is VARIADIC (``<configs...>``) in the claude
    CLI, so it greedily consumes following args until the next option flag.  The
    JSON config value is therefore placed FIRST and the boolean
    ``--strict-mcp-config`` placed LAST so it terminates the variadic before the
    positional prompt (otherwise the prompt is slurped as a bogus config path).
    """
    return ["--mcp-config", MCP_OFF_CONFIG, "--strict-mcp-config"]


@dataclass
class DeepSeekHarnessSpawnResult:
    """Spawn outcome for the DeepSeek-harness lane.

    Wraps a ClaudeSpawnResult (same transport) but attributes governance to the
    deepseek-harness provider so receipts and reports are not mislabeled as
    plain claude/anthropic.
    """

    returncode: int
    completion: Dict[str, Any]
    events_written: int
    session_id: Optional[str]
    timed_out: bool
    model: str = DEFAULT_DEEPSEEK_HARNESS_MODEL
    stopped_early: bool = False
    error: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None
    _adapter: Any = field(default=None, repr=False)

    @property
    def completion_text(self) -> str:
        """Final agent text from the result event.

        provider_dispatch._extract_response_text() reads this attribute to write
        the response body into the unified report; without it the governed
        report would record "(no response captured)".
        """
        if not isinstance(self.completion, dict):
            return ""
        return self.completion.get("text", "") or ""

    def frontmatter_fields(self) -> Dict[str, Any]:
        usage = self.token_usage or {}
        return {
            "provider": "deepseek-harness",
            "sub_provider": "deepseek",
            "exit_code": self.returncode,
            "token_usage": {
                "input": int(usage.get("input_tokens", usage.get("input", 0)) or 0),
                "output": int(usage.get("output_tokens", usage.get("output", 0)) or 0),
                "cache_read": int(
                    usage.get("cache_read_input_tokens", usage.get("cache_read", 0)) or 0
                ),
            },
        }


def spawn_deepseek_harness(
    prompt: str,
    model: Optional[str],
    dispatch_id: str,
    terminal_id: str,
    *,
    api_key: Optional[str] = None,
    event_writer: Optional[Callable[..., None]] = None,
    health_monitor: Optional[Any] = None,
    on_event: Optional[Callable[[Any], Optional[bool]]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cwd: Optional[Any] = None,
    **kwargs: Any,
) -> DeepSeekHarnessSpawnResult:
    """Spawn ``claude -p`` driving DeepSeek via the measured-safe key-auth recipe.

    Resolves the own DeepSeek key (``api_key`` arg or ``DEEPSEEK_API_KEY`` env),
    overlays the harness env + MCP-off CLI flags, and delegates transport to the
    governed ``spawn_claude`` path.  Fails closed (no spawn) when no own key is
    available — the lane must never ride the OAuth subscription.

    Parameters mirror ``spawn_claude``; ``model`` resolves to the v4-pro default
    when None/empty.  ``extra_env`` (e.g. VNX identity propagation) is merged
    UNDER the mandatory harness env so the account-safety vars cannot be
    overridden by a caller.
    """
    resolved_key = api_key if api_key is not None else os.environ.get(DEEPSEEK_API_KEY_ENV, "")
    if not (resolved_key or "").strip():
        # Fail closed — never spawn the redirect without an own key (would ride OAuth).
        logger.error(
            "spawn_deepseek_harness: %s is unset; refusing to spawn (account safety — "
            "the lane must use the own DeepSeek key in key-auth mode, never the OAuth "
            "subscription).",
            DEEPSEEK_API_KEY_ENV,
        )
        return DeepSeekHarnessSpawnResult(
            returncode=1,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=False,
            model=resolve_harness_model(model),
            error=f"{DEEPSEEK_API_KEY_ENV} unset — key-auth required, OAuth fallback forbidden",
        )

    resolved_model = resolve_harness_model(model)

    # Mandatory harness env wins over any caller-supplied extra_env so the
    # account-safety vars (base URL, key-auth token, telemetry-off) are
    # non-overridable.
    merged_env: Dict[str, str] = dict(extra_env or {})
    merged_env.update(build_harness_env(resolved_key))

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
        **kwargs,
    )

    return DeepSeekHarnessSpawnResult(
        returncode=claude_result.returncode,
        completion=claude_result.completion,
        events_written=claude_result.events_written,
        session_id=claude_result.session_id,
        timed_out=claude_result.timed_out,
        model=resolved_model,
        stopped_early=claude_result.stopped_early,
        error=claude_result.error,
        token_usage=claude_result.token_usage,
        _adapter=getattr(claude_result, "_adapter", None),
    )
