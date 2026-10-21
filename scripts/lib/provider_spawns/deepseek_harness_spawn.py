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
import provider_switch  # scripts/lib, added to the path above

from load_deepseek_key import resolve_deepseek_api_key  # (path-setup above: scripts/lib injected into sys.path)

from provider_spawns.harness_config_dir import (
    HarnessConfigDirError,
    bare_harness_cli_args,
    harness_config_env,
    require_harness_credential,
)

HARNESS_LANE = "deepseek-harness"

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

# Keys that must be absent from the final child env — the production Anthropic API key
# AND the cached OAuth session token, either of which would authenticate the redirected
# CLI against api.anthropic.com and risk account ban (constraint:
# deepseek-harness-subscription-blocked). CLAUDE_CODE_OAUTH_TOKEN is scrubbed alongside
# ANTHROPIC_API_KEY (audit S3) — an inherited OAuth token sitting next to the key-auth
# ANTHROPIC_AUTH_TOKEN override is exactly the ambiguity the measured-safe key-auth
# recipe is meant to eliminate.
_HARNESS_SCRUB_KEYS: frozenset = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})


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


def build_harness_child_env(api_key: str, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Full child environment for a direct ``claude`` call against DeepSeek.

    Starts from ``base_env`` (default: the process environment), removes the
    Anthropic credentials in ``_HARNESS_SCRUB_KEYS`` and overlays the key-auth
    harness env. For callers that run ``subprocess.run(["claude", ...])`` themselves
    instead of going through ``spawn_deepseek_harness``: without the scrub, an
    ``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN`` inherited from the shell
    would sit next to the redirect (constraint deepseek-harness-subscription-blocked).
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in _HARNESS_SCRUB_KEYS:
        env.pop(key, None)
    env.update(build_harness_env(api_key))
    env.update(harness_config_env(HARNESS_LANE))
    require_harness_credential(env)
    return env


def build_harness_cli_args(cwd: Optional[Any] = None) -> List[str]:
    """Return the claude CLI flags for this lane: ``--bare`` and MCP fully off.

    ``--bare`` stops the child from loading every ancestor CLAUDE.md (the operator's
    ``$HOME/.claude``). ``cwd`` is the worktree root; its ``CLAUDE.md`` alone is passed
    back explicitly via ``--append-system-prompt-file``. Callers with no worktree
    (the analyzer) pass nothing and get ``--bare`` only.

    Order matters: ``--mcp-config`` is VARIADIC (``<configs...>``) in the claude
    CLI, so it greedily consumes following args until the next option flag.  The
    JSON config value is therefore placed FIRST and the boolean
    ``--strict-mcp-config`` placed LAST so it terminates the variadic before the
    positional prompt (otherwise the prompt is slurped as a bogus config path).
    """
    return [*bare_harness_cli_args(cwd), "--mcp-config", MCP_OFF_CONFIG, "--strict-mcp-config"]


def build_harness_argv(
    claude_bin: str,
    model: str,
    *,
    output_format: str,
    print_flags: Optional[List[str]] = None,
    cwd: Optional[Any] = None,
) -> List[str]:
    """Complete ``claude -p`` argv for a direct call against the DeepSeek endpoint.

    The single place where a caller that runs ``subprocess.run`` itself (the analyzer)
    gets the same model flag and the same ``--bare`` / MCP-off tail as
    ``spawn_deepseek_harness``, so the two launches cannot drift apart.
    """
    return [
        claude_bin, "-p", "--output-format", output_format,
        *(print_flags or []),
        "--model", model,
        *build_harness_cli_args(cwd),
    ]


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
    # OI-2066: carried from ClaudeSpawnResult.deadline_exceeded_seconds — the
    # total deadline when that deadline stopped the run, else None.
    deadline_exceeded_seconds: Optional[float] = None
    # ADR-043 (K2a-2): the condition under which no claude process started for
    # this spawn (no own key, unsafe config dir, or spawn_claude's own refusal).
    spawn_refused: Optional[str] = None
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


def _prepare_harness_env(
    api_key: str, extra_env: Optional[Dict[str, str]]
) -> "tuple[Dict[str, str], Optional[HarnessConfigDirError]]":
    """Merged child env for a spawn, or the fail-closed error that forbids the spawn.

    The mandatory harness env wins over any caller-supplied ``extra_env`` so the
    account-safety vars (base URL, key-auth token, telemetry-off) are non-overridable.
    The config-dir overlay is applied last and replaces any inherited CLAUDE_CONFIG_DIR.
    """
    merged_env: Dict[str, str] = dict(extra_env or {})
    merged_env.update(build_harness_env(api_key))
    try:
        merged_env.update(harness_config_env(HARNESS_LANE))
        require_harness_credential(merged_env)
    except HarnessConfigDirError as exc:
        return merged_env, exc
    return merged_env, None


def _refuse_without_own_key(
    resolved_key: str, model: Optional[str]
) -> Optional[DeepSeekHarnessSpawnResult]:
    """Return the fail-closed refusal when ``resolved_key`` is empty, else None.

    Account safety: the lane must never ride the OAuth subscription, so a spawn
    with no own key (arg, env, or provider env file) is refused before any
    ``claude`` subprocess.
    """
    if (resolved_key or "").strip():
        return None
    logger.error(
        "spawn_deepseek_harness: %s is absent from the environment and the "
        "provider env file; refusing to spawn (account safety — the lane must "
        "use the own DeepSeek key in key-auth mode, never the OAuth subscription).",
        DEEPSEEK_API_KEY_ENV,
    )
    return DeepSeekHarnessSpawnResult(
        returncode=1, completion={}, events_written=0, session_id=None,
        timed_out=False, model=resolve_harness_model(model), spawn_refused="local_key_missing",
        error=(
            f"local key missing: {DEEPSEEK_API_KEY_ENV} absent from the "
            f"environment and the provider env file — key-auth required, "
            f"OAuth fallback forbidden"
        ),
    )


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

    Resolves the own DeepSeek key (``api_key`` arg, then ``DEEPSEEK_API_KEY``
    env, then the provider env file via ``load_deepseek_key``), overlays the
    harness env + MCP-off CLI flags, and delegates transport to the governed
    ``spawn_claude`` path.  Fails closed (no spawn) when no own key is available
    in any source — the lane must never ride the OAuth subscription.

    Parameters mirror ``spawn_claude``; ``model`` resolves to the v4-pro default
    when None/empty.  ``extra_env`` (e.g. VNX identity propagation) is merged
    UNDER the mandatory harness env so the account-safety vars cannot be
    overridden by a caller.

    Raises ``provider_switch.ProviderDisabledError`` before the key is resolved when the
    operator switched deepseek off for the project ``extra_env`` carries (``VNX_PROJECT_ID``).
    ``spawn_claude`` checks the same family again through the harness base URL.
    """
    provider_switch.require("deepseek-harness", consumer="deepseek_harness_spawn",
                            project_id=(extra_env or {}).get(provider_switch.PROJECT_ENV))
    resolved_key = api_key if api_key is not None else resolve_deepseek_api_key()
    missing_key = _refuse_without_own_key(resolved_key, model)
    if missing_key is not None:
        return missing_key

    resolved_model = resolve_harness_model(model)

    merged_env, refusal = _prepare_harness_env(resolved_key, extra_env)
    if refusal is not None:
        logger.error("spawn_deepseek_harness: %s; refusing to spawn.", refusal)
        return DeepSeekHarnessSpawnResult(
            returncode=1, completion={}, events_written=0, session_id=None,
            timed_out=False, model=resolved_model, spawn_refused="harness_config_dir_unsafe",
            error=f"harness config dir unsafe: {refusal}",
        )

    claude_result = spawn_claude(
        prompt=prompt,
        model=resolved_model,
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        event_writer=event_writer,
        health_monitor=health_monitor,
        on_event=on_event,
        extra_env=merged_env,
        extra_cli_args=build_harness_cli_args(cwd if cwd is not None else os.getcwd()),
        cwd=cwd,
        scrub_env_keys=_HARNESS_SCRUB_KEYS,
        **kwargs,
    )

    # Harden the harness lane: an rc=0 spawn with no assistant text is an
    # empty/garbled provider response, not a success. Make it retryable + loud
    # so the adapter retries instead of emitting a silent empty report.
    rc, err = _coerce_empty_completion_to_retryable(
        claude_result.returncode, claude_result.timed_out,
        claude_result.completion, claude_result.error, "deepseek-harness",
    )
    return DeepSeekHarnessSpawnResult(
        returncode=rc,
        completion=claude_result.completion,
        events_written=claude_result.events_written,
        session_id=claude_result.session_id,
        timed_out=claude_result.timed_out,
        deadline_exceeded_seconds=claude_result.deadline_exceeded_seconds,
        model=resolved_model,
        stopped_early=claude_result.stopped_early,
        error=err,
        token_usage=claude_result.token_usage,
        spawn_refused=getattr(claude_result, "spawn_refused", None),
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
    successful exit with no assistant text; left as-is that is a silent empty
    report with no retry. Returning (1, error) makes the flake visible and lets
    the adapter's retry budget re-attempt. Timeouts keep their own signal.
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
