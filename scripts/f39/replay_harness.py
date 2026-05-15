#!/usr/bin/env python3
"""F39 Replay Harness — runs headless T0 against scenario fixtures.

Usage:
    # Run single scenario
    python3 scripts/f39/replay_harness.py --scenario tests/f39/scenarios/level1_01_clean_receipt.json

    # Run all level-1 scenarios
    python3 scripts/f39/replay_harness.py --all --level 1

    # Run all level-2 chain scenarios
    python3 scripts/f39/replay_harness.py --all --level 2

    # Run all level-3 edge case scenarios
    python3 scripts/f39/replay_harness.py --all --level 3

    # Run all levels
    python3 scripts/f39/replay_harness.py --all

    # Use haiku for cheaper runs
    python3 scripts/f39/replay_harness.py --all --level 1 --model haiku

    # Dry-run: print context prompt only (no LLM call)
    python3 scripts/f39/replay_harness.py --scenario ... --dry-run
"""

import sys

# When executed as a script, scripts/f39/ is already on sys.path automatically.
# The replay_harness/ package in the same directory takes precedence over this file.
from replay_harness.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
