#!/usr/bin/env python3
"""
tmux-adapter — CLI for TmuxAdapter delivery and diagnostics.

Usage
-----
  # Deliver a dispatch to a terminal (primary load-dispatch path by default)
  python tmux_adapter_cli.py deliver --terminal T2 --dispatch-id <id>

  # Deliver using legacy paste path explicitly
  python tmux_adapter_cli.py deliver --terminal T2 --dispatch-id <id> \
      --legacy --skill "/backend-developer" --prompt-file /path/to/prompt.txt

  # Resolve pane for a terminal (dry-run, no delivery)
  python tmux_adapter_cli.py resolve --terminal T2

  # Show adapter config derived from environment
  python tmux_adapter_cli.py config

Exit codes:
  0  Success
  1  Delivery failed or target not found
  2  Adapter disabled (VNX_TMUX_ADAPTER_ENABLED=0)
  3  Bad arguments
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure scripts/lib is on the path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from tmux_adapter import (
    AdapterDisabledError,
    PaneNotFoundError,
    TmuxAdapter,
    adapter_config_from_env,
    adapter_enabled,
    load_adapter,
)


# ---------------------------------------------------------------------------
# Default resolution helpers (mirrors load_dispatch.py)
# ---------------------------------------------------------------------------

def _default_state_dir() -> str:
    vnx_data = os.environ.get("VNX_DATA_DIR") or os.environ.get("VNX_STATE_DIR")
    if vnx_data:
        return str(Path(vnx_data) / "state")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".vnx-data" / "state"
        if candidate.exists():
            return str(candidate)
    return ".vnx-data/state"


def _default_dispatch_dir() -> str:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return str(Path(vnx_data) / "dispatches")
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".vnx-data" / "dispatches"
        if candidate.exists():
            return str(candidate)
    return ".vnx-data/dispatches"


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_deliver(args: argparse.Namespace) -> int:
    if not adapter_enabled():
        print("Adapter disabled (VNX_TMUX_ADAPTER_ENABLED=0). Exiting.", file=sys.stderr)
        return 2

    state_dir = args.state_dir or _default_state_dir()

    primary = not args.legacy
    adapter = TmuxAdapter(state_dir, primary_path=primary)

    prompt_text: str = ""
    if args.prompt_file:
        try:
            prompt_text = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR reading prompt file: {exc}", file=sys.stderr)
            return 3

    result = adapter.deliver(
        terminal_id=args.terminal,
        dispatch_id=args.dispatch_id,
        attempt_id=args.attempt_id,
        skill_command=args.skill or "",
        prompt=prompt_text,
        actor=args.actor,
    )

    output = {
        "success": result.success,
        "terminal_id": result.terminal_id,
        "dispatch_id": result.dispatch_id,
        "pane_id": result.pane_id,
        "path_used": result.path_used,
        "failure_reason": result.failure_reason,
    }
    print(json.dumps(output, indent=2))

    return 0 if result.success else 1


def cmd_resolve(args: argparse.Namespace) -> int:
    if not adapter_enabled():
        print("Adapter disabled (VNX_TMUX_ADAPTER_ENABLED=0). Exiting.", file=sys.stderr)
        return 2

    state_dir = args.state_dir or _default_state_dir()
    adapter = TmuxAdapter(state_dir)

    try:
        target = adapter.resolve_target(args.terminal)
    except PaneNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output = {
        "terminal_id": target.terminal_id,
        "pane_id": target.pane_id,
        "provider": target.provider,
    }
    print(json.dumps(output, indent=2))
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    cfg = adapter_config_from_env()
    print(json.dumps(cfg, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tmux-adapter",
        description="VNX tmux delivery adapter CLI.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # deliver sub-command
    deliver_p = sub.add_parser("deliver", help="Deliver a dispatch to a terminal.")
    deliver_p.add_argument("--terminal", required=True, help="Target terminal (T1/T2/T3)")
    deliver_p.add_argument("--dispatch-id", required=True, metavar="ID")
    deliver_p.add_argument("--attempt-id", default=None, metavar="ID",
                           help="Attempt ID for event linkage (optional)")
    deliver_p.add_argument("--legacy", action="store_true",
                           help="Use legacy paste-buffer path instead of load-dispatch")
    deliver_p.add_argument("--skill", default=None,
                           help="Skill command for legacy path (e.g. /backend-developer)")
    deliver_p.add_argument("--prompt-file", default=None, metavar="FILE",
                           help="Path to prompt text file for legacy path")
    deliver_p.add_argument("--state-dir", default=None, metavar="DIR")
    deliver_p.add_argument("--actor", default="adapter",
                           help="Actor label for coordination events")

    # resolve sub-command
    resolve_p = sub.add_parser("resolve", help="Resolve terminal to pane (dry-run).")
    resolve_p.add_argument("--terminal", required=True, help="Terminal ID (T1/T2/T3)")
    resolve_p.add_argument("--state-dir", default=None, metavar="DIR")

    # config sub-command
    sub.add_parser("config", help="Show adapter config from environment.")

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch_table = {
        "deliver": cmd_deliver,
        "resolve": cmd_resolve,
        "config": cmd_config,
    }
    handler = dispatch_table.get(args.command)
    if handler is None:
        parser.print_help()
        return 3

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
