#!/usr/bin/env python3
"""PreToolUse spawn detector — subprocess_dispatch governance enforcement.

Reads the Claude Code PreToolUse hook JSON payload from stdin.
Outputs "allow" or "block" (no newline stripping needed; shell trims).

Hard-blocked (always, regardless of VNX_HOOK_ENFORCE):
  claude:  -p / --print / --dangerously-skip-permissions
  kimi:    --print / -p
  codex:   exec <args>

Shadow-detected (allow + log when VNX_HOOK_ENFORCE unset/0; block when =1):
  Direct lane-script invocation: tmux_interactive_dispatch.py,
    subprocess_dispatch.py, provider_dispatch.py, dispatch_cli.py
  python[3] -m <lane_module>
  python[3] -c "..." containing an import of a lane module

Allowed (no rule match):
  claude --version / claude --help / claude (interactive)
  kimi --version / kimi login / kimi (no prompt-executing flags)
  codex --version / codex --help (benign read-only)
  Any command that doesn't match a block or shadow pattern.

VNX_HOOK_ENFORCE: unset/0 = shadow rules log-and-allow; 1 = shadow rules block.
  Hard-block rules ALWAYS block regardless of this flag.

Telemetry: every block AND every shadow detection → one JSON line appended to
  <VNX_DATA_DIR>/events/hook_blocks.ndjson. Telemetry errors never block.

Exit code is always 0. Decision is stdout text.

Live-proven gap (2026-06-09): a raw `kimi --print` invocation bypassed receipts
the same way `claude -p` did. This detector now covers all three provider CLIs.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Make scripts/lib importable for project_root ───────────────────────────────
_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

try:
    import project_root
except ImportError:
    project_root = None  # type: ignore[assignment]

# ── Token boundary helpers ─────────────────────────────────────────────────────
# Match the CLI binary name when preceded by string-start, whitespace, or shell
# operators (&, ;, |, (, ), backtick via \x60, $, ', ").
# Covers: 'kimi ...', 'nohup kimi ...', '&& codex ...', 'bash -c "kimi ..."', etc.
_TOKEN_BOUNDARY = r"(?:^|[\s;&|()\x60$'\"])"
_CMD_SUFFIX = r"(?=\s|$|[\"';\x60\\])"

# ── Hard-block patterns — claude ───────────────────────────────────────────────
CLAUDE_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"claude" + _CMD_SUFFIX)
CLAUDE_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)-p(?:\s|$)"),
    re.compile(r"(?:^|\s)--print(?:\s|$)"),
    re.compile(r"(?:^|\s)--dangerously-skip-permissions(?:\s|$)"),
]

# ── Hard-block patterns — kimi ─────────────────────────────────────────────────
KIMI_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"kimi" + _CMD_SUFFIX)
KIMI_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)--print(?:\s|$)"),
    re.compile(r"(?:^|\s)-p(?:\s|$)"),
]
# login, --version, --help: benign / auth — always allowed
KIMI_ALLOWED_PATTERN = re.compile(
    r"(?:^|\s)kimi\s+(?:login|--version|-v|--help|-h)(?:\s|$)"
)

# ── Hard-block patterns — codex ────────────────────────────────────────────────
CODEX_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"codex" + _CMD_SUFFIX)
CODEX_EXEC_PATTERN = re.compile(r"(?:^|\s)codex\s+exec(?:\s|$)")

# ── Shadow evasion patterns (new, PR-9) ────────────────────────────────────────
# Lane/provider module names — the only permitted entry point post-PR-12 is
# `vnx dispatch`. Direct invocation of these scripts bypasses governance.
_LANE_MODS = (
    r"(?:tmux_interactive_dispatch|subprocess_dispatch|provider_dispatch|dispatch_cli)"
)

# Direct lane-script invocation: [/any/path/]lane_module.py at a token boundary.
# Covers: python3 scripts/lib/provider_dispatch.py, ./dispatch_cli.py, etc.
LANE_SCRIPT_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"(?:\S*/)?" + _LANE_MODS + r"\.py(?:\s|$|[\"';\x60])"
)

# python[3] [flags] -m <lane_module>
PYTHON_M_LANE_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-m\s+" + _LANE_MODS + r"(?:\s|$)"
)

# python[3] -c "..." presence (combined with _LANE_IMPORT_PATTERN below)
_PYTHON_C_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-c\s+"
)
# import / from-import of a lane module anywhere in the command string
_LANE_IMPORT_PATTERN = re.compile(
    r"(?:import\s+" + _LANE_MODS + r"|from\s+" + _LANE_MODS + r"\s+import)"
)


def _detect_shadow(cmd: str) -> str | None:
    """Return shadow rule name if an evasion vector is detected, else None."""
    if LANE_SCRIPT_PATTERN.search(cmd):
        return "lane_script_direct"
    if PYTHON_M_LANE_PATTERN.search(cmd):
        return "python_m_lane_module"
    if _PYTHON_C_PATTERN.search(cmd) and _LANE_IMPORT_PATTERN.search(cmd):
        return "python_c_lane_import"
    return None


def _classify_full(cmd: str, enforce_mode: bool) -> tuple[str, str | None, str]:
    """Return (decision, matched_rule, severity).

    decision    : "allow" or "block"
    matched_rule: rule name, or None for plain allow
    severity    : "block", "shadow", or "allow"
    """
    if not cmd:
        return "allow", None, "allow"

    # Hard-block: claude raw CLI (always block, VNX_HOOK_ENFORCE irrelevant)
    if CLAUDE_TOKEN_PATTERN.search(cmd):
        for pat in CLAUDE_BLOCKED_FLAG_PATTERNS:
            if pat.search(cmd):
                return "block", "claude_raw_cli", "block"

    # Hard-block: kimi raw CLI
    if KIMI_TOKEN_PATTERN.search(cmd):
        if not KIMI_ALLOWED_PATTERN.search(cmd):
            for pat in KIMI_BLOCKED_FLAG_PATTERNS:
                if pat.search(cmd):
                    return "block", "kimi_raw_cli", "block"

    # Hard-block: codex exec
    if CODEX_TOKEN_PATTERN.search(cmd):
        if CODEX_EXEC_PATTERN.search(cmd):
            return "block", "codex_exec_cli", "block"

    # Shadow evasion (new rules — respect VNX_HOOK_ENFORCE)
    shadow_rule = _detect_shadow(cmd)
    if shadow_rule:
        if enforce_mode:
            return "block", shadow_rule, "block"
        return "allow", shadow_rule, "shadow"

    return "allow", None, "allow"


def classify(cmd: str) -> str:
    """Return 'allow' or 'block'. Reads VNX_HOOK_ENFORCE from environment."""
    enforce_mode = os.environ.get("VNX_HOOK_ENFORCE", "0") == "1"
    decision, _, _ = _classify_full(cmd, enforce_mode)
    return decision


def _append_telemetry(cmd: str, matched_rule: str, severity: str, mode: str) -> None:
    """Append one JSON event to <data_dir>/events/hook_blocks.ndjson. Fail-open."""
    try:
        if project_root is None:
            return
        data_dir = project_root.resolve_data_dir(__file__)
        events_dir = data_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": cmd[:2000],
            "matched_rule": matched_rule,
            "severity": severity,
            "mode": mode,
        }
        with (events_dir / "hook_blocks.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass  # fail-open: telemetry must never block valid work or crash the hook


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

    enforce_mode = os.environ.get("VNX_HOOK_ENFORCE", "0") == "1"
    decision, matched_rule, severity = _classify_full(cmd, enforce_mode)

    if matched_rule is not None:
        mode = "enforce" if enforce_mode else "shadow"
        _append_telemetry(cmd, matched_rule, severity, mode)

    sys.stdout.write(decision + "\n")


if __name__ == "__main__":
    main()
