#!/usr/bin/env python3
"""vnx impact — the local ledger of the impact shadow and its report (ADR-074 besluit 7 and 8).

    vnx impact collect [--since <date|Nd>] [--limit N] [--state-dir <dir>]
    vnx impact backtest [--since <date|Nd>] [--limit N] [--state-dir <dir>]
    vnx impact report  [--since <date|Nd>] [--json] [--state-dir <dir>]

``collect`` pulls the ``impact-shadow`` and ``impact-shadow-job`` artifacts of the completed
``VNX CI`` runs through ``gh`` into ``state/impact_shadow.ndjson``, one line per run attempt,
never rewriting a line.  ``backtest`` rebuilds S for old pull_request runs from their
``profile-a-evidence`` with today's selector into ``state/impact_backtest.ndjson``.  ``report``
reads both ledgers only and writes nothing; every count is given for live, backtest and together.

Exit codes: 0 done (also when nothing is counted, the output says so), 1 collect or backtest could not
read some attempts (they are named and collected again next time), 2 bad input or no run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

ledger = importlib.import_module("impact_ledger")
backtest = importlib.import_module("impact_backtest")
resolve_state_dir = importlib.import_module("project_root").resolve_state_dir


def _seconds(block: dict) -> str:
    return "-" if block["median"] is None else f"median {block['median']:g}s, sum {block['sum']:g}s"


def _by_source(rep: dict) -> list:
    lines = ["", "## By source (live, backtest, together)"]
    for label, part in (*rep["by_source"].items(), ("together", rep)):
        share, red = part["reduced_share"], part["red_runs"]
        lines.append(f"- {label}: attempts {part['attempts_in_ledger']}, counted {share['counted']}, excluded {len(part['excluded'])}, "
                     f"reduced {share['reduced']}, misses {len(part['misses'])}, red runs {red['counted']} (reduced {red['reduced']})")
    return lines


def render_markdown(rep: dict) -> str:
    share, red, wall = rep["reduced_share"], rep["red_runs"], rep["wall_clock"]
    lines = ["# Impact shadow report", "",
             f"Attempts in the ledger: {rep['attempts_in_ledger']}. Counted: {len(rep['counted'])}."]
    if not rep["counted"]:
        lines.append("Nothing is counted yet: no attempt has a valid, reconstructible shadow record on a completed run.")
    lines += ["", "## Excluded (by name)"]
    lines += [f"- {e['attempt']}: {e['reason']}" for e in rep["excluded"]] or ["- none"]
    fraction = "-" if share["fraction"] is None else f"{share['fraction']:.1%}"
    lines += ["", "## Reduced share", f"{share['reduced']} of {share['counted']} counted attempts ran a smaller selection ({fraction})."]
    lines += ["", "## Misses (reduced attempts only)"]
    lines += [f"- {m['test_id']} (run {m['run_id']}/{m['run_attempt']})" for m in rep["misses"]] or ["- none"]
    lines += ["", "## Red runs",
              f"Counted attempts with a failing test: {red['counted']}, of which reduced: {red['reduced']}."]
    lines += ["", "## Wall clock", f"Taken over {wall['n']} attempts where the job that runs only S completed.",
              f"- Shadow job: {_seconds(wall['shadow_job_seconds'])}",
              f"- Profile A: {_seconds(wall['profile_a_seconds'])}"]
    return "\n".join(lines + _by_source(rep))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx impact", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="append the CI artifacts of new run attempts to the ledger")
    collect.add_argument("--limit", type=int, default=50, help="how many recent completed runs to read")
    back = sub.add_parser("backtest", help="rebuild S for old pull_request runs from their Profile A evidence")
    back.add_argument("--limit", type=int, default=50, help="how many recent completed runs to read")
    report = sub.add_parser("report", help="misses, reduced share and wall clock over the ledger")
    report.add_argument("--json", action="store_true", help="the report as JSON, sorted keys")
    for command in (collect, back, report):
        command.add_argument("--since", help="Nd, YYYY-MM-DD or an ISO datetime")
        command.add_argument("--state-dir", help="the state dir holding the ledgers (default: the project's)")
    return parser


def _both_ledgers(state: Path) -> list:
    """Live lines, then backtest lines; an unreadable backtest line is marked as one."""
    lines = ledger.read_ledger(ledger.ledger_path(state))
    return lines + [dict(e, source="backtest") if "_unreadable" in e else e
                    for e in ledger.read_ledger(backtest.backtest_path(state))]


def main(argv: Optional[List[str]] = None, runner: Optional[ledger.Runner] = None,
         out: Callable[[str], Any] = print, git: Optional[ledger.Runner] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        since = ledger.parse_since(args.since)
    except SystemExit as exc:
        return 2 if exc.code else 0
    except ValueError as exc:
        print(f"vnx impact: {exc}", file=sys.stderr)
        return 2
    state = Path(args.state_dir) if args.state_dir else resolve_state_dir(__file__)
    if args.command == "report":
        rep = ledger.report(ledger.filter_since(_both_ledgers(state), since))
        out(json.dumps(rep, sort_keys=True, indent=2) if args.json else render_markdown(rep))
        return 0
    try:
        if args.command == "backtest":
            result = backtest.backtest(state, runner or ledger.default_runner, git or backtest.default_git, since=since, limit=args.limit)
        else:
            result = ledger.collect(state, runner or ledger.default_runner, since=since, limit=args.limit)
    except (ledger.RunnerError, ValueError, KeyError, OSError) as exc:
        print(f"vnx impact {args.command}: could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    out(f"impact {'backtest ' if args.command == 'backtest' else ''}ledger: added {len(result['added'])}, already stored {len(result['skipped'])}, failed {len(result['failed'])}")
    for item in result["failed"]:
        out(f"  not collected, will be tried again: {item['attempt']}: {item['error']}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
