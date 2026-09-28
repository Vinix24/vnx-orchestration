#!/usr/bin/env python3
"""Collapse duplicate success_patterns / antipatterns and add the natural key.

Usage:
    python3 scripts/migrate_pattern_natural_key.py --db <path>            # dry run
    python3 scripts/migrate_pattern_natural_key.py --db <path> --apply    # write
    ... --report <file.json>                                              # also save the report

The DB path is always explicit: this script never resolves the live store
itself. Run it on a copy first; running it on the real DB is an operator
decision. Mechanism and merge rules: ``scripts/lib/pattern_natural_key.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "lib"


def main(argv=None) -> int:
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    import pattern_natural_key

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="quality_intelligence.db to migrate")
    parser.add_argument("--apply", action="store_true", help="write; without it the run is read-only")
    parser.add_argument("--report", type=Path, help="write the JSON report to this file as well")
    args = parser.parse_args(argv)

    try:
        report = pattern_natural_key.run(args.db, apply=args.apply)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"migrate_pattern_natural_key: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        tmp = args.report.with_name(args.report.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
