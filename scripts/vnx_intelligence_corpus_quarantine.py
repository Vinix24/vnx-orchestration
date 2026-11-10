#!/usr/bin/env python3
"""Measure intelligence stores, quarantine their corpus without changing the source, and restore it."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

_LIB_DIR = Path(__file__).resolve().parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from project_root import resolve_project_root
from vnx_paths import resolve_state_dir
from intelligence_corpus_quarantine.audit import AfterCommitError
from intelligence_corpus_quarantine.baseline import measure_baseline
from intelligence_corpus_quarantine.contract import write_manifest
from intelligence_corpus_quarantine.snapshot import reuse_snapshot
from intelligence_corpus_quarantine.store import quarantine, restore
from intelligence_corpus_quarantine.verify import verify


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
    back = commands.add_parser("restore")
    back.add_argument("--db", type=Path, required=True)
    back.add_argument("--quarantine", type=Path, required=True)
    back.add_argument("--allow-existing", action="store_true")
    proof = commands.add_parser("verify")
    proof.add_argument("--db", type=Path, required=True)
    proof.add_argument("--manifest", type=Path, required=True)
    proof.add_argument("--quarantine", type=Path, required=True, help="the sealed artifact directory")
    proof.add_argument("--human", action="store_true", help="key: value lines instead of JSON")
    for command in (store, back, proof):
        command.add_argument("--state-dir", type=Path, help="audit dir (default: the resolved VNX_STATE_DIR)")
    args = parser.parse_args(argv)
    if args.command in ("baseline", "quarantine") and not args.out.is_dir():
        parser.error(f"output directory does not exist: {args.out}")
    try:
        resolve_project_root(__file__)
        if args.command == "verify":
            verdict = verify(args.db, args.manifest, args.quarantine, state_dir=args.state_dir or resolve_state_dir())
            print("\n".join(f"{k}: {v}" for k, v in verdict.items()) if args.human
                  else json.dumps(verdict, sort_keys=True))
            return 0
        if args.command == "quarantine":
            print(quarantine(args.db, args.out, state_dir=args.state_dir or resolve_state_dir()))
            return 0
        if args.command == "restore":
            print(json.dumps(restore(args.db, args.quarantine, state_dir=args.state_dir or resolve_state_dir(),
                                     allow_existing=args.allow_existing), sort_keys=True))
            return 0
        target = args.out / "manifest.json"
        if args.db.expanduser().resolve() in (target.resolve(), target.with_suffix(".json.tmp").resolve()):
            raise ValueError("manifest output would overwrite the database")
        for path in (args.snapshot, args.reuse_snapshot):
            if path is not None and path.expanduser().resolve() == target.resolve():
                raise ValueError("manifest output would overwrite the snapshot")
        reused = reuse_snapshot(args.reuse_snapshot, target) if args.reuse_snapshot is not None else None
        write_manifest(target, measure_baseline(args.db, snapshot=args.snapshot, reused_snapshot=reused))
    except AfterCommitError as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 4
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 3 if isinstance(exc, ValueError) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
