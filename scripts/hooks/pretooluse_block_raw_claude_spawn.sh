#!/usr/bin/env bash
# PreToolUse Hook: Block raw claude worker spawns
#
# Purpose: Enforce governance receipt trail by blocking direct 'claude -p' /
#          '--dangerously-skip-permissions' invocations. All worker dispatch
#          must go via subprocess_dispatch.py or provider_dispatch.py, which
#          spawn claude via Popen and always emit a receipt.
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
#
# Always allowed:
#   python3 scripts/lib/subprocess_dispatch.py ...  (governed wrapper)
#   python3 scripts/lib/provider_dispatch.py ...    (governed wrapper)
#   claude --version / claude --help                (benign read-only)
#   claude (interactive, no blocked flags)
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
  printf '{"decision":"block","reason":"Worker-dispatch moet via scripts/lib/subprocess_dispatch.py of provider_dispatch.py (governed, emit receipt). Rauwe claude -p/--dangerously-skip-permissions bypast de governance receipt-trail. Gebruik: python3 scripts/lib/subprocess_dispatch.py <dispatch_id>"}\n'
fi

# Exit 0 always — block/allow is communicated via JSON stdout, not exit code
exit 0
