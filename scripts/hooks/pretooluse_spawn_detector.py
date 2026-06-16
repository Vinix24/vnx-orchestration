#!/usr/bin/env python3
"""PreToolUse spawn detector — subprocess_dispatch governance enforcement.

Reads the Claude Code PreToolUse hook JSON payload from stdin.
Outputs "allow" or "block" (shell trims the trailing newline).

PR-9c model — shell-aware, per-segment, tokenized argv classification
─────────────────────────────────────────────────────────────────────
The raw-string regex hard-block of PR-9/9b leaked against shell quoting
(`cl'a'ude -p`, `claude "-p"`, `codex "exec"`), masked a whole command on a
benign first segment (`kimi --version && kimi --print x`), and produced
arg-position false-positives (`git log --grep claude -p`). PR-9c replaces the
regex hard-block with a tokenizer:

  1. Split the command into SEGMENTS on the shell operators ; && || | & and
     newlines (quote-aware: operators inside quotes do not split). Each segment
     is classified on its own — this alone kills the kimi allow-mask.
  2. Tokenize each segment with a POSIX shlex lexer (punctuation_chars=True so
     redirects like `<<<` split off cleanly). shlex dequotes cl'a'ude→claude,
     claude "-p"→claude -p, codex "exec"→codex exec, and resolves escapes.
  3. HARD-BLOCK on the basename of argv[0] (strips paths: /usr/bin/claude →
     claude) plus an exact blocked-flag token in argv[1:]. This is airtight
     against quoting AND immune to arg-position false-positives — a provider
     name that appears as an ARGUMENT has a different argv[0].
  4. Recurse into `bash -c` / `sh -c` strings and transparent command-prefix
     runners (nohup/env/setsid/stdbuf), bounded to depth 3.
  5. Shadow-detect lane invocations on the tokenized argv (allow+log by
     default; block when VNX_HOOK_ENFORCE=1). A lane name as a non-executable
     argument (`echo provider_dispatch.py`) does not match.
  6. Fail-open absolutely: any stdin → exactly one allow/block line, never a
     traceback. An unbalanced-quote segment falls back to a best-effort legacy
     regex scan, which still BLOCKS a raw provider spawn — no blanket allow.

Hard-blocked (always, regardless of VNX_HOOK_ENFORCE):
  claude:  -p / --print / --dangerously-skip-permissions
  kimi:    --print / -p
  codex:   exec <subcommand>

Shadow-detected (allow + log when VNX_HOOK_ENFORCE unset/0; block when =1):
  Direct lane-script invocation: tmux_interactive_dispatch.py,
    subprocess_dispatch.py, provider_dispatch.py, dispatch_cli.py
  python[3] -m <lane_module>
  python[3] -c "..." importing a lane module
  importlib.import_module("<lane_module>")

Telemetry: every block AND every shadow detection → one JSON line appended to
  <VNX_DATA_DIR>/events/hook_blocks.ndjson. Telemetry errors never block.

Exit code is always 0. Decision is stdout text.

Known limitation: a renamed copy of a lane script (e.g. `python /tmp/pd.py`)
cannot be detected by static command inspection — no lane name appears in the
command. Likewise a non-listed command-prefix runner that takes positional
arguments (e.g. `timeout 5 claude -p`) is not unwrapped. The in-process
ExecutionPermit (require_permit) is the real backstop for those vectors; this
hook is the first layer, not the only one.

Live-proven gap (2026-06-09): a raw `kimi --print` invocation bypassed receipts
the same way `claude -p` did. This detector covers all three provider CLIs.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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


# ── Configuration ──────────────────────────────────────────────────────────────
MAX_RECURSION_DEPTH = 3

# Hard-block flag tables — exact token match against the dequoted argv[1:].
_CLAUDE_BLOCKED_FLAGS = frozenset({"-p", "--print", "--dangerously-skip-permissions"})
_KIMI_BLOCKED_FLAGS = frozenset({"-p", "--print"})

# Shell wrappers whose `-c <string>` argument is itself a command to classify.
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

# Transparent command-prefix runners: argv[0] launches the real command which
# follows after the runner's own options/assignments. nohup/setsid/stdbuf take
# only dash-options; env additionally takes NAME=VALUE assignments. Runners with
# positional arguments (timeout/nice) are intentionally excluded — see the
# module "Known limitation"; the ExecutionPermit is the backstop for those.
_PREFIX_RUNNERS = frozenset({"nohup", "setsid", "stdbuf", "env"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Lane/provider module names — the only permitted entry point post-PR-12 is
# `vnx dispatch`. Direct invocation of these bypasses governance receipts.
_LANE_MODULE_NAMES = frozenset({
    "tmux_interactive_dispatch",
    "subprocess_dispatch",
    "provider_dispatch",
    "dispatch_cli",
})
# Longest-first alternation so a shorter name never shadows a longer one.
_LANE_MODS_RE = r"(?:" + r"|".join(
    sorted(_LANE_MODULE_NAMES, key=len, reverse=True)
) + r")"

_PYTHON_RE = re.compile(r"python[0-9.]*$")

# Operator/redirect tokens produced by the punctuation-aware lexer
# (subshell parens, redirects, any quoted-away pipe/amp remnant).
_OP_CHARS = frozenset("();<>|&")

# Lane-import detection inside a `python -c` code string. The negative
# lookbehind blocks a string-literal mention (`print('import provider_dispatch')`)
# while still catching a real statement-position import.
_LANE_IMPORT_PATTERN = re.compile(
    r"(?<!\(')(?<!\(\")(?:import\s+" + _LANE_MODS_RE
    + r"|from\s+" + _LANE_MODS_RE + r"\s+import)"
)
# importlib.import_module("<lane_module>") — dynamic-import form.
_IMPORTLIB_PATTERN = re.compile(r"import_module\([\"']" + _LANE_MODS_RE)


# ── Legacy regex fallback (unbalanced-quote segments only) ─────────────────────
# Used ONLY when shlex cannot tokenize a segment (unbalanced quotes). It must
# still BLOCK a raw provider spawn — never blanket-allow malformed input.
_TOKEN_BOUNDARY = r"(?:^|[\s;&|()\x60$'\"])"
_CMD_SUFFIX = r"(?=\s|$|[\"';\x60\\<>&])"
_EMPTY_QUOTES_RE = re.compile(r'""' + r"|''")
_BACKSLASH_LETTER_RE = re.compile(r"\\([A-Za-z])")

_CLAUDE_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"claude" + _CMD_SUFFIX)
_CLAUDE_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)-p(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)--print(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)--dangerously-skip-permissions(?:\s|$|[<>&])"),
]
_KIMI_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"kimi" + _CMD_SUFFIX)
_KIMI_FLAG_PATTERNS = [
    re.compile(r"(?:^|\s)--print(?:\s|$|[<>&])"),
    re.compile(r"(?:^|\s)-p(?:\s|$|[<>&])"),
]
_KIMI_ALLOWED_PATTERN = re.compile(
    r"(?:^|\s)kimi\s+(?:login|--version|-v|--help|-h)(?:\s|$)"
)
_CODEX_TOKEN_PATTERN = re.compile(_TOKEN_BOUNDARY + r"codex" + _CMD_SUFFIX)
_CODEX_EXEC_PATTERN = re.compile(r"(?:^|\s)codex\s+exec(?:\s|$|[<>&])")

_EXEC_BOUNDARY = r"(?:^|[;|&(]\s*|\|\|\s*|&&\s*|\x60\s*)"
_LANE_SCRIPT_PATTERN = re.compile(
    r"(?:" + _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*(?:\S+/)?"
    + _LANE_MODS_RE + r"\.py(?:\s|$|[\"';\x60<>&])"
    r"|" + _EXEC_BOUNDARY + r"(?:/?(?:[^\s/]+/)*)?"
    + _LANE_MODS_RE + r"\.py(?:\s|$|[\"';\x60<>&])"
    r")"
)
_PYTHON_M_LANE_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-m\s*" + _LANE_MODS_RE + r"(?:\s|$)"
)
_PYTHON_C_PATTERN = re.compile(
    _TOKEN_BOUNDARY + r"python3?\s+(?:[^\s]+\s+)*-c\s*"
)


def _normalize_command(cmd: str) -> str:
    """Collapse simple obfuscation for the legacy fallback scan."""
    out = _EMPTY_QUOTES_RE.sub("", cmd)
    out = _BACKSLASH_LETTER_RE.sub(r"\1", out)
    out = re.sub(r"(?<!\w)(?:[./][^\s]*/)", " ", out)
    return out


def _detect_shadow_legacy(cmd: str) -> str | None:
    """Shadow rule name via the legacy regex patterns (fallback path)."""
    if _LANE_SCRIPT_PATTERN.search(cmd):
        return "lane_script_direct"
    if _PYTHON_M_LANE_PATTERN.search(cmd):
        return "python_m_lane_module"
    if _PYTHON_C_PATTERN.search(cmd) and _LANE_IMPORT_PATTERN.search(cmd):
        return "python_c_lane_import"
    if _IMPORTLIB_PATTERN.search(cmd):
        return "importlib_lane_import"
    return None


def _legacy_scan_segment(segment: str, enforce_mode: bool) -> tuple[str, str | None, str]:
    """Best-effort scan for a segment shlex could not tokenize. Blocks a raw
    provider spawn; never blanket-allows malformed input."""
    norm = _normalize_command(segment)
    if _CLAUDE_TOKEN_PATTERN.search(norm):
        for pat in _CLAUDE_FLAG_PATTERNS:
            if pat.search(norm):
                return "block", "claude_raw_cli", "block"
    if _KIMI_TOKEN_PATTERN.search(norm) and not _KIMI_ALLOWED_PATTERN.search(norm):
        for pat in _KIMI_FLAG_PATTERNS:
            if pat.search(norm):
                return "block", "kimi_raw_cli", "block"
    if _CODEX_TOKEN_PATTERN.search(norm) and _CODEX_EXEC_PATTERN.search(norm):
        return "block", "codex_exec_cli", "block"
    shadow_rule = _detect_shadow_legacy(segment)
    if shadow_rule:
        if enforce_mode:
            return "block", shadow_rule, "block"
        return "allow", shadow_rule, "shadow"
    return "allow", None, "allow"


# ── Tokenized classification ───────────────────────────────────────────────────

def _split_segments(command: str) -> list[str]:
    """Quote-aware split into top-level segments on ; && || | & and newlines.

    Operators inside single/double quotes (or backslash-escaped) do not split,
    so a `bash -c "claude -p; echo"` string stays whole for recursion.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            # backslash escapes the next char only inside double quotes
            if quote == '"' and ch == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in (";", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch in ("&", "|"):
            segments.append("".join(buf))
            buf = []
            i += 2 if (i + 1 < n and command[i + 1] == ch) else 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s for s in segments if s.strip()]


