#!/usr/bin/env python3
"""
VNX Dispatch Broker CLI — shell-callable entry point for broker operations.

All output is JSON to stdout for shell script consumption.
Errors are JSON with {"error": "...", "code": "..."} to stderr, exit code 1.
When the broker is disabled (VNX_BROKER_ENABLED=0), most commands print
{"status": "disabled", "broker_enabled": false} and exit 0.

Usage:
  python scripts/dispatch_broker_cli.py register \\
      --dispatch-id ID --prompt-file FILE [options]
  python scripts/dispatch_broker_cli.py claim \\
      --dispatch-id ID --terminal-id T1
  python scripts/dispatch_broker_cli.py deliver-start \\
      --dispatch-id ID --attempt-id AID
  python scripts/dispatch_broker_cli.py deliver-success \\
      --dispatch-id ID --attempt-id AID
  python scripts/dispatch_broker_cli.py deliver-failure \\
      --dispatch-id ID --attempt-id AID --reason REASON
  python scripts/dispatch_broker_cli.py inspect --dispatch-id ID
  python scripts/dispatch_broker_cli.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from runtime_coordination import get_connection, get_dispatch, get_events
from dispatch_broker import (
    BrokerDisabledError,
    BrokerError,
    DispatchBroker,
    broker_config_from_env,
    load_broker,
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _out(data: Dict[str, Any]) -> None:
    """Write JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def _err(message: str, code: str = "broker_error") -> NoReturn:
    """Write JSON error to stderr and exit 1."""
    print(json.dumps({"error": message, "code": code}), file=sys.stderr)
    sys.exit(1)


def _disabled_response() -> Dict[str, Any]:
    return {"status": "disabled", "broker_enabled": False}


# ---------------------------------------------------------------------------
# Broker factory with resolved paths
# ---------------------------------------------------------------------------

def _resolve_broker(paths: Dict[str, str]) -> Optional[DispatchBroker]:
    """Return DispatchBroker or None if disabled."""
    return load_broker(paths["VNX_STATE_DIR"], paths["VNX_DISPATCH_DIR"])


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    broker = _resolve_broker(paths)
    if broker is None:
        _out(_disabled_response())
        return

    prompt_file = Path(args.prompt_file)
    if not prompt_file.exists():
        _err(f"Prompt file not found: {prompt_file}", code="file_not_found")

    prompt = prompt_file.read_text(encoding="utf-8")

    expected_outputs: list = []
    if args.expected_outputs:
        try:
            expected_outputs = json.loads(args.expected_outputs)
        except json.JSONDecodeError:
            _err("--expected-outputs must be a valid JSON array", code="invalid_json")

    intelligence_refs: list = []
    if args.intelligence_refs:
        try:
            intelligence_refs = json.loads(args.intelligence_refs)
        except json.JSONDecodeError:
            _err("--intelligence-refs must be a valid JSON array", code="invalid_json")

    target_profile: dict = {}
    if args.target_profile:
        try:
            target_profile = json.loads(args.target_profile)
        except json.JSONDecodeError:
            _err("--target-profile must be a valid JSON object", code="invalid_json")

    metadata: dict = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError:
            _err("--metadata must be a valid JSON object", code="invalid_json")

    try:
        result = broker.register(
            args.dispatch_id,
            prompt,
            terminal_id=args.terminal_id or None,
            track=args.track or None,
            pr_ref=args.pr_ref or None,
            gate=args.gate or None,
            priority=args.priority,
            expected_outputs=expected_outputs,
            intelligence_refs=intelligence_refs,
            target_profile=target_profile,
            metadata=metadata,
        )
    except BrokerError as exc:
        _err(str(exc), code="register_failed")

    _out({
        "status": "ok",
        "dispatch_id": result.dispatch_row["dispatch_id"],
        "dispatch_state": result.dispatch_row["state"],
        "bundle_path": str(result.bundle_path),
        "already_existed": result.already_existed,
        "shadow_mode": broker.shadow_mode,
    })


