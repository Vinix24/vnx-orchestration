#!/usr/bin/env python3
"""vnx inbox ask | answer. Exit 0 done, 2 refused (bad ref, secret, bad field; nothing written), 78 ledger unreadable."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_queue as oq
import operator_queue_ask as qa
from project_root import resolve_project_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx inbox")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--state-dir", default="", help="queue state dir (tests)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="open a point")
    ask.add_argument("--bron", required=True, choices=sorted(qa.BRON))
    ask.add_argument("--ref", required=True, help="; ".join(qa.FORMS) + "; or 'antwoord' with --bron vraag")
    for name in ("--vraag", "--sluit-bij"):
        ask.add_argument(name, required=True)
    ask.add_argument("--kopieerblok", default="")
    ask.add_argument("--opened-by", default="T0")
    ans = sub.add_parser("answer", help="record the operator's own words for an antwoord: point")
    ans.add_argument("point_id")
    ans.add_argument("--quote", required=True)
    ans.add_argument("--by", default="T0")
    return parser


def main(argv: list) -> int:
    args = _parser().parse_args(argv)
    project_id, sdir = args.project_id or resolve_project_id(), Path(args.state_dir) if args.state_dir else None
    try:
        if args.cmd == "ask":
            line = qa.ask(project_id, bron=args.bron, ref=args.ref, vraag=args.vraag, sluit_bij=args.sluit_bij,
                          kopieerblok=args.kopieerblok, opened_by=args.opened_by, state_dir=sdir)
            print(f"point {line['point_id']} opened" if line else "point already open: nothing written")
        else:
            line = qa.answer(project_id, args.point_id, quote=args.quote, vastgelegd_door=args.by, state_dir=sdir)
            print(f"answer recorded for {line['point_id']}; the reader closes the point")
    except (qa.RefRefused, oq.QueueRefused) as exc:
        print(f"vnx inbox: {exc}", file=sys.stderr)
        return 2
    except oq.QueueUnreadable as exc:
        print(f"vnx inbox: ledger unreadable: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
