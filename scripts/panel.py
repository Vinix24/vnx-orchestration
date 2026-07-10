#!/usr/bin/env python3
"""`vnx panel` runner — multi-provider deliberation panel for complex, multi-view questions.

    python3 scripts/panel.py <mode> "<question>" [--context-file F] [--timeout S] [--out F]

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

from deliberation_panel import MODES, run_deliberation  # noqa: E402


def _resolve_reports_dir() -> Path:
    try:
        from vnx_paths import resolve_state_dir  # noqa: PLC0415
        base = resolve_state_dir().parent
    except Exception:
        base = Path(__file__).resolve().parents[1] / ".vnx-data"
    d = base / "unified_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="VNX multi-provider deliberation panel")
    parser.add_argument("mode", choices=sorted(MODES), help="deliberation mode")
    parser.add_argument("question", help="the question / target for the panel")
    parser.add_argument("--context-file", default=None, help="file whose contents ground every stage")
    parser.add_argument("--timeout", type=int, default=900, help="per-panelist timeout seconds")
    parser.add_argument("--out", default=None, help="write the report here (default: unified_reports/)")
    args = parser.parse_args(argv)

    context = ""
    if args.context_file:
        try:
            context = Path(args.context_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"panel: cannot read --context-file: {exc}", file=sys.stderr)
            return 2

    from plan_gate_panel import _make_default_dispatcher  # noqa: PLC0415
    # Pass a REAL data_dir: the claude/tmux lane writes each report to
    # <data_dir>/unified_reports/<id>.md and _read_report falls back to that path. With
    # data_dir=None the claude-lane reports (fan-out + synthesis) are written but never found
    # → no cited synthesis (sales-copilot T0, 2026-07-10). unified_reports_dir().parent IS
    # that data_dir, so the write-path and read-path agree.
    data_dir = str(_resolve_reports_dir().parent)
    dispatcher = _make_default_dispatcher(data_dir, args.timeout)

    print(f"[panel] mode={args.mode} — running 4-stage deliberation across the fleet ...", file=sys.stderr)
    result = run_deliberation(args.mode, args.question, dispatcher=dispatcher, context=context)
    report = result.to_report()

    out = Path(args.out) if args.out else (_resolve_reports_dir() / f"panel-{args.mode}-{uuid.uuid4().hex[:8]}.md")
    try:
        out.write_text(report, encoding="utf-8")
        print(f"[panel] report -> {out}", file=sys.stderr)
    except OSError as exc:
        print(f"panel: could not write report: {exc}", file=sys.stderr)

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
