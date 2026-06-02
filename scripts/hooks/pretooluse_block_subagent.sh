#!/usr/bin/env bash
# PreToolUse Hook: Block subagents (Task tool)
#
# Purpose: Enforce T0 governance rule — all work must route through governed
#          VNX lanes (tmux-spawn / provider_dispatch), never via subagents.
#          Subagents bypass the governance receipt trail; governed lanes
#          always emit a receipt.
#
# Claude Code hook contract (2.1+):
#   stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
#   stdout : {"decision":"block","reason":"..."} to block, empty to allow
#   exit   : 0 always — decision is communicated via JSON output
#
# Token budget: ~40 tokens/call — fast-path exits for non-Task tools

set -euo pipefail

# ── Read hook payload ─────────────────────────────────────────────────────────
INPUT="$(cat)"

# ── Extract tool name ─────────────────────────────────────────────────────────
TOOL_NAME=""
if command -v jq >/dev/null 2>&1; then
  TOOL_NAME="$(echo "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"
fi

# ── Only block the Task (subagent) tool ──────────────────────────────────────
if [[ "$TOOL_NAME" != "Task" ]]; then
  exit 0
fi

# ── Emit block decision ──────────────────────────────────────────────────────
printf '{"decision":"block","reason":"Subagents (Task tool) are disabled in this project. Route work through governed VNX lanes: tmux-spawn (scripts/lib/tmux_interactive_dispatch.py) or provider_dispatch.py — these emit receipts. See T0 CLAUDE.md worker-dispatch policy."}\n'

# Exit 0 always — block/allow is communicated via JSON stdout, not exit code
exit 0
