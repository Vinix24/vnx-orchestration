#!/usr/bin/env python3
"""permission_relay_cli.py — operator CLI for the worker-permission relay.

Subcommands (wired to scripts/lib/worker_permission_relay.py):

  vnx permission window-open --minutes N [--reason ...]   open the auto-accept window
  vnx permission window-close                             close it
  vnx permission window-status                            show window state
  vnx permission escalations                              list pending escalations
  vnx permission approve <dispatch_id>                    approve + relay "1" to the worker
  vnx permission deny <dispatch_id>                       deny (operator handles the worker)

All subcommands accept ``--json`` for machine-readable output.

The relay model: outside an open window every worker permission prompt escalates;
inside the window routine prompts auto-approve; catastrophic ops always escalate.
``approve`` resolves the escalation record AND best-effort relays the approval
keystroke ("1" + a SEPARATE Enter) into the worker's tmux session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
except Exception as exc:  # noqa: BLE001
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

import worker_permission_relay as relay  # noqa: E402


def _state_dir() -> Path:
    return Path(ensure_env()["VNX_STATE_DIR"])


def _emit(payload: dict, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(human)


def _cmd_window_open(args, sd: Path) -> int:
    win = relay.PermissionWindow(sd)
    record = win.open(args.minutes, args.reason)
    status = win.status()
    _emit(
        status,
        args.json,
        f"[ok] permission window OPEN for {args.minutes:g} min "
        f"(expires {record['expires_at']}, remaining {status['remaining_seconds']}s)"
        + (f" — reason: {args.reason}" if args.reason else ""),
    )
    return 0


def _cmd_window_close(args, sd: Path) -> int:
    win = relay.PermissionWindow(sd)
    win.close()
    _emit({"open": False}, args.json, "[ok] permission window CLOSED")
    return 0


def _cmd_window_status(args, sd: Path) -> int:
    win = relay.PermissionWindow(sd)
    status = win.status()
    if status["open"]:
        human = (
            f"[~] window OPEN — {status['remaining_seconds']}s remaining "
            f"(expires {status['expires_at']})"
        )
    else:
        human = "[ ] window CLOSED — all worker prompts escalate to the operator"
    _emit(status, args.json, human)
    return 0


def _cmd_escalations(args, sd: Path) -> int:
    pending = relay.list_escalations(state_dir=sd, pending_only=not args.all)
    if args.json:
        print(json.dumps(pending, indent=2, sort_keys=True))
        return 0
    if not pending:
        print("[ok] no pending permission escalations")
        return 0
    print(f"[!] {len(pending)} pending permission escalation(s):")
    for rec in pending:
        print(
            f"  - {rec.get('dispatch_id')}  reason={rec.get('reason')}  "
            f"captured={rec.get('captured_at')}\n"
            f"      cmd: {rec.get('command')}"
        )
    print("\nResolve with: vnx permission approve <dispatch_id>  |  deny <dispatch_id>")
    return 0


def _relay_keystroke_to_worker(dispatch_id: str, sd: Path) -> "str | None":
    """Best-effort: send "1"+Enter into the escalated worker's tmux session.

    Reads the lane handle (state/tmux_interactive/<dispatch_id>.json) for the
    session id. Returns the session id on success, None if no session/tmux.
    """
    handle_path = sd / "tmux_interactive" / f"{dispatch_id}.json"
    if not handle_path.exists():
        return None
    try:
        handle = json.loads(handle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session = (handle or {}).get("session")
    if not session:
        return None
    try:
        from tmux_interactive_dispatch import TmuxCommandRunner  # noqa: PLC0415

        runner = TmuxCommandRunner()
        if not runner.available():
            return None
        relay._send_approval(runner, session)
        return session
    except Exception:  # noqa: BLE001 — relay is best-effort
        return None


def _cmd_approve(args, sd: Path) -> int:
    record = relay.resolve_escalation(args.dispatch_id, approved=True, state_dir=sd)
    if record is None:
        _emit(
            {"error": "no_escalation", "dispatch_id": args.dispatch_id},
            args.json,
            f"[x] no escalation found for {args.dispatch_id}",
        )
        return 1
    session = _relay_keystroke_to_worker(args.dispatch_id, sd)
    record["relayed_session"] = session
    human = f"[ok] approved {args.dispatch_id}"
    human += (
        f" — relayed approval to session {session}"
        if session
        else " — no live worker session found (record updated only)"
    )
    _emit(record, args.json, human)
    return 0


def _cmd_deny(args, sd: Path) -> int:
    record = relay.resolve_escalation(args.dispatch_id, approved=False, state_dir=sd)
    if record is None:
        _emit(
            {"error": "no_escalation", "dispatch_id": args.dispatch_id},
            args.json,
            f"[x] no escalation found for {args.dispatch_id}",
        )
        return 1
    _emit(
        record,
        args.json,
        f"[ok] denied {args.dispatch_id} — operator must handle the worker pane directly",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Shared --json flag so it is accepted after the subcommand
    # (e.g. ``vnx permission window-status --json``).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")

    parser = argparse.ArgumentParser(
        prog="vnx permission",
        parents=[common],
        description="Worker-permission relay: auto-accept window + escalation control.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_open = sub.add_parser("window-open", parents=[common], help="open the operator auto-accept window")
    p_open.add_argument("--minutes", type=float, required=True, help="window duration in minutes")
    p_open.add_argument("--reason", default=None, help="why the window is open (audit)")
    p_open.set_defaults(func=_cmd_window_open)

    p_close = sub.add_parser("window-close", parents=[common], help="close the auto-accept window")
    p_close.set_defaults(func=_cmd_window_close)

    p_status = sub.add_parser("window-status", parents=[common], help="show window state")
    p_status.set_defaults(func=_cmd_window_status)

    p_esc = sub.add_parser("escalations", parents=[common], help="list pending permission escalations")
    p_esc.add_argument("--all", action="store_true", help="include resolved escalations")
    p_esc.set_defaults(func=_cmd_escalations)

    p_app = sub.add_parser("approve", parents=[common], help="approve an escalation + relay to the worker")
    p_app.add_argument("dispatch_id")
    p_app.set_defaults(func=_cmd_approve)

    p_deny = sub.add_parser("deny", parents=[common], help="deny an escalation")
    p_deny.add_argument("dispatch_id")
    p_deny.set_defaults(func=_cmd_deny)

    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sd = _state_dir()
    return args.func(args, sd)


if __name__ == "__main__":
    raise SystemExit(main())
