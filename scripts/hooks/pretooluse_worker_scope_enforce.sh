#!/usr/bin/env bash
# PreToolUse Hook: Worker-scope enforcement (dispatch 20260724-worker-scope-enforce-hook)
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
#   exit   : 0 always — block/allow is communicated via JSON stdout, never
#            the exit code. A crashing core must never surface as a hook
#            error, so the python invocation is hardened with `|| true`.
#
# Gate: VNX_ENFORCE_WORKER_PERMISSIONS (default OFF). Unset/0 → no-op, every
#       tool call is allowed exactly as if this hook were not registered.
#
# Token budget: ~60 tokens/call — fast-path exits for non-matching tools
# happen inside the Python core (pretooluse_worker_scope_enforce.py).

set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="${HOOK_DIR}/pretooluse_worker_scope_enforce.py"

# Guard: core script must exist — fail open to avoid blocking all tool calls.
if [[ ! -f "$CORE" ]]; then
  exit 0
fi

# Fail open on any core error: a broken hook must never block or error out a
# legitimate tool call. Block/allow is communicated via JSON stdout only.
python3 "$CORE" || true

exit 0
