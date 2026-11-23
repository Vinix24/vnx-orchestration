#!/usr/bin/env python3
"""vnx morning-round agenda --project-id X [--day YYYY-MM-DD] [--json]  (ADR-095, D2c)
       vnx morning-round tick [--project-id X] [--dry-run] | stop [--project-id X] --reason decided|deadline  (D4)

Exit codes: 0 done, 1 no agenda can be shown (no snapshot, unreadable ledger) or a project failed in tick/stop,
2 bad argument or unknown project.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import operator_queue as oq
from morning_agenda import AgendaUnreadable, build_agenda, render_text
from morning_snapshot import _check_day
from morning_round_routing import RoutingRefused, load_register
import morning_pause as mp
import morning_tick as mt
from morning_pause_gate import registry_getter
from morning_snapshot import PAUSE_END_REASONS


def _push(project_id: str, state_dir: Path) -> None:
    import operator_queue_inbox as inbox
    inbox.push_due(project_id, sessie="morning-round", state_dir=state_dir)  # reserves only; it is not the wake route


def _tick_or_stop(args: argparse.Namespace, now: datetime) -> int:
    try:
        register = load_register(Path(args.registry) if args.registry else None)
    except RoutingRefused as exc:
        print(f"morning-round: {exc}", file=sys.stderr)
        return 2
    if args.project_id and args.project_id not in register:
        print(f"morning-round: unknown project id {args.project_id!r}", file=sys.stderr)
        return 2
    failed = 0
    for pid in ([args.project_id] if args.project_id else sorted(register)):
        sdir = oq.state_dir_for(pid)
        try:
            get = registry_getter(pid, sdir)
            if args.command == "stop":
                window = mp.window_from_config(get)
                day = now.astimezone(mp._zone(window.tz)).date().isoformat()
                lines = [mt.stop_round(pid, day, args.reason, state_dir=sdir, now=now)]
            else:
                lines = mt.tick_project(pid, now, state_dir=sdir, get=get, tmux=mt.run_tmux, push=_push,
                                        fleet_dir=oq.state_dir_for(mt.FLEET_PROJECT), dry_run=args.dry_run)
        except Exception as exc:  # vnx-silent-except: printed and counted in the exit code; other projects still run
            print(f"morning-round: {pid}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue
        print("\n".join(lines))
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vnx morning-round")
    sub = parser.add_subparsers(dest="command", required=True)
    agenda = sub.add_parser("agenda", help="show the day snapshot with the live decision status")
    agenda.add_argument("--project-id", required=True)
    agenda.add_argument("--day", default=None, help="YYYY-MM-DD, default today (local date)")
    agenda.add_argument("--json", action="store_true", dest="as_json")
    agenda.add_argument("--state-dir", default=None, help="override the state dir (tests, debugging)")
    agenda.add_argument("--registry", default=None, help="override the project register")
    for name in ("tick", "stop"):
        cmd = sub.add_parser(name, help="start the round per project at the start time" if name == "tick"
                             else "end the round once per project and day")
        cmd.add_argument("--project-id", default=None, help="one project; default every project of the register")
        cmd.add_argument("--registry", default=None, help="override the project register")
        if name == "tick":
            cmd.add_argument("--dry-run", action="store_true", help="print what would happen, write nothing")
        else:
            cmd.add_argument("--reason", required=True, choices=sorted(PAUSE_END_REASONS))
    args = parser.parse_args(argv)
    now = datetime.now().astimezone()
    if args.command != "agenda":
        return _tick_or_stop(args, now)
    try:
        day = _check_day(args.day or now.strftime("%Y-%m-%d"))
        if args.project_id not in load_register(Path(args.registry) if args.registry else None):
            print(f"morning-round: unknown project id {args.project_id!r}", file=sys.stderr)
            return 2
        state_dir = Path(args.state_dir) if args.state_dir else oq.state_dir_for(args.project_id)
        result = build_agenda(args.project_id, day, state_dir=state_dir, now=now)
    except (ValueError, RoutingRefused) as exc:
        code = 1 if isinstance(exc, AgendaUnreadable) else 2
        print(f"morning-round: {exc}", file=sys.stderr)
        return code
    print(json.dumps(result, indent=2, sort_keys=True) if args.as_json else render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
