#!/usr/bin/env python3
"""vnx memory bind|scan|doctor (ADR-078). The operator binds; the scan only observes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import memory_doctor
import memory_ledger
from project_root import resolve_project_id
from vnx_paths import ensure_env


def _doctor(state_dir: Path, as_json: bool) -> int:
    doc = memory_doctor.run(state_dir)
    if as_json:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        counts: dict = {}
        for p in doc["proposals"]:
            counts[p["kind"]] = counts.get(p["kind"], 0) + 1
        print("doctor %s: %s" % (doc["status"], ", ".join("%s=%d" % kv for kv in sorted(counts.items())) or "no proposals"))
        print("written: %s" % (state_dir / memory_doctor.RESULT_FILE))
    return {"ok": 0, "unbound": 3}.get(doc["status"], 1)


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="vnx memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bind").add_argument("--dir", required=True)
    sub.add_parser("scan")
    sub.add_parser("doctor").add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Same store as build_t0_state.py (ADR-026): VNX_STATE_DIR from vnx_paths, never the repo-local dir.
    state_dir = Path(ensure_env()["VNX_STATE_DIR"])
    if args.cmd == "doctor":
        return _doctor(state_dir, args.json)
    if args.cmd == "scan":
        result = memory_ledger.scan(state_dir)
        if result["status"] == "unbound":
            print("unbound: " + result["reason"].split(": ", 1)[-1])
            return 3
        print("scan %s: %d line(s) written, %d unreadable"
              % (result["status"], result["written"], result["unreadable"]))
        return 0
    from content_class import load_boundary
    from state_mutation import emit_state_mutation
    try:
        doc = memory_ledger.bind(state_dir, args.dir, resolve_project_id(),
                                 boundary=load_boundary(), emit=emit_state_mutation)
    except memory_ledger.BindRefused as exc:
        print("bind refused: %s" % exc, file=sys.stderr)
        return 4
    print("bound %d folder(s) in %s" % (len(doc["memory_dirs"]), state_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
