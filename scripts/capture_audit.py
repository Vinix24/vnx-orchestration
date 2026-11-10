#!/usr/bin/env python3
"""Capture audit CLI: project-bound sources (capture PR-2).

Subcommands:
  sources  show per source the state (ok, absent, unbound, error) and counts.
           Never prints a name, title, heading or body.

The exit code of ``sources`` is 0 whenever it could report, also when a source
is not ``ok``: the state in the output is the evidence, not the exit code.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import capture_sources as cs
from vnx_paths import project_id_from_state_dir

SOURCES_MONTHS = 2


def _recent_months(now: datetime, count: int) -> List[str]:
    months: List[str] = []
    year, month = now.year, now.month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


def _bin(status: str, count: int, **extra: int) -> Dict[str, object]:
    row: Dict[str, object] = {"state": cs.state_of(status), "count": count}
    row.update(extra)
    return row


def collect_sources(state_dir: Path, now: datetime) -> Dict[str, object]:
    """Per-bin state and counts only. Reasons are fixed texts, never data."""
    binding, status = cs.load_binding(state_dir)
    if binding is None:
        unbound = _bin(status, 0)
        return {"binding": unbound, "open_items": unbound, "memory": unbound, "bucket": unbound}
    items, oi_status, oi_foreign = cs.read_open_items(state_dir)
    notes, mem_status = cs.read_memory_notes(binding)
    entries, bucket_status, foreign = cs.read_bucket_entries(binding, _recent_months(now, SOURCES_MONTHS))
    return {
        "binding": _bin(status, 1),
        "open_items": _bin(oi_status, len(items), foreign_project_skipped=oi_foreign),
        "memory": _bin(mem_status, len(notes)),
        "bucket": _bin(bucket_status, len(entries), foreign_slug_skipped=foreign),
    }


def cmd_sources(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    report = {"project_id": project_id_from_state_dir(state_dir) or None}
    report["sources"] = collect_sources(state_dir, datetime.now(timezone.utc))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for name, row in report["sources"].items():
        extras = " ".join(f"{k}={v}" for k, v in row.items() if k != "state")
        print(f"{name}: {row['state']} {extras}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture audit: project-bound sources")
    sub = parser.add_subparsers(dest="command", required=True)
    sources = sub.add_parser("sources", help="show the state and counts per source")
    sources.add_argument("--state-dir", required=True)
    sources.add_argument("--json", action="store_true")
    sources.set_defaults(func=cmd_sources)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
