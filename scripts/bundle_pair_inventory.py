#!/usr/bin/env python3
"""Read-only inventory of broker bundles that can still be consumed or retried."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from dispatch_broker import DispatchBroker
from vnx_paths import resolve_project_data_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_project_data_root(args.project_id)
    rows = DispatchBroker(root / "state", root / "dispatches", intelligence_enabled=False).claimable_bundle_pairs(args.project_id)
    totals = {state: sum(row["pair_state"] == state for row in rows)
              for state in ("whole", "partial", "absent", "error")}
    if args.json:
        print(json.dumps(dict(project_id=args.project_id, bundles=rows, totals=totals), indent=2))
    else:
        for row in rows:
            print(f"{row['dispatch_id']} {row['state']} {row['pair_state']} {row['bundle_path']} {row.get('error', '')}")
        print(json.dumps(totals, sort_keys=True))
    return int(totals["error"] > 0)


if __name__ == "__main__":
    sys.exit(main())
