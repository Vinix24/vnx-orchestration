#!/usr/bin/env python3
"""pr_ready.py — `vnx pr-ready <N>`: what does this PR still need to merge?

Five lines per PR: what is complete, what is missing, and what closing each
gap costs. The manual version of this answer took several `gh` calls plus a
throwaway Python block, fourteen times in one night.

The measuring lives in ``scripts/lib/pr_readiness.py``, which reuses the
implementations that already decide each sub-answer (``ci_contexts``,
``gate_obligations``, ``closure_verifier``) rather than growing a second
opinion beside them. This file only renders and exits.

Exit codes:
  0  - READY: every required context passed and every declared gate is evidenced
  1  - NOT READY: measured, and something blocks
  2  - UNMEASURABLE: at least one section could not be read (never 0)
  10 - bad arguments / the PR itself could not be read

The split between 1 and 2 is the point. A gate that could not look must not
exit 0, and a script that treats "could not measure" as "not ready" hides the
difference between a PR that needs another CI run and a machine that cannot
reach GitHub.

Usage:
  python3 scripts/pr_ready.py 1703
  python3 scripts/pr_ready.py 1703 --json
  python3 scripts/pr_ready.py 1703 1704 --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ci_contexts  # noqa: E402
from pr_readiness import (  # noqa: E402
    VERDICT_NOT_READY,
    VERDICT_READY,
    VERDICT_UNMEASURABLE,
    PRReadinessError,
    Readiness,
    assess,
)
from vnx_paths import ensure_env  # noqa: E402

EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_UNMEASURABLE = 2
EXIT_BAD_INPUT = 10

_EXIT_BY_VERDICT = {
    VERDICT_READY: EXIT_READY,
    VERDICT_NOT_READY: EXIT_NOT_READY,
    VERDICT_UNMEASURABLE: EXIT_UNMEASURABLE,
}


def _context_line(report: Readiness) -> str:
    if report.contexts_error:
        return f"CI        UNMEASURABLE — {report.contexts_error}"
    if not report.contexts:
        return "CI        UNMEASURABLE — branch protection lists no required contexts"
    summary = ci_contexts.summarise(report.contexts)
    parts = [f"{summary['passed']}/{summary['total']} required contexts passed"]
    in_flight = [c for c in report.contexts if c.transient]
    if in_flight:
        parts.append(f"{len(in_flight)} in flight ({', '.join(c.context for c in in_flight)})")
    settled = [c for c in report.contexts if c.blocking and not c.transient]
    if settled:
        parts.append(
            f"{len(settled)} NOT satisfied ("
            + ", ".join(f"{c.context} [{c.state}]" for c in settled)
            + ")"
        )
    return "CI        " + " · ".join(parts)


def _gate_line(report: Readiness) -> str:
    if report.gates_error:
        return f"GATES     UNMEASURABLE — {report.gates_error}"
    if not report.declared_gates and not report.observed_gates:
        return (
            "GATES     none declared — no obligation record joins this PR, so there is no "
            "gate verdict to merge on"
        )
    parts = []
    for gate in report.gates:
        suffix = "" if gate.declared else " [off the door: no obligation]"
        if gate.satisfied:
            parts.append(f"{gate.gate} OK on head{suffix}")
        elif gate.verdict == VERDICT_UNMEASURABLE:
            parts.append(f"{gate.gate} UNMEASURABLE ({gate.message})")
        elif gate.record_sha and gate.record_sha != report.head_sha:
            parts.append(
                f"{gate.gate} NOT on head (newest record on {gate.record_sha[:12]}){suffix}"
            )
        elif gate.record_sha:
            parts.append(f"{gate.gate} incomplete ({gate.message}){suffix}")
        else:
            parts.append(f"{gate.gate} absent{suffix}")
    return "GATES     " + " · ".join(parts)


def render(report: Readiness, verbose: bool = False) -> str:
    facts = report.facts
    head = f"PR #{report.pr_number}  {facts.get('state', '?')}"
    if facts.get("isDraft"):
        head += " (DRAFT)"
    head += (
        f"  {facts.get('headRefName', '?')}  head {report.head_sha[:12]}"
        f"  mergeable={facts.get('mergeable', '?')}/{facts.get('mergeStateStatus', '?')}"
    )

    lines = [head, _context_line(report), _gate_line(report)]

    costs = report.costs()
    if costs:
        lines.append(f"COST      {costs[0]}")
        lines += [f"          {c}" for c in costs[1:]]
    else:
        lines.append("COST      nothing outstanding")

    verdict = report.verdict
    if verdict == VERDICT_UNMEASURABLE:
        reason = "; ".join(report.unmeasurable_reasons)
        lines.append(f"VERDICT   {verdict} — {reason}")
    elif verdict == VERDICT_NOT_READY:
        lines.append(f"VERDICT   {verdict} — " + "; ".join(report.blockers))
    else:
        lines.append(f"VERDICT   {verdict} — every required context passed, every declared gate evidenced")

    if verbose:
        lines.append("")
        for c in report.contexts:
            lines.append(f"  [{c.state:16}] {c.context} — {c.detail}")
        for g in report.gates:
            lines.append(f"  [{g.verdict:16}] {g.gate} — {g.message}")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vnx pr-ready",
        description="Merge-readiness for one or more PRs: required contexts, review-gate "
                    "evidence bound to the current head, and what closing each gap costs.",
    )
    parser.add_argument("pr", nargs="+", help="PR number(s)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="one line per context and gate")
    parser.add_argument(
        "--protected-branch", default="main",
        help="branch whose protection defines the required contexts (default: main)",
    )
    parser.add_argument("--project-root", default=None, help="project root (default: cwd)")
    parser.add_argument("--timeout", type=int, default=20, help="per-gh-call timeout in seconds")
    args = parser.parse_args(argv)

    try:
        numbers = [int(str(p).lstrip("#").upper().removeprefix("PR-")) for p in args.pr]
    except ValueError:
        print(f"pr-ready: PR arguments must be numbers, got {args.pr}", file=sys.stderr)
        return EXIT_BAD_INPUT

    project_root = Path(args.project_root) if args.project_root else Path.cwd()
    state_dir = Path(ensure_env()["VNX_STATE_DIR"])

    reports = []
    worst = EXIT_READY
    for number in numbers:
        try:
            report = assess(
                number, project_root, state_dir,
                protected_branch=args.protected_branch, timeout=args.timeout,
            )
        except PRReadinessError as exc:
            print(f"pr-ready: #{number} could not be read: {exc}", file=sys.stderr)
            worst = max(worst, EXIT_BAD_INPUT)
            continue
        reports.append(report)
        worst = max(worst, _EXIT_BY_VERDICT[report.verdict])

    if args.json:
        print(json.dumps([r.to_dict() for r in reports], indent=2))
    else:
        print("\n\n".join(render(r, verbose=args.verbose) for r in reports))
    return worst


if __name__ == "__main__":
    sys.exit(main())
