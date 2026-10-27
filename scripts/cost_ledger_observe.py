#!/usr/bin/env python3
"""cost_ledger_observe.py — ``vnx costs observe``: one ``account_observation`` per provider (K5b).

A balance measurement is not a spend: the records go to ``events/provider_account_observations.ndjson``
beside the cost ledger and no ledger sum reads them; a probe that fails is written as
``unavailable:<reason>``, never skipped. ``--coverage`` also asks OpenRouter whether it lists a key's
generations and which of them the ledger missed (or, when it cannot, the ``total_usage`` delta).

Exit codes: 0 the records were written (an ``unavailable`` one too: it says so), 1 the write failed.

Usage: python3 scripts/cost_ledger_observe.py [--json] [--coverage] [--ledger PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_observations  # noqa: E402

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vnx costs observe", description="Measure each provider's balance.")
    parser.add_argument("--json", action="store_true", help="print the records as one JSON list")
    parser.add_argument("--coverage", action="store_true",
                        help="also report the OpenRouter missed-catch coverage of the observed window")
    parser.add_argument("--ledger", metavar="PATH", help="the ledger whose store receives the records")
    args = parser.parse_args(argv)
    try:
        records = account_observations.observe(ledger_path=Path(args.ledger) if args.ledger else None)
    except OSError as exc:
        print(f"vnx costs observe: the observations could not be written: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(records) if args.json else "\n".join(
        f"{record['provider']:<11}{record['observed_at']}  "
        f"{record.get('unavailable') or json.dumps(record.get('values', {}), sort_keys=True)}"
        for record in records))
    if args.coverage:
        coverage = account_observations.openrouter_coverage(ledger_path=Path(args.ledger) if args.ledger else None)
        print(json.dumps(coverage, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
