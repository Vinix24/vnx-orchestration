#!/usr/bin/env python3
"""Append one JSON object from stdin to intelligence_usage.ndjson under <state_dir> (OI-2714).

Exit 2 (message on stderr) when the input is no JSON object or no state dir is given.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from qi_learning_approval import append_usage_event


def main(argv: list) -> int:
    if len(argv) != 2:
        print("usage: append_usage_event.py <state_dir>  (one JSON object on stdin)", file=sys.stderr)
        return 2
    try:
        event = json.loads(sys.stdin.read())
    except ValueError as exc:
        print(f"stdin is no JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(event, dict):
        print("stdin is no JSON object", file=sys.stderr)
        return 2
    state_dir = Path(argv[1])
    state_dir.mkdir(parents=True, exist_ok=True)
    append_usage_event(state_dir, event)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
