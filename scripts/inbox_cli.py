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
import operator_queue_audit as audit
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
    parser.add_argument("--all", action="store_true", help="every project: one coverage line, then its points")
    parser.add_argument("--now", default="", help=argparse.SUPPRESS)
    parser.add_argument("--logdir", default="", help="besluiten register dir (tests); default <project root>/daily-log")
    parser.add_argument("--home", default="", help=argparse.SUPPRESS)
    parser.add_argument("--registry", default="", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd")
    ask = sub.add_parser("ask", help="open a point")
    ask.add_argument("--bron", required=True, choices=sorted(qa.BRON))
    ask.add_argument("--ref", required=True, help="; ".join(qa.FORMS) + "; or 'antwoord' with --bron vraag")
    for name in ("--vraag", "--sluit-bij"):
        ask.add_argument(name, required=True)
    ask.add_argument("--kopieerblok", default="")
    ask.add_argument("--opened-by", default="T0")
    ask.add_argument("--ceiling", type=int, default=None, help="pr:<n> only: the line ceiling asked for (above 0)")
    ask.add_argument("--reason", default=None, help="pr:<n> only: " + ", ".join(sorted(oq.GRANT_REASONS)))
    ans = sub.add_parser("answer", help="record the operator's own words for an antwoord: or pr: point")
    ans.add_argument("point_id")
    ans.add_argument("--quote", required=True)
    ans.add_argument("--by", default="T0")
    push = sub.add_parser("push-due", help="print the points a push is due for (JSON), after reserving them")
    push.add_argument("--sessie", dest="push_sessie", default="cli")
    for name, text in (("audit", "late points per project (ADR-076 decision 13)"),
                       ("compact", "drop points closed more than 180 days ago (decision 12)")):
        cmd = sub.add_parser(name, help=text)
        for flag, kw in (("--all", {"action": "store_true"}), ("--json", {"action": "store_true"}), ("--project-id", {})):
            cmd.add_argument(flag, default=argparse.SUPPRESS, **kw)  # SUPPRESS: the main parser's value stands when absent
        if name == "audit":
            cmd.add_argument("--since", required=True)
        else:
            cmd.add_argument("--dry-run", action="store_true")
    return parser


def _point_lines(points: list, more: int) -> list:
    out = [f"  {p['point_id']}  {p['vraag']}  (age {p['age']}, presented {p['presented']}x)"
           + (f"\n    unknown: {p['unknown_reden']}" if p["unknown_reden"] else "")
           + (f"\n    {p['kopieerblok']}" if p["kopieerblok"] else "") for p in points]
    return out + ([f"  +{more} more"] if more else [])


def _source_lines(coverage) -> list:
    return [f"  bron {name}: unknown ({str(reden)[:inbox.REDEN_MAX]})" for name, reden in inbox.unknown_sources(coverage)]


def _render(data: dict) -> str:
    head = f"{data['project_id']}: {data['status']}" + (f" ({data['reason']})" if data["reason"] else "")
    return "\n".join([head] + _source_lines(data["coverage"]) + _point_lines(data["points"], data["more"]))


def _show_all(args) -> int:
    home = Path(args.home) if args.home else Path.home()
    data = inbox.show_all(home=home, registry_path=Path(args.registry) if args.registry else None, limit=args.limit)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    reg = data["registry"]
    lines = [f"registry: {reg['dekking']}" + (f" ({reg['reden']})" if reg["reden"] else "")]
    sto = data["store"]
    lines.append(f"store: {sto['dekking']}" + (f" ({sto['reden']})" if sto["reden"] else ""))
    for p in data["projects"]:
        total = "?" if p["open_total"] is None else p["open_total"]
        lines.append(f"{p['project_id']}: {p['dekking']}" + (f" ({p['reden']})" if p["reden"] else "") + f", open {total}")
        lines += _source_lines(p["sources"]) + _point_lines(p["points"], p["more"])
    print("\n".join(lines))
    return 0


def _targets(args, home: Path) -> list:
    """[(project_id, state_dir)] for --all (the one discovery of `vnx inbox --all`) or the single project."""
    return [(p["project_id"], home / ".vnx-data" / p["project_id"] / "state") if p["in_store"] else (p["project_id"], None)
            for p in inbox.discover_projects(home=home, registry_path=Path(args.registry) if args.registry else None)["projects"]]


def _audit(args, project_id: str, sdir, root) -> int:
    try:
        days = audit.parse_since(args.since)
    except ValueError as exc:
        print(f"vnx inbox: {exc}", file=sys.stderr)
        return 2
    now = args.now or None
    if args.all:
        results = audit.audit_all(home=Path(args.home) if args.home else Path.home(),
                                  registry_path=Path(args.registry) if args.registry else None, now=now, since_days=days)
    else:
        results = [audit.audit_project(project_id, state_dir=sdir, data_root=root, now=now, since_days=days)]
    print(json.dumps({"since_days": days, "projects": results}, indent=2, ensure_ascii=False, sort_keys=True)
          if args.json else audit.render(results))
    return audit.exit_code(results)


def _compact(args, project_id: str, sdir) -> int:
    now = inbox._parse(args.now) if args.now else None
    if args.all:
        home = Path(args.home) if args.home else Path.home()
        results = [oq.compact(pid, now=now, dry_run=args.dry_run, state_dir=sd) if sd is not None and sd.is_dir() else
                   {"project_id": pid, "status": "unknown", "reason": "geen state/", "line": None, "points": 0, "lines": 0}
                   for pid, sd in _targets(args, home)]
    else:
        results = [oq.compact(project_id, now=now, dry_run=args.dry_run, state_dir=sdir)]
    lines = [f"{r['project_id']}: " + (f"unknown ({r['reason']})" if r["status"] != "ok" else
             f"{'would remove' if args.dry_run else 'removed'} {r['points']} points, {r['lines']} lines") for r in results]
    print(json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True) if args.json else "\n".join(lines))
    return 3 if any(r["status"] != "ok" for r in results) else 0


