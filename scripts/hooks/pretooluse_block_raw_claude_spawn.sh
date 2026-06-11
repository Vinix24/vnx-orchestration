#!/usr/bin/env bash
# PreToolUse Hook: Block raw provider CLI spawns (claude / kimi / codex)
#
# Purpose: Enforce governance receipt trail by blocking direct prompt-executing
#          invocations of provider CLIs. All worker dispatch must go via
#          subprocess_dispatch.py or provider_dispatch.py, which spawn CLIs via
#          Popen and always emit a receipt.
#
# Claude Code hook contract (2.1+):
#   stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
#   stdout : {"decision":"block","reason":"..."} to block, empty to allow
#   exit   : 0 always — decision is communicated via JSON output
#
# Detection is delegated to pretooluse_spawn_detector.py (same directory)
# for reliable cross-platform regex without bash heredoc/quoting issues.
#
# Blocked patterns:
#   claude -p / claude --print               (non-interactive print mode)
#   claude --dangerously-skip-permissions    (headless/background spawn)
#   kimi --print / kimi -p                   (prompt-executing kimi CLI)
#   codex exec <args>                        (prompt-executing codex CLI)
#
# Always allowed:
#   python3 scripts/lib/subprocess_dispatch.py ...  (governed wrapper)
#   python3 scripts/lib/provider_dispatch.py ...    (governed wrapper)
#   claude --version / claude --help / claude       (benign / interactive)
#   kimi --version / kimi login / kimi              (benign / auth)
#   codex --version / codex --help                  (benign read-only)
#
# Token budget: ~80 tokens/call — fast-path exits for non-Bash tools

set -euo pipefail

# Resolve hook directory for sibling Python detector
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECTOR="${HOOK_DIR}/pretooluse_spawn_detector.py"

# ── Read hook payload ─────────────────────────────────────────────────────────
INPUT="$(cat)"

# ── Fast path: only inspect Bash tool calls ───────────────────────────────────
# settings.json matcher="Bash" already filters, but defense-in-depth here.
TOOL_NAME=""
if command -v jq >/dev/null 2>&1; then
  TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"
fi

if [[ "$TOOL_NAME" != "Bash" ]]; then
  exit 0
fi

# ── Guard: detector must exist ───────────────────────────────────────────────
if [[ ! -f "$DETECTOR" ]]; then
  # Detector missing — fail open to avoid blocking all Bash tool calls
  exit 0
fi

# ── Delegate detection to Python helper ───────────────────────────────────────
# Pass full JSON payload via stdin; Python outputs "allow" or "block"
DECISION="$(echo "$INPUT" | python3 "$DETECTOR")"

# ── Emit JSON decision ────────────────────────────────────────────────────────
if [[ "$DECISION" == "block" ]]; then
  printf '{"decision":"block","reason":"Worker-dispatch moet via scripts/lib/subprocess_dispatch.py of provider_dispatch.py (governed, emit receipt). Rauwe claude -p/--dangerously-skip-permissions, kimi --print/-p, en codex exec bypassen de governance receipt-trail. Gebruik: python3 scripts/lib/provider_dispatch.py --provider <claude|kimi|codex> <dispatch_id>"}\n'
fi

# Exit 0 always — block/allow is communicated via JSON stdout, not exit code
exit 0
