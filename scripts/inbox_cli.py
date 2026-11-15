#!/usr/bin/env python3
"""vnx inbox [--json] [--present] | ask | answer | push-due.
Exit 0 done, 2 refused (bad ref, secret, bad field; nothing written), 78 ledger unreadable."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_queue as oq
import operator_queue_ask as qa
import operator_queue_inbox as inbox
from project_root import resolve_project_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx inbox")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--state-dir", default="", help="queue state dir (tests)")
    parser.add_argument("--data-root", default="", help="project data root the readers use (tests)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--present", action="store_true", help="write a presented line per shown point")
    parser.add_argument("--channel", default="cli", choices=oq.CHANNELS)
    parser.add_argument("--sessie", default="cli")
    parser.add_argument("--limit", type=int, default=5)
    sub = parser.add_subparsers(dest="cmd")
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
    push = sub.add_parser("push-due", help="print the points a push is due for (JSON), after reserving them")
    push.add_argument("--sessie", dest="push_sessie", default="cli")
    return parser


def _point_lines(points: list, more: int) -> list:
    out = [f"  {p['point_id']}  {p['vraag']}  (age {p['age']}, presented {p['presented']}x)"
           + (f"\n    unknown: {p['unknown_reden']}" if p["unknown_reden"] else "")
           + (f"\n    {p['kopieerblok']}" if p["kopieerblok"] else "") for p in points]
    return out + ([f"  +{more} more"] if more else [])


def _render(data: dict) -> str:
    head = f"{data['project_id']}: {data['status']}" + (f" ({data['reason']})" if data["reason"] else "")
    return "\n".join([head] + _point_lines(data["points"], data["more"]))


def _show(args, project_id: str, sdir, root) -> int:
    data = inbox.show(project_id, limit=args.limit, state_dir=sdir, data_root=root)
    if args.present and data["points"]:
        inbox.present(project_id, [p["point_id"] for p in data["points"]], kanaal=args.channel, sessie=args.sessie,
                      state_dir=sdir)
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) if args.json else _render(data))
    return 0


def main(argv: list) -> int:
    args = _parser().parse_args(argv)
    project_id, sdir = args.project_id or resolve_project_id(), Path(args.state_dir) if args.state_dir else None
    root = Path(args.data_root) if args.data_root else (sdir.parent if sdir is not None else None)
    try:
        if args.cmd == "ask":
            line = qa.ask(project_id, bron=args.bron, ref=args.ref, vraag=args.vraag, sluit_bij=args.sluit_bij,
                          kopieerblok=args.kopieerblok, opened_by=args.opened_by, state_dir=sdir)
            print(f"point {line['point_id']} opened" if line else "point already open: nothing written")
        elif args.cmd == "answer":
            line = qa.answer(project_id, args.point_id, quote=args.quote, vastgelegd_door=args.by, state_dir=sdir)
            print(f"answer recorded for {line['point_id']}; the reader closes the point")
        elif args.cmd == "push-due":
            due = inbox.push_due(project_id, sessie=args.push_sessie, state_dir=sdir, data_root=root)
            if due:
                print(json.dumps(due, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            return _show(args, project_id, sdir, root)
    except (qa.RefRefused, oq.QueueRefused) as exc:
        print(f"vnx inbox: {exc}", file=sys.stderr)
        return 2
    except oq.QueueUnreadable as exc:
        print(f"vnx inbox: ledger unreadable: {exc}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
