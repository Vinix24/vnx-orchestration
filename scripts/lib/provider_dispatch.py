#!/usr/bin/env python3
"""provider_dispatch.py — Provider-agnostic dispatch entry-point (Wave 4.6).

Routes dispatch execution to the appropriate provider spawn handler based on
``--provider``. PR-4.6.1: claude wired. PR-4.6.3: codex wired. PR-4.6.4: gemini wired.
PR-4.6.5: litellm wired (litellm:<sub_provider> format).
All other providers raise SystemExit(64) until their handlers land.

See: claudedocs/wave4.6-provider-dispatch-generalization-design-2026-05-13.md

BILLING SAFETY: this module does NOT import the Anthropic SDK.  Claude dispatch
delegates entirely to ``subprocess_dispatch.py`` which invokes ``claude -p`` via
subprocess only.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger(__name__)

_EX_USAGE = 64  # sysexits.h EX_USAGE

# Providers whose spawn handlers exist.
_IMPLEMENTED_PROVIDERS = {"claude", "codex", "gemini", "litellm"}

# Mapping: provider literal -> which future PR delivers its handler.
_FUTURE_PR_MAP: dict = {}

# LiteLLM sub-provider defaults when VNX_LITELLM_MODEL is not set.
_LITELLM_SUB_PROVIDER_DEFAULTS: dict = {
    "bedrock": "bedrock/claude-sonnet-4-6",
    "deepseek": "deepseek/deepseek-v3.2",
    "moonshot": "moonshot/kimi-k2-0905-preview",
    "glm-5.1": "zhipuai/glm-4",
    "ollama": "ollama/llama3",
    "anthropic": "anthropic/claude-sonnet-4-6",
}

# Env vars required per sub-provider (fast-fail before subprocess spawn)
_SUB_PROVIDER_KEY_REQS: dict = {
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VNX provider-agnostic dispatch entry (Wave 4.6)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        required=True,
        help=(
            "Provider to use for dispatch. "
            "Accepted values: claude, codex, gemini, litellm:<model>. "
            "Example: --provider claude, --provider litellm:deepseek-v4-pro"
        ),
    )
    # Forward all existing subprocess_dispatch.py flags verbatim.
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--role", default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--no-auto-commit", action="store_true")
    parser.add_argument("--gate", default="")
    parser.add_argument("--dispatch-paths", default="")
    parser.add_argument("--pr-id", default=None)
    return parser


def _dispatch_claude(args: argparse.Namespace) -> int:
    """Delegate to subprocess_dispatch.deliver_with_recovery (claude path).

    Produces byte-identical NDJSON + receipt as direct subprocess_dispatch
    invocation — the delegation preserves all argument semantics unchanged.
    """
    import subprocess_dispatch as sd

    # OI-1107: fall back to Role: header in instruction, then to documented default.
    role = args.role
    if role is None:
        role = sd._extract_role_from_instruction(args.instruction) or sd._ROLE_FALLBACK

    dispatch_paths: list[str] | None = None
    if args.dispatch_paths.strip():
        dispatch_paths = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]

    ok = sd.deliver_with_recovery(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
        role=role,
        max_retries=args.max_retries,
        auto_commit=not args.no_auto_commit,
        gate=args.gate,
        dispatch_paths=dispatch_paths,
        pr_id=args.pr_id,
    )
    return 0 if ok else 1


def _dispatch_codex(args: argparse.Namespace) -> int:
    """Route to spawn_codex for codex-provider dispatches (PR-4.6.3).

    Prompt is the raw instruction; file-content injection is caller's responsibility.
    Wires EventStore as event_writer so codex dispatches produce a NDJSON audit trail
    identical to the claude path (provider-agnostic audit completeness, ADR-005).
    """
    import os
    from provider_spawns.codex_spawn import spawn_codex

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.warning(
            "_dispatch_codex: EventStore unavailable; NDJSON audit sink skipped: %s",
            _es_exc,
        )

    model = os.environ.get("VNX_CODEX_MODEL", "")
    result = spawn_codex(
        prompt=args.instruction,
        model=model,
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal_id,
        event_writer=event_store.append if event_store is not None else None,
    )
    if result.error:
        print(f"spawn_codex failed: {result.error}", file=sys.stderr)
        return 1
    if result.timed_out:
        print("spawn_codex timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        return 1
    if result.event_writer_failures > 0:
        logger.error(
            "codex dispatch completed but %d event_writer failures occurred — audit gap",
            result.event_writer_failures,
        )
        return 2
    return 0


def _resolve_deepseek_model() -> str:
    """Load DeepSeek model litellm_name from registry, fallback to hardcoded default."""
    from providers import provider_registry as _reg
    try:
        rec = _reg.get_default_model("deepseek")
    except (FileNotFoundError, ValueError) as e:
        logger.error("provider_dispatch: registry resolve failed for deepseek: %s", e)
        raise RuntimeError(f"provider registry resolution failed: {e}") from e
    if rec is not None:
        return rec.litellm_name
    return _LITELLM_SUB_PROVIDER_DEFAULTS["deepseek"]


def _resolve_moonshot_model(model_alias: "str | None" = None) -> str:
    """Load Moonshot model litellm_name from registry.

    When model_alias is given (e.g. 'kimi-k2-6'), looks up that specific model key.
    Defaults to 'kimi-k2-0905-default' (cost-effective lane) when alias is absent.
    Falls back to hardcoded default when registry is unavailable.
    """
    from providers import provider_registry as _reg
    try:
        registry = _reg.load()
    except (FileNotFoundError, ValueError) as e:
        logger.error("provider_dispatch: registry resolve failed for moonshot: %s", e)
        raise RuntimeError(f"provider registry resolution failed: {e}") from e
    cfg = registry.get("moonshot")
    if cfg is None or not cfg.enabled or not cfg.models:
        return _LITELLM_SUB_PROVIDER_DEFAULTS["moonshot"]
    target_key = model_alias or "kimi-k2-0905-default"
    if target_key in cfg.models:
        return cfg.models[target_key].litellm_name
    # alias not found — fall back to first available model
    return next(iter(cfg.models.values())).litellm_name


def _dispatch_litellm(args: argparse.Namespace) -> int:
    """Route to spawn_litellm for litellm-provider dispatches (PR-4.6.5).

    Accepts --provider litellm:<sub_provider>, e.g. litellm:deepseek.
    Model resolved via VNX_LITELLM_MODEL env var, registry lookup, sub_provider default,
    or "anthropic/claude-sonnet-4-6" fallback. Wires EventStore for NDJSON audit.
    DeepSeek requires DEEPSEEK_API_KEY env var (fast-fail before subprocess spawn).
    """
    import os
    from provider_spawns.litellm_spawn import spawn_litellm

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.warning(
            "_dispatch_litellm: EventStore unavailable; NDJSON audit sink skipped: %s",
            _es_exc,
        )

    parts = args.provider.split(":", 1)
    sub_provider = parts[1] if len(parts) > 1 else ""

    # Normalize sub-sub-routing: litellm:moonshot:kimi-k2-6 -> base=moonshot, alias=kimi-k2-6
    sub_parts = sub_provider.split(":", 1)
    base_sub = sub_parts[0]
    model_alias = sub_parts[1] if len(sub_parts) > 1 else None

    # Fast-fail for providers that require an explicit API key
    required_key = _SUB_PROVIDER_KEY_REQS.get(base_sub)
    if required_key and not os.environ.get(required_key):
        print(
            f"litellm:{base_sub} requires {required_key} env var",
            file=sys.stderr,
        )
        return _EX_USAGE

    env_model = os.environ.get("VNX_LITELLM_MODEL", "")
    if base_sub == "deepseek":
        model = env_model or _resolve_deepseek_model()
    elif base_sub == "moonshot":
        model = env_model or _resolve_moonshot_model(model_alias)
    elif env_model:
        model = env_model
    elif base_sub and base_sub in _LITELLM_SUB_PROVIDER_DEFAULTS:
        model = _LITELLM_SUB_PROVIDER_DEFAULTS[base_sub]
    elif base_sub:
        model = f"{base_sub}/default"
    else:
        model = "anthropic/claude-sonnet-4-6"

    result = spawn_litellm(
        prompt=args.instruction,
        model=model,
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal_id,
        sub_provider=base_sub or None,
        event_writer=event_store.append if event_store is not None else None,
    )
    if result.error:
        print(f"spawn_litellm failed: {result.error}", file=sys.stderr)
        return 1
    if result.timed_out:
        print("spawn_litellm timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        return 1
    if result.event_writer_failures > 0:
        logger.error(
            "litellm dispatch completed but %d event_writer failures occurred — audit gap",
            result.event_writer_failures,
        )
        return 2
    return 0


def _dispatch_gemini(args: argparse.Namespace) -> int:
    """Route to spawn_gemini for gemini-provider dispatches (PR-4.6.4).

    Prompt is the raw instruction; file-content injection is caller's responsibility.
    """
    import os
    from event_store import EventStore
    from provider_spawns.gemini_spawn import spawn_gemini

    model = os.environ.get("VNX_GEMINI_MODEL", "gemini-2.5-pro")
    event_store = EventStore()
    result = spawn_gemini(
        prompt=args.instruction,
        model=model,
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal_id,
        event_writer=event_store.append,
    )
    if result.error:
        print(f"spawn_gemini failed: {result.error}", file=sys.stderr)
        return 1
    if result.timed_out:
        print("spawn_gemini timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        return 1
    if result.event_writer_failures > 0:
        logger.error(
            "gemini dispatch completed but %d event_writer failures occurred — audit gap",
            result.event_writer_failures,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args, route to the correct provider handler, return exit code."""
    parser = _build_parser()

    # argparse exits with code 2 on unrecognised provider values — but provider
    # is a free-form string (litellm:<model>), not a fixed choices= set, so we
    # validate manually after parsing.
    args = parser.parse_args(argv)

    provider = args.provider

    if provider == "claude":
        return _dispatch_claude(args)

    if provider == "codex":
        return _dispatch_codex(args)

    if provider == "gemini":
        return _dispatch_gemini(args)

    if provider.startswith("litellm:") or provider == "litellm":
        return _dispatch_litellm(args)

    # Unknown literal — argparse-style error (exit code 2).
    parser.error(
        f"Unknown provider '{provider}'. "
        "Accepted values: claude, codex, gemini, litellm:<model>."
    )
    return 2  # unreachable; parser.error() exits


if __name__ == "__main__":
    sys.exit(main())
