#!/usr/bin/env python3
"""CLI: sweep expired leases. Idempotent. Used by dispatcher prelude tick (SUP-PR2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running directly via "python3 scripts/lib/lease_sweep.py"
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from lease_manager import LeaseManager  # noqa: E402
from project_root import resolve_state_dir  # noqa: E402


def run(state_dir: Path, *, actor: str = "lease_sweep", reason: str = "TTL elapsed") -> list[str]:
    """Expire stale leases under state_dir. Returns list of expired terminal_ids."""
    mgr = LeaseManager(state_dir)
    return mgr.expire_stale(actor=actor, reason=reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sweep expired terminal leases (TTL enforcement, idempotent).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="path to runtime state directory (default: resolve from project root).",
    )
    parser.add_argument(
        "--actor",
        default="lease_sweep",
        help="actor recorded on lease_expired coordination events.",
    )
    parser.add_argument(
        "--reason",
        default="TTL elapsed",
        help="reason recorded on lease_expired coordination events.",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON to stdout.")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else resolve_state_dir(__file__)
    expired = run(state_dir, actor=args.actor, reason=args.reason)

    if args.json:
        json.dump({"expired": expired, "count": len(expired)}, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(f"lease_sweep: expired {len(expired)} stale leases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
