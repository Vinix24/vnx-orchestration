#!/usr/bin/env python3
"""Measure intelligence stores without changing their contents."""
import argparse
import sqlite3
import sys
from pathlib import Path

from lib.project_root import resolve_project_root
from lib.intelligence_corpus_quarantine.baseline import measure_baseline
from lib.intelligence_corpus_quarantine.contract import write_manifest


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"{self.prog}: {message}\n")


def main(argv=None):
    parser = Parser(description=__doc__)
    baseline = parser.add_subparsers(dest="command", required=True).add_parser("baseline")
    baseline.add_argument("--db", type=Path, required=True)
    baseline.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.out.is_dir():
        parser.error(f"output directory does not exist: {args.out}")
    try:
        resolve_project_root(__file__)
        target = args.out / "manifest.json"
        if args.db.expanduser().resolve() in (target.resolve(), target.with_suffix(".json.tmp").resolve()):
            raise ValueError("manifest output would overwrite the database")
        write_manifest(target, measure_baseline(args.db))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 3 if isinstance(exc, ValueError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
