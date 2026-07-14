#!/usr/bin/env python3
"""CLI for the regression attribution primitive.

Given a check command that fails at `bad_ref` (default HEAD) but is known
to have passed at `good_ref`, finds and prints the introducing commit via
`git bisect`.

Usage:
    python3 scripts/regression_attribution.py --check "pytest tests/foo.py" --good v1.2.0
    python3 scripts/regression_attribution.py --check "./scripts/check.sh" --good abc123 --bad HEAD
    python3 scripts/regression_attribution.py --check "make lint" --good main~50 --json

This is a thin argument-parsing wrapper only; all logic lives in
scripts/lib/regression_attribution.py so it stays independently testable.

BILLING SAFETY: No Anthropic SDK. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from regression_attribution import (  # noqa: E402
    AttributionResult,
    RegressionAttributionError,
    attribute_regression,
)

EXIT_ATTRIBUTED = 0
EXIT_INCONCLUSIVE = 1
EXIT_ERROR = 2


def _print_human(result: AttributionResult) -> None:
    good_label = f"{result.good_ref} ({(result.good_sha or '')[:12]})"
    bad_label = f"{result.bad_ref} ({(result.bad_sha or '')[:12]})"
    print(f"check:   {result.check_cmd}")
    print(f"range:   {good_label} (good) -> {bad_label} (bad)")
    print(f"status:  {result.status}")
    if result.status == "attributed":
        print(f"commit:  {result.commit_sha}")
        print(f"author:  {result.author} <{result.author_email}>")
        print(f"date:    {result.date}")
        print(f"subject: {result.subject}")
        print("files:")
        for changed_file in result.changed_files:
            print(f"  - {changed_file}")
    else:
        print(f"reason:  {result.reason}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", required=True, dest="check_cmd",
        help="shell command to run at each candidate commit (exit 0 = pass, nonzero = fail)",
    )
    parser.add_argument(
        "--good", required=True, dest="good_ref",
        help="ref known to pass --check",
    )
    parser.add_argument(
        "--bad", default="HEAD", dest="bad_ref",
        help="ref known to fail --check (default: HEAD)",
    )
    parser.add_argument(
        "--repo-root", default=None, dest="repo_root",
        help="git repo to operate in (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="emit structured JSON output")
    args = parser.parse_args(argv)

    try:
        result = attribute_regression(
            check_cmd=args.check_cmd,
            good_ref=args.good_ref,
            bad_ref=args.bad_ref,
            repo_root=args.repo_root,
        )
    except RegressionAttributionError as exc:
        if args.json:
            print(json.dumps({"status": "error", "reason": str(exc)}))
        else:
            print(f"regression_attribution: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_human(result)

    return EXIT_ATTRIBUTED if result.status == "attributed" else EXIT_INCONCLUSIVE


if __name__ == "__main__":
    raise SystemExit(main())
