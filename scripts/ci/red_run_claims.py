#!/usr/bin/env python3
"""Placeholder for scripts/ci/red_run_claims.py; the implementation lands after the red run."""

from __future__ import annotations

import argparse
import sys


def parse_claims(body: str) -> list[str]:
    raise NotImplementedError("parse_claims is not implemented yet")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body", nargs="?")
    parser.add_argument("--out")
    parser.parse_args(argv)
    parse_claims("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
