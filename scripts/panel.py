#!/usr/bin/env python3
"""`vnx panel` runner — multi-provider deliberation panel for complex, multi-view questions.

    python3 scripts/panel.py <mode> "<question>" [--context-file F] [--timeout S] [--out F] [--seats LIST] [--allow-degraded]

Modes: sweep | research | architecture | strategy. Runs the 4-stage deliberation
(diverge → contrarian → verify → synthesis) via the governed review-lane dispatcher, then
writes the cited report to unified_reports/ (and prints it).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from deliberation_panel import KERN_ROSTER_SEATS, default_roster, MODES, run_deliberation


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
        known = ", ".join(sorted(provider for provider, _ in default_roster()))
        raise ValueError(f"--seats must be a comma-list of known seats: {known}")

    known_roster = {provider: (provider, model) for provider, model in default_roster()}
    unknown = [seat for seat in seats if seat not in known_roster]
    if unknown:
        known = ", ".join(sorted(known_roster))
        bad = ", ".join(unknown)
        raise ValueError(f"unknown --seats value(s): {bad}; known seats: {known}")

    return [known_roster[seat] for seat in seats]


def _classify_panel_content(args: argparse.Namespace, context: str):
    """Content class of this panel run: cwd, --context-file, --out, the question and the context.

    The cwd decides for a run from a client folder: VNX_PROJECT_ID and the project root both
    say "dev" there (business-panel pins VNX_PROJECT_ID=vnx-dev for every run).
    """
    from content_class import classify_dispatch
    from dispatch_worktree_isolation import resolve_consumer_project_root

    return classify_dispatch(
        cwd=Path.cwd(),
        project_root=resolve_consumer_project_root(),
        project_id=os.environ.get("VNX_PROJECT_ID"),
        instruction="\n".join(part for part in (args.question, context) if part),
        extra_paths=[p for p in (args.context_file, args.out) if p],
    )


def _external_seats(roster: "list[tuple[str, str]]", content) -> "list[str]":
    """Seats of ``roster`` that may not read this content: outside the kern and not excepted."""
    from providers.constraint_enforcer import content_route_names

    granted = {name.lower() for name in content.exception_providers}
    external = []
    for provider, _model in roster:
        if provider in KERN_ROSTER_SEATS:
            continue
        sub = provider.split(":", 1)[1] if provider.startswith("litellm:") else None
        if granted & content_route_names(provider, sub):
            continue
        external.append(provider)
    return external


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="VNX multi-provider deliberation panel")
    parser.add_argument("mode", choices=sorted(MODES), help="deliberation mode")
    parser.add_argument("question", help="the question / target for the panel")
    parser.add_argument("--context-file", default=None, help="file whose contents ground every stage")
    parser.add_argument("--timeout", type=int, default=900, help="per-panelist timeout seconds")
    parser.add_argument("--out", default=None, help="write the report here (default: unified_reports/)")
    parser.add_argument("--seats", default=None, help="comma-list of seats to run; default = full fleet")
    parser.add_argument(
        "--allow-external-seats", default=None, metavar="REASON",
        help="client or personal content only: let the seats outside codex and claude run "
             "anyway. The reason is required, goes into the seat subprocess environment as the "
             "content-boundary override and into the report header",
    )
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

    allow_reason = args.allow_external_seats
    if allow_reason is not None and not allow_reason.strip():
        print("panel: --allow-external-seats needs a non-empty REASON", file=sys.stderr)
        return 2

    boundary_note = ""
    seat_env: "dict[str, str] | None" = None
    content = _classify_panel_content(args, context)
    if content.restricted or content.unreadable:
        what = f"{content.cls} content" if content.restricted else "content of unknown class (marker unreadable)"
        planned = roster if roster is not None else default_roster()
        external = _external_seats(planned, content)
        if allow_reason is not None and allow_reason.strip():
            if external:
                seat_env = {
                    "VNX_OVERRIDE_CONTENT_BOUNDARY": "1",
                    "VNX_OVERRIDE_CONTENT_BOUNDARY_REASON": allow_reason.strip(),
                }
                boundary_note = (
                    f"{what} (signal: {content.signal}); external seats allowed by "
                    f"--allow-external-seats: {', '.join(external)}. Reason: {allow_reason.strip()}"
                )
        elif roster is None:
            roster = [seat for seat in default_roster() if seat[0] not in external]
            seats = ", ".join(provider for provider, _ in roster)
            print(f"panel: {what} (signal: {content.signal}); running seats: {seats}", file=sys.stderr)
            boundary_note = f"{what} (signal: {content.signal}); seats limited to: {seats}"
        elif external:
            print(
                f"panel: {what} (signal: {content.signal}) may not go to seat(s): "
                f"{', '.join(external)}. Use the seats {', '.join(KERN_ROSTER_SEATS)} or pass "
                "--allow-external-seats REASON",
                file=sys.stderr,
            )
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
    # role="deliberation-panelist" (OI-811): _make_default_dispatcher defaults to
    # "plan-reviewer" for its original run_panel caller. A diverge/contrarian/verify/
    # synthesis prompt is NOT a plan review — tagging it plan-reviewer wrapped it in
    # "you are an independent plan reviewer... review the IMPLEMENTATION PLAN" framing,
    # which a plan-reviewer-role worker correctly rejected as not a plan, corrupting the
    # panel stage.
    dispatcher_kwargs = {"extra_env": seat_env} if seat_env else {}
    dispatcher = _make_default_dispatcher(
        data_dir, args.timeout, role="deliberation-panelist", **dispatcher_kwargs,
    )
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
    if boundary_note:
        result.boundary_note = boundary_note
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
    # OI-1358: 0/N seats delivering real content must never exit 0, even when no
    # min_seats floor is configured (or --allow-degraded let the synthesis run anyway) —
    # a caller must be able to detect a phantom-full panel without parsing the report.
    # getattr(..., False) keeps this safe against older/fake DeliberationResult doubles
    # in tests that predate this attribute.
    if getattr(result, "zero_seats_delivered", False):
        total = len(getattr(result, "fan_out", []))
        print(f"[panel] ZERO SEATS DELIVERED: 0/{total} lenses produced real content", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
