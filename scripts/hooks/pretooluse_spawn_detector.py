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
  importlib.import_module("<lane_module>")

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

Known limitation: a renamed copy of a lane script (e.g. `python /tmp/pd.py`)
cannot be detected by static command inspection — no lane name appears in the
command. The in-process ExecutionPermit (require_permit) is the real backstop
for that vector; this hook is the first layer, not the only one.

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
# Path-form detection (/usr/bin/claude, ./claude) is handled via _normalize_command()
# before this pattern is applied, so path separators need not be in the boundary.
_TOKEN_BOUNDARY = r"(?:^|[\s;&|()\x60$'\"])"
# CMD_SUFFIX: binary name must be followed by whitespace, EOL, or shell/redirect chars.
# <, >, & added so "claude -p<<<x" (here-string) is detected — the binary token
# "claude" is followed by space before the flag, and the flag by "<" (redirect).
_CMD_SUFFIX = r"(?=\s|$|[\"';\x60\\<>&])"

# ── _normalize_command: collapse evasion obfuscation before hard-block matching ─
# Collapses empty quote pairs (cla""ude → claude, co''dex → codex) and
# stray backslash-escapes before letters (\c\l\a\u\d\e → claude).
# Also strips a leading directory path so /usr/local/bin/claude → claude.
# The raw command string is preserved for telemetry; only normalization is used
# for rule matching.
_EMPTY_QUOTES_RE = re.compile(r'""' + r"|''")
_BACKSLASH_LETTER_RE = re.compile(r'\\([A-Za-z])')
_LEADING_PATH_RE = re.compile(r'(?:^|\s)(?:[./][^\s]*/)?([^\s/]+)')


def _normalize_command(cmd: str) -> str:
    """Return a de-obfuscated form of cmd for hard-block matching.

    Steps applied (in order):
    1. Remove empty quote pairs ('' and "").
    2. Remove backslash-escapes before letters (\\c → c).
    3. Strip leading directory paths from binary names
       (/usr/local/bin/claude → claude, ./codex → codex).
    """
    out = _EMPTY_QUOTES_RE.sub("", cmd)
    out = _BACKSLASH_LETTER_RE.sub(r"\1", out)
    # Strip leading path components from each token that starts with / or ./
    # so /usr/bin/claude becomes (treated as) claude.
    # We do a simple regex replacement: any /path/to/name token → space + name.
    out = re.sub(r'(?<!\w)(?:[./][^\s]*/)', ' ', out)
    return out


# ── Hard-block patterns — claude ───────────────────────────────────────────────
CLAUDE_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"claude" + _CMD_SUFFIX)
CLAUDE_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)-p(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)--print(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)--dangerously-skip-permissions(?:\s|$|[<>&])"),
]

# ── Hard-block patterns — kimi ─────────────────────────────────────────────────
KIMI_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"kimi" + _CMD_SUFFIX)
KIMI_BLOCKED_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)--print(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)-p(?:\s|$|[<>&])"),
]
# login, --version, --help: benign / auth — always allowed
KIMI_ALLOWED_PATTERN = re.compile(
    r"(?:^|\s)kimi\s+(?:login|--version|-v|--help|-h)(?:\s|$)"
)

# ── Hard-block patterns — codex ────────────────────────────────────────────────
CODEX_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"codex" + _CMD_SUFFIX)
CODEX_EXEC_PATTERN = re.compile(r"(?:^|\s)codex\s+exec(?:\s|$|[<>&])")

# ── Shadow evasion patterns (new, PR-9) ────────────────────────────────────────
# Lane/provider module names — the only permitted entry point post-PR-12 is
# `vnx dispatch`. Direct invocation of these scripts bypasses governance.
_LANE_MODS = (
    r"(?:tmux_interactive_dispatch|subprocess_dispatch|provider_dispatch|dispatch_cli)"
)

# Execution-position boundary: string-start or shell operator position.
# A lane .py appearing after echo/cat/grep/git (as an argument) must NOT match.
# We require the lane .py to be an execution target:
#   - preceded by python/python3 (+ optional flags), OR
#   - at command-position: string-start or after ;/&&/||/|/backtick/( optionally
#     via ./ or an absolute path.
# A lane .py appearing as an argument to echo/cat/grep/git/in a comment → no match.
_EXEC_BOUNDARY = r"(?:^|[;|&(]\s*|\|\|\s*|&&\s*|\x60\s*)"

