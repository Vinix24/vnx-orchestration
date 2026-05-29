"""CLI for hash-chain operations."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from ndjson_hash_chain import verify_chain, walk_chain


def main():
    parser = argparse.ArgumentParser(description="NDJSON hash-chain audit tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Verify chain integrity")
    p_verify.add_argument("path", type=Path)

    p_walk = sub.add_parser("walk", help="Walk chain and emit hashes")
    p_walk.add_argument("path", type=Path)

    args = parser.parse_args()

    if args.cmd == "verify":
        ok, violations = verify_chain(args.path)
        if ok:
            print(json.dumps({"verified": True, "path": str(args.path)}))
            sys.exit(0)
        else:
            print(json.dumps({"verified": False, "violations": violations[:20]}, indent=2))
            sys.exit(1)
    elif args.cmd == "walk":
        for line_no, entry, hash_ in walk_chain(args.path):
            print(f"{line_no}\t{hash_[:16]}\t{entry.get('event_type', 'plain')}")


if __name__ == "__main__":
    main()
