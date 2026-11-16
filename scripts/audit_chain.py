"""CLI for hash-chain operations."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from ndjson_hash_chain import verify_chain, walk_chain


HEALTHY_STATUSES = ("verified", "verified-segmented")
EXIT_UNKNOWN_STATUS = 3


def _verify_report(path, ok, violations, status):
    """Map a verify_chain() result to (payload, exit code), fail-closed.

    Healthy only on a positive status AND ok is True (exit 0). "unchained"
    means nothing could be checked (exit 2). "broken", or a healthy status
    with ok not True, is a real defect (exit 1, violations capped at 20 plus
    the total). Any other status is unknown, never healthy (exit 3).
    """
    if status in HEALTHY_STATUSES and ok is True:
        return {"verified": True, "status": status, "path": str(path)}, 0
    if status == "unchained":
        # D1: "unchained" means verify_chain() could not check anything — no
        # entry carries prev_hash, so integrity is UNMEASURED, not confirmed.
        # Chaining stays an explicit decision (VNX_CHAIN_RECEIPTS=1).
        return {
            "verified": False,
            "status": "unchained",
            "path": str(path),
            "warning": (
                "Hash-chain verification not possible: no entries carry prev_hash. "
                "Enable chaining by setting VNX_CHAIN_RECEIPTS=1."
            ),
        }, 2
    payload = {
        "verified": False,
        "status": status,
        "path": str(path),
        "violations": violations[:20],
        "violation_count": len(violations),
    }
    if status == "broken" or status in HEALTHY_STATUSES:
        return payload, 1
    payload["error"] = f"unknown verify_chain status: {status!r}"
    return payload, EXIT_UNKNOWN_STATUS


def main():
    parser = argparse.ArgumentParser(description="NDJSON hash-chain audit tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Verify chain integrity")
    p_verify.add_argument("path", type=Path)

    p_walk = sub.add_parser("walk", help="Walk chain and emit hashes")
    p_walk.add_argument("path", type=Path)

    args = parser.parse_args()

    if args.cmd == "verify":
        ok, violations, status = verify_chain(args.path)
        payload, code = _verify_report(args.path, ok, violations, status)
        print(json.dumps(payload, indent=2))
        sys.exit(code)
    elif args.cmd == "walk":
        for line_no, entry, hash_ in walk_chain(args.path):
            print(f"{line_no}\t{hash_[:16]}\t{entry.get('event_type', 'plain')}")


if __name__ == "__main__":
    main()
