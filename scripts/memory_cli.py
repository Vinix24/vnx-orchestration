#!/usr/bin/env python3
"""vnx memory bind|scan (ADR-078). The operator binds; the scan only observes."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import memory_ledger
from project_root import resolve_project_id, resolve_state_dir


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="vnx memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("bind").add_argument("--dir", required=True)
    args = parser.parse_args(argv)
    state_dir = resolve_state_dir()
    if args.cmd == "scan":
        result = memory_ledger.scan(state_dir)
        if result["status"] == "unbound":
            print(result["status"] + ": " + result["reason"].split(": ", 1)[-1])
            return 3
        print("scan %s: %d line(s) written" % (result["status"], result["written"]))
        return 0
    from content_class import load_boundary
    from state_mutation import emit_state_mutation
    try:
        doc = memory_ledger.bind(state_dir, args.dir, resolve_project_id(),
                                 boundary=load_boundary(), emit=emit_state_mutation)
    except memory_ledger.BindRefused as exc:
        print("bind refused: %s" % exc, file=sys.stderr)
        return 4
    print("bound %d folder(s)" % len(doc["memory_dirs"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
