#!/usr/bin/env python3
"""PreToolUse spawn detector — subprocess_dispatch governance enforcement.

Reads the Claude Code PreToolUse hook JSON payload from stdin.
Outputs "allow" or "block" (no newline stripping needed; shell trims).

Blocked: 'claude -p', 'claude --print', 'claude --dangerously-skip-permissions'
Allowed: subprocess_dispatch.py, provider_dispatch.py calls; benign claude invocations.

Exit code is always 0. Decision is in stdout text, not exit code.
"""

from __future__ import annotations

import json
import os
import re
import sys


# ── Allowlist patterns ─────────────────────────────────────────────────────────
# These scripts invoke claude internally via Popen — invisible to this hook.
# Allow any Bash command that explicitly calls one of these governed wrappers.
ALLOWLIST_PATTERN = re.compile(
    r"(subprocess_dispatch|provider_dispatch)\.py"
)

# ── Claude-as-command-token pattern ───────────────────────────────────────────
# Match 'claude' when preceded by string-start, whitespace, or shell operators
# (&, ;, |, (, ), backtick-equivalent via \x60, $, ', ").
# Covers: 'claude ...', 'nohup claude ...', '&& claude ...', 'bash -c "claude ..."',
#         pipes, subshell spawns, background jobs, etc.
CLAUDE_TOKEN_PATTERN = re.compile(
    r"(?:^|[\s;&|()\x60$'\"])claude(?=\s|$|[\"';\x60\\])"
)

# ── Blocked flag patterns ──────────────────────────────────────────────────────
# -p / --print : non-interactive print mode (worker spawn indicator)
# --dangerously-skip-permissions : headless/background execution indicator
BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)-p(?:\s|$)"),
    re.compile(r"(?:^|\s)--print(?:\s|$)"),
    re.compile(r"(?:^|\s)--dangerously-skip-permissions(?:\s|$)"),
]


def classify(cmd: str) -> str:
    """Return "allow" or "block" for the given bash command string."""
    if not cmd:
        return "allow"

    # Allowlist: governed dispatch wrappers are always permitted
    if ALLOWLIST_PATTERN.search(cmd):
        return "allow"

    # Must have 'claude' as a command token to be relevant
    if not CLAUDE_TOKEN_PATTERN.search(cmd):
        return "allow"

    # Check for blocked flags — any match is enough to block
    for pattern in BLOCKED_FLAG_PATTERNS:
        if pattern.search(cmd):
            return "block"

    # 'claude' present but no blocked flags → interactive / --version / --help
    return "allow"


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Malformed JSON — fail open to avoid blocking valid work
        sys.stdout.write("allow\n")
        return

    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    decision = classify(cmd)
    sys.stdout.write(decision + "\n")


if __name__ == "__main__":
    main()
