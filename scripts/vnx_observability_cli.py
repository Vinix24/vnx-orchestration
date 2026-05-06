#!/usr/bin/env python3
"""vnx_observability_cli.py — VNX Observability Tiers CLI.

Commands:
  tiers             List each registered adapter with its observability tier.
  gate-check        Check if a provider is admitted for a governance variant.

Usage::

    python3 scripts/vnx_observability_cli.py tiers
    python3 scripts/vnx_observability_cli.py gate-check gemini coding-strict

BILLING SAFETY: No Anthropic SDK. No network calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPTS_LIB = Path(__file__).resolve().parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from observability_tier import (
    ADAPTER_DEFAULT_TIERS,
    ADAPTER_MINIMUM_TIERS,
    GOVERNANCE_MIN_TIERS,
    resolve_effective_tier,
)
from gate_stack_resolver import check_tier_admission


def _tier_label(tier: int) -> str:
    labels = {1: "full-streaming", 2: "limited-streaming", 3: "final-only"}
    return labels.get(tier, f"tier-{tier}")


def cmd_tiers(argv: list[str]) -> int:
    """List each registered adapter with its current observability tier."""
    print("VNX Observability Tiers")
    print("=" * 60)
    print(f"{'Adapter':<14} {'Effective':<10} {'Minimum':<10} {'Mode'}")
    print("-" * 60)

    adapters = ["claude", "codex", "gemini", "litellm", "ollama"]
    for provider in adapters:
        effective = resolve_effective_tier(provider)
        minimum = ADAPTER_MINIMUM_TIERS.get(provider, effective)
        mode = _tier_label(effective)

        # Annotate environment-dependent adapters
        note = ""
        if provider == "gemini":
            stream = os.environ.get("VNX_GEMINI_STREAM", "0").strip()
            note = f"  [VNX_GEMINI_STREAM={stream}]"
        elif provider == "litellm":
            note = "  [streaming SSE]"
        elif provider == "ollama":
            note = "  [text-only baseline; Tier-1 when tool_use detected]"

        print(f"  {provider:<12} {effective:<10} {minimum:<10} {mode}{note}")

    print()
    print("Governance variant minimum requirements:")
    print("-" * 60)
    for variant, min_tier in sorted(GOVERNANCE_MIN_TIERS.items()):
        print(f"  {variant:<20} min_tier={min_tier}  ({_tier_label(min_tier)})")

    return 0


def cmd_gate_check(argv: list[str]) -> int:
    """Check tier admission for a provider + governance variant."""
    if len(argv) < 2:
        print("Usage: vnx_observability_cli.py gate-check <provider> [governance_variant]",
              file=sys.stderr)
        return 2

    provider = argv[1]
    variant = argv[2] if len(argv) > 2 else "default"

    result = check_tier_admission(provider, variant)

    status = "ALLOWED" if result.is_allowed() else "REJECTED"
    print(f"[{status}] {result.reason}")
    print(f"  provider={result.provider!r}  adapter_tier={result.adapter_tier}"
          f"  required_tier={result.required_tier}"
          f"  governance={result.governance_variant!r}")

    return 0 if result.is_allowed() else 1


_COMMANDS = {
    "tiers": cmd_tiers,
    "gate-check": cmd_gate_check,
}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    cmd = argv[0]
    handler = _COMMANDS.get(cmd)
    if handler is None:
        print(f"Unknown command: {cmd!r}. Available: {', '.join(_COMMANDS)}", file=sys.stderr)
        return 2

    return handler(argv)


if __name__ == "__main__":
    raise SystemExit(main())
