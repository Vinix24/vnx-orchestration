#!/usr/bin/env python3
"""The plan of one Profile A shard (pr-ci-onder-zes-minuten C2b), for profile_a_pytest_args.sh.

Plans the files of the collection before the split (without the serial file entries) over N shards with
test_shard_plan.py, writes shard.json and prints this shard's files NUL-delimited. Exit 2, no plan: an unreadable
manifest or source file, a collected path that is not tests/<path>.py, an empty shard.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling scripts, not a package
import check_xdist_parity as cxp
import test_shard_plan as tsp


class Refused(Exception):
    """An input the shard cannot be planned from."""


def read_source(path: str) -> tuple:
    """(source id, XMLs, reason) of a select-source output; reason None means a usable source."""
    if not path:
        return "none", [], "no duration source given (VNX_CI_SHARD_SOURCE is unset)"
    doc = cxp.read_json_object(Path(path)) or {}  # absent or unreadable: junit is None, refused below
    run_id, junit, reason = doc.get("run_id"), doc.get("junit"), doc.get("reason")
    if run_id is None and junit == [] and isinstance(reason, str) and reason:
        return "none", [], reason
    if cxp.whole_number(run_id) is None or reason is not None or not isinstance(junit, list) or not junit \
            or not all(isinstance(j, str) for j in junit):
        raise Refused(f"duration source {path} is not a select-source output")
    return str(run_id), [Path(j) for j in junit], None


def shard_plan(args: argparse.Namespace) -> dict:
    if not 1 <= args.shard <= args.shards:
        raise Refused(f"shard {args.shard} is not in 1..{args.shards}")
    manifest = cxp.load_manifest(Path(args.collected))
    nodes = list(manifest["nodeids"]) + [o["nodeid"] for o in manifest["collection_outcomes"]]
    paths = {n.split("::", 1)[0] for n in nodes}
    bad = sorted(p for p in paths if not (p.startswith("tests/") and p.endswith(".py")) or ".." in p.split("/"))
    if manifest.get("collection_finished") is not True or bad:
        raise Refused(f"the collection before the split did not finish or holds non-test paths: {bad[:5]}")
    files = sorted(paths - {e for e in args.serial_entry if "::" not in e})
    source_id, xmls, reason = read_source(args.source)
    result = tsp.build_result(files, args.shards, xmls, source_id, reason)
    mine = sorted(f for f, k in result["assignment"].items() if k == args.shard)
    if not mine:
        raise Refused(f"shard {args.shard} of {args.shards} has no test file")
    stamp = {k: os.environ.get(f"GITHUB_{k.upper()}") or None for k in ("run_id", "run_attempt")}
    return {"schema": "vnx-ci-shard/1", **stamp, **result, "shard": args.shard, "files": mine}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--collected", required=True, help="collection manifest of the selection before the split")
    parser.add_argument("--source", default="", help="select-source output; empty: the hash split")
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--serial-entry", action="append", default=[], help="an entry of serial_tests.txt")
    parser.add_argument("--output", required=True, help="path of shard.json")
    args = parser.parse_args(argv)
    try:
        doc = shard_plan(args)
        cxp.write_json_atomic(Path(args.output), doc)
    except (Refused, cxp.EvidenceError, ValueError, OSError) as exc:
        print(f"profile_a_shard: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write("".join(f + "\0" for f in doc["files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