LANE_SCRIPT_PATTERN = re.compile(
    # python[3] [flags] path/lane.py
    r"(?:" + _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*(?:\S+/)?" + _LANE_MODS + r"\.py(?:\s|$|[\"';\x60<>&])"
    # OR lane.py at execution position (command-position token)
    # Covers: ./lane.py, scripts/lib/lane.py, /abs/path/lane.py, bare lane.py
    r"|" + _EXEC_BOUNDARY + r"(?:/?(?:[^\s/]+/)*)?" + _LANE_MODS + r"\.py(?:\s|$|[\"';\x60<>&])"
    r")"
)

# python[3] [flags] -m<lane_module> (accept no-space form: -mprovider_dispatch)
PYTHON_M_LANE_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-m\s*" + _LANE_MODS + r"(?:\s|$)"
)

# python[3] -c<string> (accept no-space form: -c'import ...')
_PYTHON_C_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-c\s*"
)

# import of a lane module that looks like a real statement, NOT a string-literal mention.
# Best-effort: exclude imports immediately preceded by `('` or `("` (nested function arg).
# Handles: python -c "import X", python -c'import X', python3 -c 'from X import ...'
# Residual: a mention like `python -c "print('import X')"` does not match because
# `import` is preceded by `('`, which the negative lookbehind blocks.
# A mention like `python -c "x = 'import X'"` may still match (documented limitation).
_LANE_IMPORT_PATTERN = re.compile(
    r"(?<!\(')(?<!\(\")(?:import\s+" + _LANE_MODS + r"|from\s+" + _LANE_MODS + r"\s+import)"
)

# importlib.import_module("<lane_module>") — dynamic-import form
_IMPORTLIB_PATTERN = re.compile(
    r"import_module\([\"']" + _LANE_MODS
)


def _detect_shadow(cmd: str) -> str | None:
    """Return shadow rule name if an evasion vector is detected, else None."""
    if LANE_SCRIPT_PATTERN.search(cmd):
        return "lane_script_direct"
    if PYTHON_M_LANE_PATTERN.search(cmd):
        return "python_m_lane_module"
    if _PYTHON_C_PATTERN.search(cmd) and _LANE_IMPORT_PATTERN.search(cmd):
        return "python_c_lane_import"
    if _IMPORTLIB_PATTERN.search(cmd):
        return "importlib_lane_import"
    return None


def _classify_full(cmd: str, enforce_mode: bool) -> tuple[str, str | None, str]:
    """Return (decision, matched_rule, severity).

    decision    : "allow" or "block"
    matched_rule: rule name, or None for plain allow
    severity    : "block", "shadow", or "allow"
    """
    if not cmd:
        return "allow", None, "allow"

    # Normalize for hard-block matching (raw cmd kept for telemetry)
    norm = _normalize_command(cmd)

    # Hard-block: claude raw CLI (always block, VNX_HOOK_ENFORCE irrelevant)
    if CLAUDE_TOKEN_PATTERN.search(norm):
        for pat in CLAUDE_BLOCKED_FLAG_PATTERNS:
            if pat.search(norm):
                return "block", "claude_raw_cli", "block"

    # Hard-block: kimi raw CLI
    if KIMI_TOKEN_PATTERN.search(norm):
        if not KIMI_ALLOWED_PATTERN.search(norm):
            for pat in KIMI_BLOCKED_FLAG_PATTERNS:
                if pat.search(norm):
                    return "block", "kimi_raw_cli", "block"

    # Hard-block: codex exec
    if CODEX_TOKEN_PATTERN.search(norm):
        if CODEX_EXEC_PATTERN.search(norm):
            return "block", "codex_exec_cli", "block"

    # Shadow evasion (new rules — respect VNX_HOOK_ENFORCE); use raw cmd
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
    try:
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            sys.stdout.write("allow\n")
            return

        # Guard: data must be a dict
        if not isinstance(data, dict):
            sys.stdout.write("allow\n")
            return

        tool_input = data.get("tool_input") or {}

        # Guard: tool_input must be a dict
        if not isinstance(tool_input, dict):
            sys.stdout.write("allow\n")
            return

        raw_cmd = tool_input.get("command", "")

        # Guard: command must be a string; list commands (e.g. ["claude", "-p"]) → allow
        if not isinstance(raw_cmd, str):
            sys.stdout.write("allow\n")
            return

        cmd = raw_cmd
        enforce_mode = os.environ.get("VNX_HOOK_ENFORCE", "0") == "1"
        decision, matched_rule, severity = _classify_full(cmd, enforce_mode)

        if matched_rule is not None:
            mode = "enforce" if enforce_mode else "shadow"
            _append_telemetry(cmd, matched_rule, severity, mode)

        sys.stdout.write(decision + "\n")
    except Exception:  # noqa: BLE001
        # Absolute fail-open: any unexpected error → allow, never crash the hook
        sys.stdout.write("allow\n")


if __name__ == "__main__":
    main()
