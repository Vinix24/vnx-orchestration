#!/usr/bin/env python3
"""
VNX Runtime Core CLI — Shell interface for dispatcher integration.

Called by dispatcher_v8_minimal.sh to perform durable coordination
operations (broker registration, lease acquisition, delivery tracking)
without embedding Python logic in the shell script.

Usage:
  python runtime_core_cli.py register
      --dispatch-id <id> [--terminal T2] [--track B] [--skill <name>]
      [--gate <gate>] [--pr-ref PR-5] [--priority P1]
      [--prompt-file /path] [--prompt-text "..."]

  python runtime_core_cli.py delivery-start
      --dispatch-id <id> --terminal T2 [--attempt-number 1]

  python runtime_core_cli.py delivery-success
      --dispatch-id <id> --attempt-id <aid>

  python runtime_core_cli.py delivery-failure
      --dispatch-id <id> --attempt-id <aid> [--reason "..."]

  python runtime_core_cli.py check-terminal
      --terminal T2 --dispatch-id <id>

  python runtime_core_cli.py acquire-lease
      --terminal T2 --dispatch-id <id> [--lease-seconds 600]

  python runtime_core_cli.py release-lease
      --terminal T2 --generation <N>

  python runtime_core_cli.py compat-check

All commands output a single JSON line to stdout.
Exit codes: 0 = success, 1 = failure/unavailable.

When VNX_RUNTIME_PRIMARY=0, commands that require runtime core output
a disabled marker and exit 0 (non-blocking for legacy path compatibility).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR / "lib"))

from runtime_core import RuntimeCore, load_runtime_core, runtime_primary_active


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_dirs() -> tuple[str, str]:
    """Return (state_dir, dispatch_dir) from VNX environment."""
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


def _out(data: dict, exit_code: int = 0) -> None:
    print(json.dumps(data))
    sys.exit(exit_code)


def _require_core() -> RuntimeCore:
    """Return RuntimeCore or exit 1 if not available."""
    state_dir, dispatch_dir = _get_dirs()
    core = load_runtime_core(state_dir, dispatch_dir)
    if core is None:
        _out({"ok": False, "error": "VNX_RUNTIME_PRIMARY=0, runtime core disabled"}, 1)
    return core  # type: ignore[return-value]  # _out exits


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> None:
    """Register dispatch with broker before delivery."""
    state_dir, dispatch_dir = _get_dirs()
    core = load_runtime_core(state_dir, dispatch_dir)
    if core is None:
        # Non-blocking when runtime core is disabled — legacy path continues
        _out({"registered": False, "reason": "runtime_core_disabled"}, 0)

    prompt = ""
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            _out({"registered": False, "error": f"prompt_file_read_error:{exc}"}, 1)
    elif args.prompt_text:
        prompt = args.prompt_text

    result = core.register(
        dispatch_id=args.dispatch_id,
        prompt=prompt,
        terminal_id=args.terminal,
        track=args.track,
        skill=args.skill,
        gate=args.gate,
        pr_ref=args.pr_ref,
        priority=args.priority or "P1",
    )
    _out(result.to_dict(), 0 if result.registered else 1)


def cmd_delivery_start(args: argparse.Namespace) -> None:
    """Claim dispatch and record delivery start in broker."""
    core = _require_core()
    result = core.delivery_start(
        dispatch_id=args.dispatch_id,
        terminal_id=args.terminal,
        attempt_number=args.attempt_number or 1,
    )
    _out(result.to_dict(), 0 if result.started else 1)


def cmd_delivery_success(args: argparse.Namespace) -> None:
    """Record successful delivery (delivering -> accepted).

    Idempotent: duplicate acceptance returns exit 0 with noop=true.
    Terminal-state rejection returns exit 1 with noop_rejected=true.
    """
    core = _require_core()
    result = core.delivery_success(
        dispatch_id=args.dispatch_id,
        attempt_id=args.attempt_id,
    )
    # No-op (duplicate acceptance) is still success from the caller's perspective
    exit_code = 0 if result.get("success") or result.get("noop") else 1
    _out(result, exit_code)


def cmd_delivery_failure(args: argparse.Namespace) -> None:
    """Record delivery failure durably (delivering -> failed_delivery)."""
    core = _require_core()
    result = core.delivery_failure(
        dispatch_id=args.dispatch_id,
        attempt_id=args.attempt_id,
        reason=args.reason or "delivery failed",
    )
    _out(result, 0 if result.get("recorded") else 1)


def cmd_check_terminal(args: argparse.Namespace) -> None:
    """Check terminal availability via canonical lease state."""
    state_dir, dispatch_dir = _get_dirs()
    core = load_runtime_core(state_dir, dispatch_dir)
    if core is None:
        # When runtime core off, terminals are considered available (legacy decides)
        _out({"available": True, "terminal_id": args.terminal, "reason": "runtime_core_disabled"})

    result = core.check_terminal(
        terminal_id=args.terminal,
        dispatch_id=args.dispatch_id,
    )
    _out(result, 0)


def cmd_acquire_lease(args: argparse.Namespace) -> None:
    """Acquire canonical lease for terminal (idle -> leased)."""
    core = _require_core()
    result = core.acquire_lease(
        terminal_id=args.terminal,
        dispatch_id=args.dispatch_id,
        lease_seconds=args.lease_seconds or 600,
    )
    _out(result.to_dict(), 0 if result.acquired else 1)


def cmd_release_lease(args: argparse.Namespace) -> None:
    """Release canonical lease (leased -> idle)."""
    core = _require_core()
    result = core.release_lease(
        terminal_id=args.terminal,
        generation=args.generation,
    )
    _out(result, 0 if result.get("released") else 1)


def cmd_release_on_failure(args: argparse.Namespace) -> None:
    """Record delivery failure and release canonical lease atomically.

    Returns a structured result with explicit success/failure markers for
    both the delivery-failure record and the lease release. The caller
    uses these markers to emit a structured audit entry.

    Exit 0 even on partial failure so the caller can read the JSON and
    emit its own audit — do not let exit-code semantics hide the detail.
    """
    core = _require_core()
    result = core.release_on_delivery_failure(
        dispatch_id=args.dispatch_id,
        attempt_id=args.attempt_id or "",
        terminal_id=args.terminal,
        generation=args.generation,
        reason=args.reason or "delivery failed",
    )
    # Exit 0 always: caller reads cleanup_complete/lease_released to decide
    # whether to emit a cleanup-failure audit entry.
    _out(result, 0)


def cmd_compat_check(_args: argparse.Namespace) -> None:
    """Validate all runtime core components are functional."""
    state_dir, dispatch_dir = _get_dirs()
    result = RuntimeCore.check_compatibility(state_dir, dispatch_dir)
    _out(result, 0 if result.get("compatible") else 1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VNX Runtime Core CLI — shell interface for durable dispatch coordination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p = sub.add_parser("register", help="Register dispatch with broker before delivery")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--terminal")
    p.add_argument("--track")
    p.add_argument("--skill")
    p.add_argument("--gate")
    p.add_argument("--pr-ref")
    p.add_argument("--priority", default="P1")
    p.add_argument("--prompt-file", help="Path to file containing the prompt text")
    p.add_argument("--prompt-text", help="Prompt text inline (use prompt-file for large prompts)")

    # delivery-start
    p = sub.add_parser("delivery-start", help="Claim dispatch and record delivery start")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--terminal", required=True)
    p.add_argument("--attempt-number", type=int, default=1)

    # delivery-success
    p = sub.add_parser("delivery-success", help="Record successful delivery")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--attempt-id", required=True)

    # delivery-failure
    p = sub.add_parser("delivery-failure", help="Record delivery failure durably")
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--attempt-id", required=True)
    p.add_argument("--reason", default="delivery failed")

    # check-terminal
    p = sub.add_parser("check-terminal", help="Check terminal availability via canonical lease")
    p.add_argument("--terminal", required=True)
    p.add_argument("--dispatch-id", required=True)

    # acquire-lease
    p = sub.add_parser("acquire-lease", help="Acquire canonical lease for terminal")
    p.add_argument("--terminal", required=True)
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--lease-seconds", type=int, default=600)

    # release-lease
    p = sub.add_parser("release-lease", help="Release canonical lease")
    p.add_argument("--terminal", required=True)
    p.add_argument("--generation", type=int, required=True)

    # release-on-failure
    p = sub.add_parser(
        "release-on-failure",
        help="Record delivery failure and release canonical lease atomically",
    )
    p.add_argument("--dispatch-id", required=True)
    p.add_argument("--attempt-id", default="")
    p.add_argument("--terminal", required=True)
    p.add_argument("--generation", type=int, required=True)
    p.add_argument("--reason", default="delivery failed")

    # compat-check
    sub.add_parser("compat-check", help="Check runtime core compatibility")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "register": cmd_register,
        "delivery-start": cmd_delivery_start,
        "delivery-success": cmd_delivery_success,
        "delivery-failure": cmd_delivery_failure,
        "check-terminal": cmd_check_terminal,
        "acquire-lease": cmd_acquire_lease,
        "release-lease": cmd_release_lease,
        "release-on-failure": cmd_release_on_failure,
        "compat-check": cmd_compat_check,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
