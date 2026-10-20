#!/usr/bin/env python3
"""vnx costs — the cost ledger per run (ADR-043).

``vnx costs replay`` moves the records that waited in
``events/provider_costs.spool/`` into the ledger
(``events/provider_costs.ndjson``). A record whose ``run_id`` + ``record_id``
already stands in the ledger is not written a second time, and a spool file is
removed only after its lines landed. An unreadable spool file stays where it is
and is named. The dispatcher prelude runs this on its tick; the T0 can run it by
hand.

The ledger is the one ``provider_costs._resolve_costs_path`` resolves, the same
file every writer appends to.

Exit codes:
  0  the spool is empty after the replay (or there was none)
  1  spool files remain: unreadable ones, named on stdout
  2  the replay failed: the ledger could not be read or written; the spool stays

Usage:
  python3 scripts/costs_cli.py replay [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import cost_ledger  # noqa: E402

EXIT_OK = 0
EXIT_PENDING = 1
EXIT_FAILED = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx costs", description="the cost ledger per run (ADR-043)")
    subs = parser.add_subparsers(dest="verb", required=True)
    replay = subs.add_parser("replay", help="move spooled cost records into the ledger, once per record")
    replay.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def _render(result: "cost_ledger.ReplayResult") -> str:
    text = (
        f"cost-ledger replay: {result.written} line(s) written, "
        f"{result.already_present} already in the ledger, "
        f"{len(result.removed)} spool file(s) removed, {result.pending} pending"
    )
    if result.unreadable:
        text += "\nunreadable spool file(s), left in place: " + ", ".join(result.unreadable)
    return text


def cmd_replay(as_json: bool) -> int:
    try:
        result = cost_ledger.replay_spool()
    except OSError as exc:
        print(f"vnx costs: replay failed, the spool stays: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(asdict(result)) if as_json else _render(result))
    return EXIT_PENDING if result.pending else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.verb == "replay":
        return cmd_replay(args.json)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
