#!/usr/bin/env python3
"""provider_dispatch.py — Provider-agnostic dispatch entry-point (Wave 4.6 PR-4.6.1).

Routes dispatch execution to the appropriate provider spawn handler based on
``--provider``.  In PR-4.6.1 only the ``claude`` provider is wired; all other
providers raise NotImplementedError with exit code 64 (EX_USAGE) until their
respective spawn handlers land in subsequent PRs.

See: claudedocs/wave4.6-provider-dispatch-generalization-design-2026-05-13.md §PR-4.6.1

BILLING SAFETY: this module does NOT import the Anthropic SDK.  Claude dispatch
delegates entirely to ``subprocess_dispatch.py`` which invokes ``claude -p`` via
subprocess only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_EX_USAGE = 64  # sysexits.h EX_USAGE

# Providers whose spawn handlers exist in this PR.
_IMPLEMENTED_PROVIDERS = {"claude"}

# Mapping: provider literal -> which future PR delivers its handler.
_FUTURE_PR_MAP = {
    "codex": "PR-4.6.3",
    "gemini": "PR-4.6.4",
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
        future_pr = _FUTURE_PR_MAP["codex"]
        print(
            f"Provider 'codex' spawn handler lands in {future_pr}. "
            "Use --provider claude for now.",
            file=sys.stderr,
        )
        raise SystemExit(_EX_USAGE)

    if provider == "gemini":
        future_pr = _FUTURE_PR_MAP["gemini"]
        print(
            f"Provider 'gemini' spawn handler lands in {future_pr}. "
            "Use --provider claude for now.",
            file=sys.stderr,
        )
        raise SystemExit(_EX_USAGE)

    if provider.startswith("litellm:"):
        print(
            f"Provider '{provider}' spawn handler lands in PR-4.6.5. "
            "Use --provider claude for now.",
            file=sys.stderr,
        )
        raise SystemExit(_EX_USAGE)

    # Unknown literal — argparse-style error (exit code 2).
    parser.error(
        f"Unknown provider '{provider}'. "
        "Accepted values: claude, codex, gemini, litellm:<model>."
    )
    return 2  # unreachable; parser.error() exits


if __name__ == "__main__":
    sys.exit(main())
