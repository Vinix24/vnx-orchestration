#!/usr/bin/env python3
"""vnx morning-round agenda --project-id X [--day YYYY-MM-DD] [--json]  (ADR-095, D2c)

Exit codes: 0 agenda shown, 1 no agenda can be shown (no snapshot, unreadable ledger), 2 bad argument or unknown project.
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vnx morning-round")
    sub = parser.add_subparsers(dest="command", required=True)
    agenda = sub.add_parser("agenda", help="show the day snapshot with the live decision status")
    agenda.add_argument("--project-id", required=True)
    agenda.add_argument("--day", default=None, help="YYYY-MM-DD, default today (local date)")
    agenda.add_argument("--json", action="store_true", dest="as_json")
    agenda.add_argument("--state-dir", default=None, help="override the state dir (tests, debugging)")
    agenda.add_argument("--registry", default=None, help="override the project register")
    args = parser.parse_args(argv)
    now = datetime.now().astimezone()
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
