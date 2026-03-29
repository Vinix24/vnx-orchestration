#!/usr/bin/env python3
"""
VNX Lease Heartbeat — Worker-side lease renewal CLI.

Workers call this script periodically while a dispatch is active to renew
their terminal lease and prevent TTL expiry. Each renewal must supply the
correct generation number to prevent stale renewals from racing with a new
lease acquisition on the same terminal (G-R3).

Usage (environment variables):
    VNX_TERMINAL_ID=T2 \
    VNX_LEASE_GENERATION=3 \
    python lease_heartbeat.py

Command-line flags override environment variables:
    python lease_heartbeat.py --terminal T2 --generation 3 [--lease-seconds 600]

Exit codes:
    0  Renewal succeeded
    1  Generation mismatch or terminal not leased (stale heartbeat)
    2  Terminal not found or schema not initialized
    3  Usage/configuration error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:
    print(f"[ERROR] Failed to load vnx_paths: {exc}", file=sys.stderr)
    sys.exit(3)

from lease_manager import LeaseManager
from runtime_coordination import InvalidTransitionError


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Renew a VNX terminal lease heartbeat.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--terminal",
        default=os.environ.get("VNX_TERMINAL_ID"),
        help="Terminal ID (e.g. T1, T2, T3). Also: VNX_TERMINAL_ID env var.",
    )
    parser.add_argument(
        "--generation",
        type=int,
        default=None,
        help="Lease generation to renew. Also: VNX_LEASE_GENERATION env var.",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=600,
        help="TTL extension in seconds (default: 600).",
    )
    parser.add_argument(
        "--project",
        action="store_true",
        default=False,
        help="Write terminal_state.json projection after renewing.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Output result as JSON instead of human-readable text.",
    )
    return parser.parse_args()


def _resolve_generation(args: argparse.Namespace) -> int:
    if args.generation is not None:
        return args.generation
    env_val = os.environ.get("VNX_LEASE_GENERATION")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return -1  # sentinel: validation below will catch this


def main() -> int:
    args = _parse_args()

    terminal_id = args.terminal
    if not terminal_id:
        print("[ERROR] --terminal or VNX_TERMINAL_ID is required", file=sys.stderr)
        return 3

    generation = _resolve_generation(args)
    if generation < 1:
        print(
            "[ERROR] --generation or VNX_LEASE_GENERATION must be a positive integer",
            file=sys.stderr,
        )
        return 3

    try:
        paths = ensure_env()
    except Exception as exc:
        print(f"[ERROR] env init failed: {exc}", file=sys.stderr)
        return 3

    state_dir = Path(paths["VNX_STATE_DIR"])
    mgr = LeaseManager(state_dir, auto_init=False)

    # Verify terminal exists before attempting renewal
    current = mgr.get(terminal_id)
    if current is None:
        msg = f"Terminal {terminal_id!r} not found. Schema initialized?"
        if args.json_output:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        return 2

    try:
        result = mgr.renew(
            terminal_id,
            generation=generation,
            lease_seconds=args.lease_seconds,
            actor=f"worker:{terminal_id}",
        )
    except ValueError as exc:
        # Generation mismatch — stale heartbeat
        msg = str(exc)
        if args.json_output:
            print(json.dumps({"ok": False, "error": msg, "terminal": terminal_id}))
        else:
            print(f"[STALE] {msg}", file=sys.stderr)
        return 1
    except InvalidTransitionError as exc:
        # Terminal is not leased
        msg = str(exc)
        if args.json_output:
            print(json.dumps({"ok": False, "error": msg, "terminal": terminal_id}))
        else:
            print(f"[WARN] Cannot renew: {msg}", file=sys.stderr)
        return 1

    if args.project:
        try:
            out = mgr.project_to_file()
        except Exception as exc:
            if not args.json_output:
                print(f"[WARN] Projection write failed: {exc}", file=sys.stderr)

    if args.json_output:
        print(json.dumps({
            "ok": True,
            "terminal": result.terminal_id,
            "state": result.state,
            "generation": result.generation,
            "expires_at": result.expires_at,
            "last_heartbeat_at": result.last_heartbeat_at,
        }))
    else:
        print(
            f"[OK] Lease renewed: {result.terminal_id} "
            f"gen={result.generation} "
            f"expires={result.expires_at}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
