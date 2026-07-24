#!/usr/bin/env bash
# PreToolUse Hook: Worker-scope enforcement PoC (OI-788 feasibility spike)
#
# Purpose: fine-grained enforcement layer on top of the coarse ADR-012
#          --allowedTools/--disallowedTools launch-time posture. Blocks a
#          Bash call matching a role's bash_deny_patterns, or a Write/Edit/
#          MultiEdit call targeting a path outside the role's file_write_scope.
#          Delegates matching to the existing worker_permissions.py matchers
#          (match_bash_deny / match_file_write_scope) — no reimplementation.
#
# Claude Code hook contract (2.1+):
#   stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
#   stdout : {"decision":"block","reason":"..."} to block, empty to allow
#   exit   : 0 always — decision is communicated via JSON output
#
# Gate: VNX_ENFORCE_WORKER_PERMISSIONS (default OFF). Unset/0 → no-op, every
#       tool call is allowed exactly as if this hook were not registered.
#
# Token budget: ~60 tokens/call — fast-path exits for non-matching tools
# happen inside the Python core (pretooluse_worker_scope_enforce.py).

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="${HOOK_DIR}/pretooluse_worker_scope_enforce.py"

INPUT="$(cat)"

# Guard: core script must exist — fail open to avoid blocking all tool calls.
if [[ ! -f "$CORE" ]]; then
  exit 0
fi

echo "$INPUT" | python3 "$CORE"

# Exit 0 always — block/allow is communicated via JSON stdout, not exit code
exit 0
