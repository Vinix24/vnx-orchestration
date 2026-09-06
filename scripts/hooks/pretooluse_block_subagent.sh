#!/usr/bin/env bash
# PreToolUse Hook: Block subagents (Task tool), with an operator override.
#
# Purpose: Enforce T0 governance rule — all work must route through governed
#          VNX lanes (tmux-spawn / provider_dispatch), never via subagents.
#          Subagents bypass the governance receipt trail; governed lanes
#          always emit a receipt.
#
# OI-1643: this hook used to emit the deprecated flat
# {"decision":"block","reason":"..."} form, which is the PostToolUse/Stop/
# UserPromptSubmit contract, not PreToolUse. Claude Code's PreToolUse event
# reads hookSpecificOutput.permissionDecision instead, so that output was
# silently ignored and every Task call was allowed through — the rule
# existed on paper only. Fixed by delegating the whole decision (including
# the operator-override marker + audit trail) to pretooluse_subagent_guard.py.
#
# Claude Code hook contract (2.1+), PreToolUse:
#   stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
#   stdout : {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#             "permissionDecision":"deny","permissionDecisionReason":"..."}}
#            to deny; "allow" to allow; empty stdout for non-Task tools.
#   exit   : 0 always — decision is communicated via JSON output
#
# Default is deny: no marker, an unreadable marker, an expired marker, or a
# marker without a reason all deny. A crash of the Python core also denies
# (fail-closed) — a broken enforcement mechanism must never silently degrade
# to "allow everything", which is the exact defect this hook replaces.
#
# Token budget: ~40 tokens/call for non-Task tools (fast-path exit below);
# a Task call spawns python3 once to consult the marker/audit state.

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="${HOOK_DIR}/pretooluse_subagent_guard.py"

# ── Read hook payload ─────────────────────────────────────────────────────────
INPUT="$(cat)"

# ── Fast path: only inspect Task tool calls ───────────────────────────────────
# settings.json matcher="Task" already filters, but defense-in-depth here
# avoids spawning python3 for other tools if this hook is ever wired broader.
TOOL_NAME=""
if command -v jq >/dev/null 2>&1; then
  TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || echo "")"
fi

if [[ -n "$TOOL_NAME" && "$TOOL_NAME" != "Task" ]]; then
  exit 0
fi

# ── Guard: core script must exist — fail CLOSED (deny), not open ────────────
if [[ ! -f "$CORE" ]]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Subagent guard core ontbreekt (%s) — standaard geblokkeerd (fail-closed)."}}\n' "$CORE"
  exit 0
fi

# ── Delegate the full decision to the Python core ────────────────────────────
OUTPUT="$(printf '%s' "$INPUT" | python3 "$CORE" 2>/dev/null)"
RC=$?

if [[ $RC -ne 0 ]]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Subagent guard core faalde (exit %s) — standaard geblokkeerd (fail-closed). Zie scripts/subagents_allow.py."}}\n' "$RC"
  exit 0
fi

# Empty output means the core decided "not a Task call" (allow silently).
if [[ -n "$OUTPUT" ]]; then
  printf '%s\n' "$OUTPUT"
fi

# ── Tool-call signal aggregation (receipt-quality PR-B2, additive) ───────────
# Best-effort record for later receipt-time aggregation
# (scripts/lib/toolcall_signals.py). No-op unless this is a dispatch that
# exports VNX_TMUX_SIGNAL_DIR (same scoping as tmux_signal_stop_receipt.sh).
# Writes to /dev/null so nothing leaks into stdout (Claude Code treats hook
# stdout as the decision payload).
if [[ -n "${VNX_TMUX_SIGNAL_DIR:-}" ]]; then
  _BLOCKED=0
  case "$OUTPUT" in
    *'"permissionDecision": "deny"'*) _BLOCKED=1 ;;
  esac
  printf '%s' "$INPUT" | python3 "${HOOK_DIR}/../lib/toolcall_signals.py" \
    --signal-dir "$VNX_TMUX_SIGNAL_DIR" --blocked "$_BLOCKED" >/dev/null 2>&1 || true
fi

# Exit 0 always — allow/deny is communicated via JSON stdout, not exit code
exit 0