def _show(args, project_id: str, sdir, root) -> int:
    data = inbox.show(project_id, limit=args.limit, state_dir=sdir, data_root=root)
    if args.present and data["points"]:
        inbox.present(project_id, [p["point_id"] for p in data["points"]], kanaal=args.channel, sessie=args.sessie,
                      state_dir=sdir)
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) if args.json else _render(data))
    return 0


def _sync_decision(args, project_id: str, sdir, point_id: str) -> None:
    """Decision for the answer just recorded; a failure is named on stderr and never undoes the answer."""
    try:
        import besluiten_adapter
        from project_root import resolve_project_root
        logdir = Path(args.logdir) if args.logdir else resolve_project_root(__file__) / "daily-log"
        res = besluiten_adapter.sync_answers(project_id, logdir, state_dir=sdir)
    except Exception as exc:  # vnx-silent-except: the answer is recorded; the failure is named on stderr and in the index
        print(f"vnx inbox: decision not recorded: {type(exc).__name__}", file=sys.stderr)
        return
    if point_id in res["ids"]:
        print(f"decision {res['ids'][point_id]} in {logdir}/besluiten.ndjson")
    for pid, reason in res["refused"]:
        print(f"vnx inbox: decision refused for {pid}: {reason}", file=sys.stderr)


def main(argv: list) -> int:
    args = _parser().parse_args(argv)
    if args.all and (args.present or args.cmd not in (None, "audit", "compact") or args.project_id or args.state_dir or args.data_root):
        print("vnx inbox: --all only shows; ask, answer, push-due, --present and a project scope are per project", file=sys.stderr)
        return 2
    if args.all and args.cmd == "audit":
        return _audit(args, "", None, None)
    if args.all and args.cmd == "compact":
        return _compact(args, "", None)
    if args.all:  # lists every project itself: needs no current project
        return _show_all(args)
    project_id, sdir = args.project_id or resolve_project_id(), Path(args.state_dir) if args.state_dir else None
    root = Path(args.data_root) if args.data_root else (sdir.parent if sdir is not None else None)
    if args.cmd == "audit":
        return _audit(args, project_id, sdir, root)
    if args.cmd == "compact":
        return _compact(args, project_id, sdir)
    try:
        if args.cmd == "ask":
            line = qa.ask(project_id, bron=args.bron, ref=args.ref, vraag=args.vraag, sluit_bij=args.sluit_bij,
                          kopieerblok=args.kopieerblok, opened_by=args.opened_by, state_dir=sdir,
                          ceiling=args.ceiling, reason=args.reason)
            print(f"point {line['point_id']} opened" if line else "point already open: nothing written")
        elif args.cmd == "answer":
            line = qa.answer(project_id, args.point_id, quote=args.quote, vastgelegd_door=args.by, state_dir=sdir)
            print(f"answer recorded for {line['point_id']}; the reader closes the point")
            _sync_decision(args, project_id, sdir, line["point_id"])
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
