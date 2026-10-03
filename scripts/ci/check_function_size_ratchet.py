#!/usr/bin/env python3
"""CI ratchet: a function may not cross or grow past 70 executable lines in a PR.

Usage: check_function_size_ratchet.py --base <ref> [--head <ref>]
Run from a git checkout. Exit 0 clean, 1 violations, 2 usage or git error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from function_size_gate import FUNCTION_SIZE_LIMIT, GitError, check_ratchet


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.exit(2, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Function size ratchet (limit %d)" % FUNCTION_SIZE_LIMIT)
    parser.add_argument("--base", required=True, help="base ref (merge base with head is used)")
    parser.add_argument("--head", default="HEAD", help="head ref (default HEAD)")
    args = parser.parse_args(argv)
    try:
        violations = check_ratchet(Path.cwd(), args.base, args.head)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not violations:
        print(f"function size ratchet: no violations (limit {FUNCTION_SIZE_LIMIT})")
        return 0
    for violation in violations:
        print(violation.render())
    return 1


if __name__ == "__main__":
    sys.exit(main())
