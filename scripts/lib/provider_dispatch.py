#!/usr/bin/env python3
"""provider_dispatch.py — Provider-agnostic dispatch entry-point (Wave 4.6).

Routes dispatch execution to the appropriate provider spawn handler based on
``--provider``. PR-4.6.1: claude wired. PR-4.6.3: codex wired. PR-4.6.4: gemini wired.
PR-4.6.5: litellm wired (litellm:<sub_provider> format).
PR-4.6.7: claude-sdk wired (opt-in, API-key only, non-governed per ADR-003 amendment).
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
_IMPLEMENTED_PROVIDERS = {"claude", "claude-sdk", "codex", "gemini", "litellm"}

# Mapping: provider literal -> which future PR delivers its handler.
_FUTURE_PR_MAP: dict = {}

# LiteLLM sub-provider defaults when VNX_LITELLM_MODEL is not set.
_LITELLM_SUB_PROVIDER_DEFAULTS: dict = {
    "bedrock": "bedrock/claude-sonnet-4-6",
    "deepseek": "deepseek/deepseek-v3",
    "kimi": "openai/moonshot-v1-32k",
    "glm-5.1": "zhipuai/glm-4",
    "ollama": "ollama/llama3",
    "anthropic": "anthropic/claude-sonnet-4-6",
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
            "Accepted values: claude, codex, gemini, litellm:<model>, claude-sdk. "
            "Example: --provider claude, --provider litellm:deepseek-v4-pro, --provider claude-sdk"
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


def _dispatch_litellm(args: argparse.Namespace) -> int:
    """Route to spawn_litellm for litellm-provider dispatches (PR-4.6.5).

    Accepts --provider litellm:<sub_provider>, e.g. litellm:deepseek.
    Model resolved via VNX_LITELLM_MODEL env var, sub_provider default, or
    "anthropic/claude-sonnet-4-6" fallback. Wires EventStore for NDJSON audit.
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

    env_model = os.environ.get("VNX_LITELLM_MODEL", "")
    if env_model:
        model = env_model
    elif sub_provider and sub_provider in _LITELLM_SUB_PROVIDER_DEFAULTS:
        model = _LITELLM_SUB_PROVIDER_DEFAULTS[sub_provider]
    elif sub_provider:
        model = f"{sub_provider}/default"
    else:
        model = "anthropic/claude-sonnet-4-6"

    result = spawn_litellm(
        prompt=args.instruction,
        model=model,
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal_id,
        sub_provider=sub_provider or None,
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


def _dispatch_claude_sdk(args: argparse.Namespace) -> int:
    """Route to spawn_claude_sdk for opt-in non-governed API-key dispatches (PR-4.6.7).

    ADR-003 amended: API-key only, OAuth runtime-refused. Not a replacement for
    the governed claude -p subprocess path. Use cases: sandbox experiments,
    fast iteration without audit-isolation requirements.
    """
    import os
    from provider_spawns.claude_sdk_spawn import spawn_claude_sdk

    event_store = None
    try:
        from event_store import EventStore
        event_store = EventStore()
    except Exception as _es_exc:
        logger.warning(
            "_dispatch_claude_sdk: EventStore unavailable; NDJSON audit sink skipped: %s",
            _es_exc,
        )

    model = os.environ.get("VNX_CLAUDE_SDK_MODEL", args.model or "claude-sonnet-4-6")

    result = spawn_claude_sdk(
        prompt=args.instruction,
        model=model,
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal_id,
        event_writer=event_store.append if event_store is not None else None,
    )
    if result.error:
        print(f"spawn_claude_sdk failed: {result.error}", file=sys.stderr)
        return 1
    if result.timed_out:
        print("spawn_claude_sdk timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        return 1
    if result.event_writer_failures > 0:
        logger.error(
            "claude_sdk dispatch completed but %d event_writer failures occurred — audit gap",
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

    if provider == "claude-sdk":
        return _dispatch_claude_sdk(args)

    # Unknown literal — argparse-style error (exit code 2).
    parser.error(
        f"Unknown provider '{provider}'. "
        "Accepted values: claude, codex, gemini, litellm:<model>, claude-sdk."
    )
    return 2  # unreachable; parser.error() exits


if __name__ == "__main__":
    sys.exit(main())
