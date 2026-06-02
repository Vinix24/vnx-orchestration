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
``approve`` relays the approval keystroke ("1" + a SEPARATE Enter) into the worker's
tmux session and marks the escalation resolved=approved ONLY on confirmed delivery.
If the keystroke does not land (no live session / send failure) the escalation is
left pending and ``approve`` exits non-zero with a clear message, so a failed relay
is never silently recorded as approved.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _relay_keystroke_to_worker(
    dispatch_id: str, sd: Path, *, runner=None
) -> "tuple[str, str | None]":
    """Best-effort: send "1"+Enter into the escalated worker's tmux session.

    Reads the lane handle (state/tmux_interactive/<dispatch_id>.json) for the
    session id and relays the approval keystroke ("1" + a SEPARATE Enter) to the
    EXPLICIT session. Returns a ``(status, session)`` tuple:

      - ("sent", session)        approval keystroke confirmed delivered
      - ("no_session", None)     no live worker session found (handle / session field absent)
      - ("send_failed", session) session exists but the send-keys did not land
      - ("error", session|None)  operational failure (handle read / tmux / send raised)

    Distinguishing a genuine "no live session" from an operational failure lets the
    caller avoid marking an escalation approved when nothing was delivered. Never
    raises — but, unlike before, the actual exception is logged with context so a
    swallowed tmux/read/send failure is observable.
    """
    handle_path = sd / "tmux_interactive" / f"{relay._safe_dispatch_id(dispatch_id)}.json"
    if not handle_path.exists():
        return ("no_session", None)
    try:
        handle = json.loads(handle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "permission approve: handle read failed dispatch=%s path=%s (%s)",
            dispatch_id, handle_path, exc,
        )
        return ("error", None)
    session = (handle or {}).get("session")
    if not session:
        return ("no_session", None)
    try:
        if runner is None:
            from tmux_interactive_dispatch import TmuxCommandRunner  # noqa: PLC0415

            runner = TmuxCommandRunner()
        if not runner.available():
            logger.warning(
                "permission approve: tmux unavailable, cannot relay dispatch=%s session=%s",
                dispatch_id, session,
            )
            return ("error", session)
        delivered = relay._send_approval(runner, session)
        if not delivered:
            logger.warning(
                "permission approve: send-keys did NOT land dispatch=%s session=%s",
                dispatch_id, session,
            )
            return ("send_failed", session)
        return ("sent", session)
    except Exception as exc:  # noqa: BLE001 — best-effort, but now observable
        logger.warning(
            "permission approve: relay to worker failed dispatch=%s session=%s (%s)",
            dispatch_id, session, exc, exc_info=True,
        )
        return ("error", session)


def _cmd_approve(args, sd: Path) -> int:
    try:
        relay._safe_dispatch_id(args.dispatch_id)
    except ValueError as exc:
        _emit(
            {"error": "invalid_dispatch_id", "dispatch_id": args.dispatch_id, "detail": str(exc)},
            args.json,
            f"[x] invalid dispatch_id {args.dispatch_id!r}: {exc}",
        )
        return 2

    existing = relay.read_escalation(args.dispatch_id, state_dir=sd)
    if existing is None:
        _emit(
            {"error": "no_escalation", "dispatch_id": args.dispatch_id},
            args.json,
            f"[x] no escalation found for {args.dispatch_id}",
        )
        return 1

    # Relay FIRST; only mark the escalation approved on confirmed keystroke delivery.
    # A failed/absent relay must NOT silently flip the record to approved.
    status, session = _relay_keystroke_to_worker(args.dispatch_id, sd)

    if status == "sent":
        record = relay.resolve_escalation(args.dispatch_id, approved=True, state_dir=sd) or existing
        record["relayed_session"] = session
        _emit(
            record,
            args.json,
            f"[ok] approved {args.dispatch_id} — relayed approval to session {session}",
        )
        return 0

    # Delivery did not land — leave the escalation PENDING so it can be retried,
    # and surface a clear failure (non-zero exit) distinguishing the cause.
    if status == "no_session":
        human = (
            f"[x] approve {args.dispatch_id} NOT applied — no live worker session found; "
            "escalation left pending (use `deny` to clear, or retry once the worker is up)"
        )
    elif status == "send_failed":
        human = (
            f"[x] approve {args.dispatch_id} FAILED — send-keys to session {session} did not land; "
            "escalation left pending (retry)"
        )
    else:  # "error"
        human = (
            f"[x] approve {args.dispatch_id} FAILED — relay error reaching the worker "
            "(see logs); escalation left pending"
        )
    payload = {
        "error": status,
        "dispatch_id": args.dispatch_id,
        "approved": False,
        "relayed_session": session,
    }
    _emit(payload, args.json, human)
    return 1


def _cmd_deny(args, sd: Path) -> int:
    try:
        relay._safe_dispatch_id(args.dispatch_id)
    except ValueError as exc:
        _emit(
            {"error": "invalid_dispatch_id", "dispatch_id": args.dispatch_id, "detail": str(exc)},
            args.json,
            f"[x] invalid dispatch_id {args.dispatch_id!r}: {exc}",
        )
        return 2
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
