#!/usr/bin/env python3
"""Look up the claimed_by field for a terminal in terminal_state.json.

Usage:
    python3 get_terminal_claimed_by.py <terminal_id>

Reads VNX_STATE_DIR from the environment to locate terminal_state.json.
Prints the claimed_by value (string) or an empty string when the terminal
is unclaimed, the state file is absent, or any parse error occurs.

Exit code is always 0 — errors are silent and callers test for empty
output (matching the heredoc behaviour this script replaces).
"""

from __future__ import annotations

import json
import os
import sys


def _get_terminal_claimed_by(terminal: str, state_dir: str) -> str:
    """Return claimed_by for *terminal* from terminal_state.json, or ''."""
    state_file = os.path.join(state_dir, "terminal_state.json")
    try:
        with open(state_file, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return ((d.get("terminals") or {}).get(terminal) or {}).get("claimed_by") or ""
    except Exception:
        return ""


def main() -> None:
    if len(sys.argv) >= 2:
        terminal = sys.argv[1]
    else:
        # Backward-compatibility fallback used by the old heredoc call site.
        terminal = os.environ.get("_VNX_STUCK_TERMINAL", "")

    state_dir = os.environ.get("VNX_STATE_DIR", "")
    print(_get_terminal_claimed_by(terminal, state_dir))


if __name__ == "__main__":
    main()
