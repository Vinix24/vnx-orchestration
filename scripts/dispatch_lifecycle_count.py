#!/usr/bin/env python3
"""ADR-045 (K-6): the rollback count, read-only.

``v3_in_flight`` (a ``v3`` start without an end line), ``open_intents`` (an intent
without ``published_at``) and ``v3_in_flight_lifecycle_off`` — what the brake
(``ledger_lifecycle`` off) refuses to close (K-5's Open Items). Exit 0 when both
sources were read, 2 when one was not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import dispatch_lifecycle  # noqa: E402
from append_receipt_internals.common import AppendReceiptError  # noqa: E402

EXIT_OK, EXIT_UNREADABLE = 0, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True, help="the store's state dir")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if not args.state_dir.is_dir():
        print(f"ERROR: state dir not found: {args.state_dir}", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        report = dispatch_lifecycle.in_flight_counts(args.state_dir)
    except AppendReceiptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name in ("v3_in_flight", "open_intents", "v3_in_flight_lifecycle_off"):
            print(f"{name}={report[name]}")
        for problem in report["unreadable"]:
            print(f"unreadable: {problem}", file=sys.stderr)
    return EXIT_UNREADABLE if report["unreadable"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
