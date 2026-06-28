#!/usr/bin/env bash
#
# Tests for vnx_dispatch_resolve_agent_role — the governed tmux delivery path recovers the agent role
# from the dispatch file's "Role:" header when none was threaded through, so dispatch_metadata stamps a
# role for per-role rework attribution (parity with the subprocess/provider/headless lanes, OI-1107).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [ "$expected" != "$actual" ]; then
    echo "FAIL: $msg (expected='$expected' actual='$actual')"
    exit 1
  fi
}

with_role="$(mktemp)"
printf 'Role: backend-developer\nGate: implementation\n' > "$with_role"
no_header="$(mktemp)"   # empty file, no Role: header

# explicit role always wins, even when the file carries a different header
assert_eq "debugger" "$(vnx_dispatch_resolve_agent_role "debugger" "$with_role")" "explicit role wins over file header"
# no explicit role -> recover from the file's Role: header
assert_eq "backend-developer" "$(vnx_dispatch_resolve_agent_role "" "$with_role")" "empty role recovers from file header"
# no explicit role and no header -> empty (honest: no invented fallback)
assert_eq "" "$(vnx_dispatch_resolve_agent_role "" "$no_header")" "empty role + no header stays empty"
# missing file path -> empty, never errors
assert_eq "" "$(vnx_dispatch_resolve_agent_role "" "/nonexistent/path")" "missing file stays empty"

rm -f "$with_role" "$no_header"
echo "PASS: vnx_dispatch_resolve_agent_role"
