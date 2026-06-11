#!/usr/bin/env python3
"""PreToolUse spawn detector — subprocess_dispatch governance enforcement.

Reads the Claude Code PreToolUse hook JSON payload from stdin.
Outputs "allow" or "block" (no newline stripping needed; shell trims).

Blocked:
  claude:  -p / --print / --dangerously-skip-permissions (raw worker spawn)
  kimi:    --print / -p  (prompt-executing invocation outside governed path)
  codex:   exec <args>   (prompt-executing invocation outside governed path)

Allowed:
  subprocess_dispatch.py / provider_dispatch.py (governed wrappers — always)
  claude --version / claude --help / claude (interactive)
  kimi --version / kimi login / kimi (no prompt-executing flags)
  codex --version / codex --help (benign read-only)

Exit code is always 0. Decision is in stdout text, not exit code.

Live-proven gap (2026-06-09): a raw `kimi --print` invocation bypassed receipts
the same way `claude -p` did. This detector now covers all three provider CLIs.
"""

from __future__ import annotations

import json
import re
import sys


# ── Allowlist patterns ─────────────────────────────────────────────────────────
# These scripts invoke provider CLIs internally via Popen — invisible to this hook.
# Allow any Bash command that explicitly calls one of these governed wrappers.
ALLOWLIST_PATTERN = re.compile(
    r"(subprocess_dispatch|provider_dispatch)\.py"
)

# ── CLI command-token patterns ────────────────────────────────────────────────
# Match the CLI binary name when preceded by string-start, whitespace, or shell
# operators (&, ;, |, (, ), backtick via \x60, $, ', ").
# Covers: 'kimi ...', 'nohup kimi ...', '&& codex ...', 'bash -c "kimi ..."', etc.
_TOKEN_BOUNDARY = r"(?:^|[\s;&|()\x60$'\"])"
_CMD_SUFFIX = r"(?=\s|$|[\"';\x60\\])"

CLAUDE_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"claude" + _CMD_SUFFIX)
KIMI_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"kimi" + _CMD_SUFFIX)
CODEX_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"codex" + _CMD_SUFFIX)

# ── Blocked flag patterns — claude ────────────────────────────────────────────
# -p / --print : non-interactive print mode (worker spawn indicator)
# --dangerously-skip-permissions : headless/background execution indicator
CLAUDE_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)-p(?:\s|$)"),
    re.compile(r"(?:^|\s)--print(?:\s|$)"),
    re.compile(r"(?:^|\s)--dangerously-skip-permissions(?:\s|$)"),
]

# ── Blocked flag patterns — kimi ──────────────────────────────────────────────
# --print / -p : prompt-executing invocations that bypass the receipt trail.
# kimi login, kimi --version, kimi --help, bare `kimi` (interactive) are allowed.
KIMI_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)--print(?:\s|$)"),
    re.compile(r"(?:^|\s)-p(?:\s|$)"),
]

# ── Allowed kimi sub-commands / flags (benign, no prompt execution) ───────────
# login, --version, --help: read-only / auth operations.
KIMI_ALLOWED_PATTERN = re.compile(
    r"(?:^|\s)kimi\s+(?:login|--version|-v|--help|-h)(?:\s|$)"
)

# ── Blocked sub-command patterns — codex ──────────────────────────────────────
# `codex exec` is the prompt-executing form. `codex --version` / `codex --help`
# are benign; bare `codex` (interactive) is allowed.
CODEX_EXEC_PATTERN = re.compile(r"(?:^|\s)codex\s+exec(?:\s|$)")


def _classify_claude(cmd: str) -> str:
    """Return "allow" or "block" for a command containing the claude token."""
    for pattern in CLAUDE_BLOCKED_FLAG_PATTERNS:
        if pattern.search(cmd):
            return "block"
    return "allow"


def _classify_kimi(cmd: str) -> str:
    """Return "allow" or "block" for a command containing the kimi token."""
    # Benign invocations are always allowed regardless of other flags.
    if KIMI_ALLOWED_PATTERN.search(cmd):
        return "allow"
    for pattern in KIMI_BLOCKED_FLAG_PATTERNS:
        if pattern.search(cmd):
            return "block"
    return "allow"


def _classify_codex(cmd: str) -> str:
    """Return "allow" or "block" for a command containing the codex token."""
    if CODEX_EXEC_PATTERN.search(cmd):
        return "block"
    return "allow"


def classify(cmd: str) -> str:
    """Return "allow" or "block" for the given bash command string."""
    if not cmd:
        return "allow"

    # Allowlist: governed dispatch wrappers are always permitted
    if ALLOWLIST_PATTERN.search(cmd):
        return "allow"

    # Check each provider CLI independently; first "block" wins.
    if CLAUDE_TOKEN_PATTERN.search(cmd):
        if _classify_claude(cmd) == "block":
            return "block"

    if KIMI_TOKEN_PATTERN.search(cmd):
        if _classify_kimi(cmd) == "block":
            return "block"

    if CODEX_TOKEN_PATTERN.search(cmd):
        if _classify_codex(cmd) == "block":
            return "block"

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
