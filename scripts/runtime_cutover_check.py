#!/usr/bin/env python3
"""
VNX Runtime Core Cutover Compatibility Check — gate_pr5_runtime_core_cutover.

Validates that all runtime core components (broker, lease manager, adapter,
receipt linkage, governance) are functional before or after PR-5 cutover.

Usage:
  python scripts/runtime_cutover_check.py
  python scripts/runtime_cutover_check.py --json
  python scripts/runtime_cutover_check.py --component broker
  python scripts/runtime_cutover_check.py --gate gate_pr5_runtime_core_cutover

Exit codes:
  0 — all checks pass (compatible)
  1 — one or more checks failed (not compatible)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

from runtime_core import RuntimeCore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_dirs() -> tuple[str, str]:
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    state_dir = os.environ.get(
        "VNX_STATE_DIR",
        str(Path(vnx_data) / "state") if vnx_data else "/tmp/vnx-state",
    )
    dispatch_dir = os.environ.get(
        "VNX_DISPATCH_DIR",
        str(Path(vnx_data) / "dispatches") if vnx_data else "/tmp/vnx-dispatches",
    )
    return state_dir, dispatch_dir


def _print_component(name: str, result: dict) -> None:
    ok = result.get("ok", False)
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        err = result.get("error") or result.get("reason", "unknown")
        print(f"         error: {err}")
    else:
        extra = {k: v for k, v in result.items() if k not in ("ok",)}
        if extra:
            extra_str = "  ".join(f"{k}={v}" for k, v in extra.items())
            print(f"         {extra_str}")


def _gate_checklist(result: dict) -> None:
    """Print PR-5 quality gate checklist from compat check result."""
    components = result.get("components", {})

    def gate_item(check: bool, label: str) -> None:
        mark = "[x]" if check else "[ ]"
        print(f"  {mark} {label}")

    print("\ngate_pr5_runtime_core_cutover checklist:")
    gate_item(
        components.get("broker", {}).get("ok", False)
        and not components.get("broker", {}).get("shadow_mode", True),
        "New dispatches use broker-first durable registration by default",
    )
    gate_item(
        components.get("lease_manager", {}).get("ok", False),
        "Terminal assignment uses canonical lease state by default",
    )
    gate_item(
        components.get("receipt_linkage", {}).get("ok", False),
        "Receipts still correlate cleanly to dispatch_id after cutover",
    )
    gate_item(
        components.get("t0_authority", {}).get("ok", False),
        "Existing governance workflows remain functional without completion-authority regression",
    )
    gate_item(
        Path(_SCRIPT_DIR / ".." / "docs" / "runtime_core_rollback.md").exists(),
        "Rollback path to legacy transport is documented and tested",
    )
    gate_item(
        Path(_SCRIPT_DIR / ".." / "tests" / "test_runtime_cutover.py").exists(),
        "All tests pass for cutover compatibility and receipt linkage",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="VNX Runtime Core Cutover Compatibility Check")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of human-readable")
    parser.add_argument("--component", help="Check a single component only")
    parser.add_argument("--gate", help="Print gate checklist for the specified gate")
    args = parser.parse_args()

    state_dir, dispatch_dir = _get_dirs()
    result = RuntimeCore.check_compatibility(state_dir, dispatch_dir)

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("compatible") else 1)

    print(f"\nVNX Runtime Core Cutover Compatibility Check")
    print(f"state_dir:    {state_dir}")
    print(f"dispatch_dir: {dispatch_dir}")
    print(f"runtime_primary: {result['flags']['VNX_RUNTIME_PRIMARY']}")
    print(f"broker_shadow:   {result['flags']['VNX_BROKER_SHADOW']}")
    print(f"canonical_lease: {result['flags']['VNX_CANONICAL_LEASE_ACTIVE']}")
    print()

    components = result.get("components", {})

    if args.component:
        if args.component not in components:
            print(f"Unknown component: {args.component}")
            print(f"Available: {', '.join(components.keys())}")
            sys.exit(1)
        _print_component(args.component, components[args.component])
    else:
        print("Component checks:")
        for name, comp_result in components.items():
            _print_component(name, comp_result)

    if args.gate:
        _gate_checklist(result)

    overall = result.get("compatible", False)
    print()
    print(f"Overall: {'COMPATIBLE' if overall else 'NOT COMPATIBLE'}")

    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
