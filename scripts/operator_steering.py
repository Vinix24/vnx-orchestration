#!/usr/bin/env python3
"""Operator steering CLI (ADR-072): ``bind``, ``report`` and ``show`` (``--prompt`` or ``--case``).

Exit codes (docs/EXIT_CODES.md): 0 ok, 1 source not readable or prompt/case not found,
2 invalid arguments, a refused binding, or ``report`` on a source that is not ``ok``.
``show`` prints to the terminal and writes nothing; ``report`` writes only ``steering_report.json`` (no text).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering_report as report_lib
import operator_steering_source as source
from operator_steering_match import prompt_context
from project_root import resolve_central_data_dir, resolve_project_id


def _state_dir(args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir)
    return resolve_central_data_dir(resolve_project_id()) / "state"


def _cmd_bind(args: argparse.Namespace) -> int:
    try:
        record = source.bind(_state_dir(args), Path(args.projects_root), [args.transcript_dir], Path(args.memory_dir))
    except source.BindingError as exc:
        print(f"bind geweigerd: {exc}", file=sys.stderr)
        return 2
    print(f"gebonden: project {record['project_id']}, transcript-map {args.transcript_dir}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    result = source.read_prompts(_state_dir(args), Path(args.projects_root))
    if result.state != "ok":
        names = ", ".join(result.refused_files)
        print(f"bron {result.state}: {result.reason}" + (f" ({names})" if names else ""), file=sys.stderr)
        return 1
    for prompt in result.prompts:
        if prompt.prompt_id == args.prompt:
            print(f"{prompt.source_file}:{prompt.line}")
            print(prompt.text)
            return 0
    counts = ", ".join(f"{k}={v}" for k, v in sorted(result.excluded.items())) or "geen"
    print(f"prompt-id niet gevonden onder de doorgelaten prompts; uitgesloten per reden: {counts}", file=sys.stderr)
    return 1


def _parse_window(value: str, now: datetime) -> datetime:
    """``<N>d`` days before now, or an ISO time that names its zone."""
    days = re.fullmatch(r"(\d+)d", value.strip())
    if days:
        return now - timedelta(days=int(days.group(1)))
    parsed = report_lib._parse_ts(value)
    if parsed is None:
        raise ValueError(f"tijdvenster {value!r}: gebruik <N>d of een ISO-tijd met tijdzone")
    return parsed


def _window(args: argparse.Namespace) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return _parse_window(args.since, now), (_parse_window(args.until, now) if args.until else now)


def _source_problem(report: dict) -> str:
    bad = [(n, b) for n, b in report["bronstaat"].items() if b["state"] != "ok"]
    return "; ".join(f"bron {n} {b['state']}: {b['reason']}" + (f" ({', '.join(b['refused_files'])})" if b["refused_files"] else "")
                     for n, b in bad)


def _live_texts(state_dir: Path, projects_root: Path) -> tuple:
    """(prompts by id, assistant turns by id) of a live, contained read; the text never leaves this process."""
    collected = report_lib._Collected()
    source.read_sources(state_dir, projects_root, report_lib._consumer(collected))
    return {p.prompt_id: p for p in collected.prompts}, collected.turns


def _md(report: dict, texts: Optional[Dict[str, str]] = None) -> str:
    out = [f"# steering_report/1: {report['project_id']}", "",
           f"venster {report['venster']['since']} tot {report['venster']['until']}, status {report['status']}, dekking {report['dekking']}", "",
           "| klasse | aantal |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in report["tellingen"].items()]
    out += ["", "## Herhalingsgroepen", ""] + [f"- {g['group_id']} {g['klasse']}: {g['sessies']} sessies, {len(g['prompt_ids'])} prompts" for g in report["herhalingsgroepen"]]
    out += ["", "## Notities", ""] + [f"- {n['naam']}: {n['status']}" for n in report["notities"]]
    out += ["", "## Gevallen", ""]
    for c in report["gevallen"]:
        out.append(f"- {c['case_id']} {c['klasse']} ({c['reden']}), {c['sessies']} sessies, prompts: {', '.join(c['prompt_ids'])}")
        for pid in c["prompt_ids"] if texts is not None else []:
            out.append(f"    {pid}: {texts.get(pid, '(tekst niet meer leesbaar)')}")
    return "\n".join(out)


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        since, until = _window(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state_dir, projects_root = _state_dir(args), Path(args.projects_root)
    report = report_lib.build_report(state_dir, projects_root, since, until)
    report_lib.write_report(state_dir, report)
    problem = _source_problem(report)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if report["status"] == "low_coverage":
        print(f"low_coverage: dekking {report['dekking']}, het rapport is geldig maar een deel van de prompts is niet geclassificeerd", file=sys.stderr)
    texts = None
    if args.show:
        prompts, _turns = _live_texts(state_dir, projects_root)
        texts = {pid: p.text for pid, p in prompts.items()}
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _md(report, texts))
    return 0


def _show_context(prompt, turn) -> List[str]:
    ctx = prompt_context(prompt, turn)
    calls = [f"{t.name}: {t.first_line}" if t.first_line else t.name for t in ctx.tool_calls]
    return [f"  tool-aanroepen: {'; '.join(calls) or 'geen'}",
            f"  dispatches: {', '.join(ctx.dispatch_ids) or 'geen'}",
            f"  PR's: {', '.join('#' + str(n) for n in ctx.pr_numbers) or 'geen'}"]


def _cmd_show_case(args: argparse.Namespace) -> int:
    try:
        since, until = _window(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state_dir, projects_root = _state_dir(args), Path(args.projects_root)
    report = report_lib.build_report(state_dir, projects_root, since, until)
    problem = _source_problem(report)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    case = next((c for c in report["gevallen"] if c["case_id"] == args.case), None)
    if case is None:
        print(f"case-id niet gevonden in het venster {report['venster']['since']} tot {report['venster']['until']}", file=sys.stderr)
        return 1
    prompts, turns = _live_texts(state_dir, projects_root)
    print(f"case {case['case_id']} klasse {case['klasse']} reden {case['reden']}")
    for pid in case["prompt_ids"]:
        prompt = prompts.get(pid)
        if prompt is None:
            print(f"{pid}: niet meer leesbaar in de gebonden bron")
            continue
        print(f"{pid} {prompt.ts.isoformat()}")
        print(prompt.text)
        print("\n".join(_show_context(prompt, turns.get(pid))))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="project state dir (default: central store of this project)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="bind this project to a transcript dir and a memory dir")
    bind.add_argument("--transcript-dir", required=True, help="slug directly under the projects root; write --transcript-dir=<slug>, slugs start with a dash")
    bind.add_argument("--memory-dir", required=True)
    bind.set_defaults(func=_cmd_bind)
    report = sub.add_parser("report", help="steering_report/1 for a window; writes steering_report.json (no text)")
    report.add_argument("--since", required=True, help="<N>d or an ISO time with zone")
    report.add_argument("--until", help="<N>d or an ISO time with zone (default: now)")
    form = report.add_mutually_exclusive_group(required=True)
    form.add_argument("--json", action="store_true", help="print the report dict")
    form.add_argument("--md", action="store_true", help="print counts, coverage, groups, notes and case ids")
    form.add_argument("--show", action="store_true", help="like --md plus the prompt text of each case, to the terminal only")
    report.set_defaults(func=_cmd_report)
    show = sub.add_parser("show", help="print one passed prompt, or the prompts and context of one case")
    which = show.add_mutually_exclusive_group(required=True)
    which.add_argument("--prompt", help="prompt id (16 hex)")
    which.add_argument("--case", help="case id (steering_report/1 gevallen[].case_id)")
    show.add_argument("--since", default="30d", help="window for --case: <N>d or ISO (default 30d)")
    show.add_argument("--until", help="window end for --case (default: now)")
    show.set_defaults(func=lambda a: _cmd_show_case(a) if a.case else _cmd_show(a))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
