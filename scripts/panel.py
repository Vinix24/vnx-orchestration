#!/usr/bin/env python3
"""`vnx panel` runner — multi-provider deliberation panel for complex, multi-view questions.

    python3 scripts/panel.py <mode> "<question>" [--context-file F] [--timeout S] [--out F] [--seats LIST] [--allow-degraded]

Modes: sweep | research | architecture | strategy. Runs the 4-stage deliberation
(diverge → contrarian → verify → synthesis) via the governed review-lane dispatcher, then
writes the cited report to unified_reports/ (and prints it).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from deliberation_panel import DEFAULT_ROSTER, MODES, run_deliberation  # noqa: E402


def _resolve_reports_dir() -> Path:
    try:
        from vnx_paths import resolve_state_dir  # noqa: PLC0415
        base = resolve_state_dir().parent
    except Exception:
        base = Path(__file__).resolve().parents[1] / ".vnx-data"
    d = base / "unified_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_seats(value: str | None) -> "list[tuple[str, str]] | None":
    if value is None:
        return None

    seats = [part.strip() for part in value.split(",")]
    if not seats or any(not seat for seat in seats):
        known = ", ".join(sorted(provider for provider, _ in DEFAULT_ROSTER))
        raise ValueError(f"--seats must be a comma-list of known seats: {known}")

    known_roster = {provider: (provider, model) for provider, model in DEFAULT_ROSTER}
    unknown = [seat for seat in seats if seat not in known_roster]
    if unknown:
        known = ", ".join(sorted(known_roster))
        bad = ", ".join(unknown)
        raise ValueError(f"unknown --seats value(s): {bad}; known seats: {known}")

    return [known_roster[seat] for seat in seats]


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="VNX multi-provider deliberation panel")
    parser.add_argument("mode", choices=sorted(MODES), help="deliberation mode")
    parser.add_argument("question", help="the question / target for the panel")
    parser.add_argument("--context-file", default=None, help="file whose contents ground every stage")
    parser.add_argument("--timeout", type=int, default=900, help="per-panelist timeout seconds")
    parser.add_argument("--out", default=None, help="write the report here (default: unified_reports/)")
    parser.add_argument("--seats", default=None, help="comma-list of seats to run; default = full fleet")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="proceed with the synthesis even when fewer than the configured "
             "minimum seats delivered a report (the choice is stamped on the report)",
    )
    args = parser.parse_args(argv)

    try:
        roster = _parse_seats(args.seats)
    except ValueError as exc:
        print(f"panel: {exc}", file=sys.stderr)
        return 2

    context = ""
    if args.context_file:
        try:
            context = Path(args.context_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"panel: cannot read --context-file: {exc}", file=sys.stderr)
            return 2

    from plan_gate_panel import (  # noqa: PLC0415
        _make_default_dispatcher,
        load_synthesis_min_seats,
    )
    # Pass a REAL data_dir: the claude/tmux lane writes each report to
    # <data_dir>/unified_reports/<id>.md and _read_report falls back to that path. With
    # data_dir=None the claude-lane reports (fan-out + synthesis) are written but never found
    # → no cited synthesis (sales-copilot T0, 2026-07-10). unified_reports_dir().parent IS
    # that data_dir, so the write-path and read-path agree.
    data_dir = str(_resolve_reports_dir().parent)
    # role="research-analyst" (OI-811, corrected OI-1359): _make_default_dispatcher
    # defaults to "plan-reviewer" for its original run_panel caller. A diverge/
    # contrarian/verify/synthesis prompt is NOT a plan review — tagging it
    # plan-reviewer wrapped it in "you are an independent plan reviewer... review the
    # IMPLEMENTATION PLAN" framing, which a plan-reviewer-role worker correctly
    # rejected as not a plan, corrupting the panel stage. OI-811 fixed that call site
    # but invented role="deliberation-panelist", which exists in NEITHER register
    # (no agents/deliberation-panelist/, no profile in worker_permissions.yaml) — every
    # seat then failed fail-closed in resolve_worker_profile before producing any
    # content (OI-1359). "research-analyst" is a real role in both registers: it
    # analyzes and writes only its own report (file_write_scope limited to
    # unified_reports/**, Edit/MultiEdit denied) — the posture a panel seat needs.
    # It is also not "plan-reviewer", so the branch in _make_default_dispatcher below
    # still gives it the generic (non-plan-framed) file-ref instruction, preserving
    # the OI-811 fix.
    dispatcher = _make_default_dispatcher(data_dir, args.timeout, role="research-analyst")
    # OI-1154: the synthesis coverage floor comes from the config, never a Python literal.
    min_seats = load_synthesis_min_seats()

    print(f"[panel] mode={args.mode} — running 4-stage deliberation across the fleet ...", file=sys.stderr)
    if roster is None:
        result = run_deliberation(
            args.mode, args.question, dispatcher=dispatcher, context=context,
            min_seats=min_seats, allow_degraded=args.allow_degraded,
        )
    else:
        result = run_deliberation(
            args.mode, args.question, dispatcher=dispatcher, context=context, roster=roster,
            min_seats=min_seats, allow_degraded=args.allow_degraded,
        )
    report = result.to_report()

    out = Path(args.out) if args.out else (_resolve_reports_dir() / f"panel-{args.mode}-{uuid.uuid4().hex[:8]}.md")
    try:
        out.write_text(report, encoding="utf-8")
        print(f"[panel] report -> {out}", file=sys.stderr)
    except OSError as exc:
        print(f"panel: could not write report: {exc}", file=sys.stderr)

    print(report)
    if result.synthesis_refused_reason:
        print(f"[panel] SYNTHESIS REFUSED: {result.synthesis_refused_reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
