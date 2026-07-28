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

# Resolve hook directory for the tool-call signal recorder (scripts/lib/).
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# ── Tool-call signal aggregation (receipt-quality PR-B2, additive) ───────────
# Every Task call reaching this point was just blocked above. Best-effort
# record for later receipt-time aggregation (scripts/lib/toolcall_signals.py).
# No-op unless this is a dispatch that exports VNX_TMUX_SIGNAL_DIR (same
# scoping as tmux_signal_stop_receipt.sh). Writes to /dev/null so nothing
# leaks into stdout (Claude Code treats hook stdout as the decision payload).
if [[ -n "${VNX_TMUX_SIGNAL_DIR:-}" ]]; then
  printf '%s' "$INPUT" | python3 "${HOOK_DIR}/../lib/toolcall_signals.py" \
    --signal-dir "$VNX_TMUX_SIGNAL_DIR" --blocked 1 >/dev/null 2>&1 || true
fi

# Exit 0 always — block/allow is communicated via JSON stdout, not exit code
exit 0
