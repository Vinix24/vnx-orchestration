#!/usr/bin/env python3
"""vnx slot — host capacity slots. RS-2 ships only ``doctor``; RS-8 adds the rest.

Exit codes follow ADR-062: 0 valid, 78 invalid store, policy or state.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from slot.slot_store import SlotStore


def main(argv: list) -> int:
    if argv[:1] != ["doctor"]:
        print("usage: vnx slot doctor", file=sys.stderr)
        return 2
    code, reasons = SlotStore().doctor()
    for reason in reasons:
        print(reason)
    if code == 0:
        print("slot doctor: ok")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