def cmd_claim(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    broker = _resolve_broker(paths)
    if broker is None:
        _out(_disabled_response())
        return

    try:
        result = broker.claim(
            args.dispatch_id,
            args.terminal_id,
            attempt_number=args.attempt_number,
        )
    except BrokerError as exc:
        _err(str(exc), code="claim_failed")

    _out({
        "status": "ok",
        "dispatch_id": result.dispatch_row["dispatch_id"],
        "dispatch_state": result.dispatch_row["state"],
        "attempt_id": result.attempt_id,
        "attempt_number": result.attempt_number,
    })


def cmd_deliver_start(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    broker = _resolve_broker(paths)
    if broker is None:
        _out(_disabled_response())
        return

    try:
        broker.deliver_start(args.dispatch_id, args.attempt_id)
    except BrokerError as exc:
        _err(str(exc), code="deliver_start_failed")

    _out({
        "status": "ok",
        "dispatch_id": args.dispatch_id,
        "attempt_id": args.attempt_id,
        "action": "deliver_start",
    })


def cmd_deliver_success(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    broker = _resolve_broker(paths)
    if broker is None:
        _out(_disabled_response())
        return

    try:
        broker.deliver_success(args.dispatch_id, args.attempt_id)
    except BrokerError as exc:
        _err(str(exc), code="deliver_success_failed")

    _out({
        "status": "ok",
        "dispatch_id": args.dispatch_id,
        "attempt_id": args.attempt_id,
        "action": "deliver_success",
    })


def cmd_deliver_failure(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    broker = _resolve_broker(paths)
    if broker is None:
        _out(_disabled_response())
        return

    try:
        broker.deliver_failure(args.dispatch_id, args.attempt_id, args.reason)
    except BrokerError as exc:
        _err(str(exc), code="deliver_failure_failed")

    _out({
        "status": "ok",
        "dispatch_id": args.dispatch_id,
        "attempt_id": args.attempt_id,
        "action": "deliver_failure",
        "reason": args.reason,
    })


def cmd_inspect(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    config = broker_config_from_env()

    bundle_data = None
    dispatch_row = None

    # Always attempt to read bundle from disk regardless of enabled state
    dispatch_dir = Path(paths["VNX_DISPATCH_DIR"])
    bundle_json_path = dispatch_dir / args.dispatch_id / "bundle.json"
    if bundle_json_path.exists():
        try:
            bundle_data = json.loads(bundle_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            bundle_data = {"error": f"Could not read bundle.json: {exc}"}

    # Always attempt to read DB row
    state_dir = paths["VNX_STATE_DIR"]
    try:
        with get_connection(state_dir) as conn:
            dispatch_row = get_dispatch(conn, args.dispatch_id)
            events = get_events(conn, entity_id=args.dispatch_id, limit=20)
    except Exception as exc:
        events = []
        dispatch_row = {"error": f"DB read failed: {exc}"}

    _out({
        "dispatch_id": args.dispatch_id,
        "broker_enabled": config["enabled"],
        "shadow_mode": config["shadow_mode"],
        "bundle": bundle_data,
        "dispatch_row": dispatch_row,
        "recent_events": events,
    })


def cmd_status(args: argparse.Namespace, paths: Dict[str, str]) -> None:
    config = broker_config_from_env()
    _out({
        "status": "ok",
        "broker_enabled": config["enabled"],
        "shadow_mode": config["shadow_mode"],
        "state_dir": paths["VNX_STATE_DIR"],
        "dispatch_dir": paths["VNX_DISPATCH_DIR"],
    })


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dispatch_broker_cli.py",
        description="VNX Dispatch Broker CLI — JSON output for shell consumption",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # register
    reg = subparsers.add_parser("register", help="Register a dispatch and write its bundle")
    reg.add_argument("--dispatch-id", required=True, help="Unique dispatch identifier")
    reg.add_argument("--prompt-file", required=True, help="Path to file containing the prompt text")
    reg.add_argument("--terminal-id", default="", help="Target terminal (e.g. T2)")
    reg.add_argument("--track", default="", help="Worker track label (e.g. B)")
    reg.add_argument("--pr-ref", default="", help="PR reference (e.g. PR-1)")
    reg.add_argument("--gate", default="", help="Quality gate identifier")
    reg.add_argument("--priority", default="P2", help="Dispatch priority (default P2)")
    reg.add_argument("--expected-outputs", default="", help="JSON array of expected output specs")
    reg.add_argument("--intelligence-refs", default="", help="JSON array of intelligence references")
    reg.add_argument("--target-profile", default="", help="JSON object for target routing profile")
    reg.add_argument("--metadata", default="", help="JSON object for extra metadata")

    # claim
    claim = subparsers.add_parser("claim", help="Claim a dispatch for a terminal")
    claim.add_argument("--dispatch-id", required=True)
    claim.add_argument("--terminal-id", required=True)
    claim.add_argument("--attempt-number", type=int, default=1, help="Attempt sequence number")

    # deliver-start
    ds = subparsers.add_parser("deliver-start", help="Record that delivery has begun")
    ds.add_argument("--dispatch-id", required=True)
    ds.add_argument("--attempt-id", required=True)

    # deliver-success
    dsucc = subparsers.add_parser("deliver-success", help="Record successful delivery")
    dsucc.add_argument("--dispatch-id", required=True)
    dsucc.add_argument("--attempt-id", required=True)

    # deliver-failure
    dfail = subparsers.add_parser("deliver-failure", help="Record a delivery failure durably")
    dfail.add_argument("--dispatch-id", required=True)
    dfail.add_argument("--attempt-id", required=True)
    dfail.add_argument("--reason", required=True, help="Human-readable failure description")

    # inspect
    ins = subparsers.add_parser("inspect", help="Show bundle and DB state for a dispatch")
    ins.add_argument("--dispatch-id", required=True)

    # status
    subparsers.add_parser("status", help="Print broker configuration and resolved paths")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    paths = ensure_env()

    dispatch_table = {
        "register":       cmd_register,
        "claim":          cmd_claim,
        "deliver-start":  cmd_deliver_start,
        "deliver-success": cmd_deliver_success,
        "deliver-failure": cmd_deliver_failure,
        "inspect":        cmd_inspect,
        "status":         cmd_status,
    }

    handler = dispatch_table.get(args.command)
    if handler is None:
        _err(f"Unknown command: {args.command!r}", code="unknown_command")

    try:
        handler(args, paths)
    except BrokerDisabledError as exc:
        _out(_disabled_response())
    except (BrokerError, KeyError, ValueError) as exc:
        _err(str(exc))
    except Exception as exc:
        _err(f"Unexpected error: {exc}", code="internal_error")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