def _tokenize(text: str) -> list[str]:
    """POSIX shlex tokens with shell operators split off as their own tokens.

    Raises ValueError on unbalanced quotes (handled by the caller via the
    legacy fallback). punctuation_chars=True keeps redirects (`<<<`, `>`) from
    gluing onto an adjacent flag, so `claude -p<<<x` → ['claude','-p','<<<','x'].
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_operator_token(tok: str) -> bool:
    """True for a token made up entirely of shell operator/redirect chars."""
    return bool(tok) and all(c in _OP_CHARS for c in tok)


def _value_for_flag(rest: list[str], flag: str) -> str | None:
    """Value passed to a flag, handling both 'flag value' and 'flagvalue' forms
    (e.g. '-m provider_dispatch' and '-mprovider_dispatch')."""
    for i, tok in enumerate(rest):
        if tok == flag:
            return rest[i + 1] if i + 1 < len(rest) else None
        if tok.startswith(flag) and len(tok) > len(flag):
            return tok[len(flag):]
    return None


def _is_lane_script(basename: str) -> bool:
    """True if a file basename is a lane module .py (provider_dispatch.py, …)."""
    return basename.endswith(".py") and basename[:-3] in _LANE_MODULE_NAMES


def _strip_prefix_runner(argv: list[str]) -> list[str] | None:
    """Inner command argv if argv[0] is a transparent prefix runner, else None.

    Drops the runner plus its leading dash-options and NAME=VALUE assignments;
    returns the remaining argv (the real command), or None if nothing remains.
    """
    if os.path.basename(argv[0]) not in _PREFIX_RUNNERS:
        return None
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok.startswith("-") or _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        break
    return argv[i:] if i < n else None


def _detect_shadow_argv(argv: list[str]) -> str | None:
    """Shadow rule name for a tokenized argv, else None."""
    exe = os.path.basename(argv[0])
    # Direct lane-script invocation: argv[0] IS the lane .py.
    if _is_lane_script(exe):
        return "lane_script_direct"
    # python / python3 / python3.x forms.
    if _PYTHON_RE.match(exe):
        rest = argv[1:]
        # python <path>/lane.py  (the lane .py is the script being executed)
        for tok in rest:
            if not tok.startswith("-") and _is_lane_script(os.path.basename(tok)):
                return "lane_script_direct"
        # python -m <lane_module>
        m_target = _value_for_flag(rest, "-m")
        if m_target and m_target in _LANE_MODULE_NAMES:
            return "python_m_lane_module"
        # python -c "<code importing a lane>"
        code = _value_for_flag(rest, "-c")
        if code:
            if _IMPORTLIB_PATTERN.search(code):
                return "importlib_lane_import"
            if _LANE_IMPORT_PATTERN.search(code):
                return "python_c_lane_import"
    return None


def _classify_argv(argv: list[str], enforce_mode: bool, depth: int) -> tuple[str, str | None, str]:
    """Classify a single tokenized command (one shell segment)."""
    if not argv:
        return "allow", None, "allow"
    exe = os.path.basename(argv[0])
    rest = argv[1:]

    # Hard-block — always block, VNX_HOOK_ENFORCE irrelevant.
    if exe == "claude" and any(a in _CLAUDE_BLOCKED_FLAGS for a in rest):
        return "block", "claude_raw_cli", "block"
    if exe == "kimi" and any(a in _KIMI_BLOCKED_FLAGS for a in rest):
        return "block", "kimi_raw_cli", "block"
    if exe == "codex" and "exec" in rest:
        return "block", "codex_exec_cli", "block"

    # Transparent prefix runner (nohup/env/setsid/stdbuf) → classify inner cmd.
    if depth < MAX_RECURSION_DEPTH:
        inner = _strip_prefix_runner(argv)
        if inner is not None:
            return _classify_argv(inner, enforce_mode, depth + 1)

    # Shell wrapper -c "<command>" → recurse on the dequoted command string.
    if exe in _SHELL_WRAPPERS and depth < MAX_RECURSION_DEPTH:
        inner_code = _value_for_flag(rest, "-c")
        if inner_code:
            sub = _classify_command(inner_code, enforce_mode, depth + 1)
            if sub[2] != "allow":
                return sub

    # Shadow evasion (respects VNX_HOOK_ENFORCE).
    shadow_rule = _detect_shadow_argv(argv)
    if shadow_rule:
        if enforce_mode:
            return "block", shadow_rule, "block"
        return "allow", shadow_rule, "shadow"

    return "allow", None, "allow"


def _classify_segment(segment: str, enforce_mode: bool, depth: int) -> tuple[str, str | None, str]:
    """Tokenize and classify one segment; fall back to legacy scan on bad quotes."""
    seg = segment.strip()
    if not seg:
        return "allow", None, "allow"
    try:
        tokens = _tokenize(seg)
    except ValueError:
        return _legacy_scan_segment(seg, enforce_mode)
    argv = [t for t in tokens if not _is_operator_token(t)]
    return _classify_argv(argv, enforce_mode, depth)


_SEV_RANK = {"allow": 0, "shadow": 1, "block": 2}


def _classify_command(command: str, enforce_mode: bool, depth: int = 0) -> tuple[str, str | None, str]:
    """Split a command into segments and return the strongest decision.

    Block beats shadow beats allow; the first block short-circuits.
    """
    if not command or not command.strip():
        return "allow", None, "allow"
    try:
        segments = _split_segments(command)
    except Exception:  # noqa: BLE001
        segments = [command]
    if not segments:
        return "allow", None, "allow"
    best: tuple[str, str | None, str] = ("allow", None, "allow")
    for seg in segments:
        dec = _classify_segment(seg, enforce_mode, depth)
        if dec[2] == "block":
            return dec
        if _SEV_RANK[dec[2]] > _SEV_RANK[best[2]]:
            best = dec
    return best


def classify(cmd: str) -> str:
    """Return 'allow' or 'block'. Reads VNX_HOOK_ENFORCE from environment."""
    enforce_mode = os.environ.get("VNX_HOOK_ENFORCE", "0") == "1"
    try:
        decision, _, _ = _classify_command(cmd or "", enforce_mode)
        return decision
    except Exception:  # noqa: BLE001
        return "allow"  # absolute fail-open


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
        try:
            decision, matched_rule, severity = _classify_command(cmd, enforce_mode)
        except Exception:  # noqa: BLE001
            sys.stdout.write("allow\n")
            return

        if matched_rule is not None:
            mode = "enforce" if enforce_mode else "shadow"
            _append_telemetry(cmd, matched_rule, severity, mode)

        sys.stdout.write(decision + "\n")
    except Exception:  # noqa: BLE001
        # Absolute fail-open: any unexpected error → allow, never crash the hook
        sys.stdout.write("allow\n")


if __name__ == "__main__":
    main()
