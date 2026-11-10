#!/usr/bin/env python3
"""Measure intelligence stores and quarantine their corpus without changing the source."""
import argparse
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from project_root import resolve_project_root
from intelligence_corpus_quarantine.baseline import measure_baseline
from intelligence_corpus_quarantine.contract import write_manifest
from intelligence_corpus_quarantine.snapshot import reuse_snapshot
from intelligence_corpus_quarantine.store import quarantine


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"{self.prog}: {message}\n")


def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--db", type=Path, required=True)
    baseline.add_argument("--out", type=Path, required=True)
    snapshots = baseline.add_mutually_exclusive_group()
    snapshots.add_argument("--snapshot", type=Path)
    snapshots.add_argument("--reuse-snapshot", type=Path)
    store = commands.add_parser("quarantine")
    store.add_argument("--db", type=Path, required=True)
    store.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.out.is_dir():
        parser.error(f"output directory does not exist: {args.out}")
    try:
        resolve_project_root(__file__)
        if args.command == "quarantine":
            print(quarantine(args.db, args.out))
            return 0
        target = args.out / "manifest.json"
        if args.db.expanduser().resolve() in (target.resolve(), target.with_suffix(".json.tmp").resolve()):
            raise ValueError("manifest output would overwrite the database")
        for path in (args.snapshot, args.reuse_snapshot):
            if path is not None and path.expanduser().resolve() == target.resolve():
                raise ValueError("manifest output would overwrite the snapshot")
        reused = reuse_snapshot(args.reuse_snapshot, target) if args.reuse_snapshot is not None else None
        write_manifest(target, measure_baseline(args.db, snapshot=args.snapshot, reused_snapshot=reused))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 3 if isinstance(exc, ValueError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
