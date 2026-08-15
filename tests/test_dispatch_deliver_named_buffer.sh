#!/usr/bin/env bash
# OI-1144: _ddt_send_content / tmux_load_buffer_safe must address a NAMED tmux
# buffer (pid + dispatch_id) and delete it afterwards, so two simultaneous
# deliveries can never cross on tmux's anonymous "most recent buffer" stack.
#
# NOTE: this asserts the ARGV the functions build (a unique -b <name> shared by
# load+paste and removed by delete-buffer), NOT a live two-process tmux race —
# a shell function is sequential, so the crossing can't be reproduced in-process.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS_COUNT=0
FAIL_COUNT=0
pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [ "$expected" = "$actual" ]; then pass "$msg"; else fail "$msg" "expected='$expected' actual='$actual'"; fi
}

TMP_ROOT=$(mktemp -d)
MOCK_ARGV_LOG="$TMP_ROOT/mock_argv"
touch "$MOCK_ARGV_LOG"

# Runtime deps of tmux_load_buffer_safe / _ddt_send_content.
VNX_DISPATCH_MAX_INLINE=50000
VNX_DISPATCH_PAYLOAD_DIR="$TMP_ROOT/payload"

log() { :; }
tmux_retry() { local max_attempts="$1"; shift; "$@"; }
tmux_send_best_effort() { return 0; }

# tmux mock: record the full argv of every call; always succeed.
tmux() {
    echo "$*" >> "$MOCK_ARGV_LOG"
    return 0
}

# shellcheck disable=SC1091
source "$PROJECT_ROOT/scripts/lib/dispatch_deliver.sh"

_buf_name() {
    sed -n 's/.*-b \([^ ]*\).*/\1/p'
}

# ===========================================================================
# Test 1: codex path — one named buffer, same name on load+paste, deleted after
# ===========================================================================
_ddt_send_content "pane1" "codex" "/model" "hello" "dispatch-1144"

assert_eq "1" "$(grep -c '^load-buffer ' "$MOCK_ARGV_LOG")" \
    "D1 (OI-1144): codex path loads exactly once"
assert_eq "1" "$(grep -c '^paste-buffer ' "$MOCK_ARGV_LOG")" \
    "D1 (OI-1144): codex path pastes exactly once"
LOAD_NAME=$(grep '^load-buffer ' "$MOCK_ARGV_LOG" | _buf_name)
PASTE_NAME=$(grep '^paste-buffer ' "$MOCK_ARGV_LOG" | _buf_name)
assert_eq "$LOAD_NAME" "$PASTE_NAME" \
    "D1 (OI-1144): load and paste address the SAME named buffer"
assert_eq "1" "$(grep -c "^delete-buffer -b ${LOAD_NAME}" "$MOCK_ARGV_LOG")" \
    "D1 (OI-1144): buffer is deleted after use"
assert_eq "0" "$(grep -c '^load-buffer -$' "$MOCK_ARGV_LOG")" \
    "D1 (OI-1144): no fallback to the anonymous '-' stack"

# ===========================================================================
# Test 2: non-codex path — same named-buffer discipline after the skill send
# ===========================================================================
: > "$MOCK_ARGV_LOG"
_ddt_send_content "pane2" "claude_code" "/skill" "body" "dispatch-1144"

LOAD_NAME2=$(grep '^load-buffer ' "$MOCK_ARGV_LOG" | _buf_name)
PASTE_NAME2=$(grep '^paste-buffer ' "$MOCK_ARGV_LOG" | _buf_name)
assert_eq "1" "$(grep -c '^load-buffer ' "$MOCK_ARGV_LOG")" \
    "D2 (OI-1144): claude path loads exactly once"
assert_eq "$LOAD_NAME2" "$PASTE_NAME2" \
    "D2 (OI-1144): claude path load+paste share the named buffer"
assert_eq "1" "$(grep -c "^delete-buffer -b ${LOAD_NAME2}" "$MOCK_ARGV_LOG")" \
    "D2 (OI-1144): claude path deletes the buffer after use"

# ===========================================================================
# Test 3: two deliveries with distinct dispatch ids build DISTINCT buffer names
# (argv evidence that simultaneous crossings are structurally impossible)
# ===========================================================================
: > "$MOCK_ARGV_LOG"
_ddt_send_content "paneA" "claude_code" "/skill" "body-A" "dispatch-A"
_ddt_send_content "paneB" "claude_code" "/skill" "body-B" "dispatch-B"

DISTINCT_NAMES=$(grep '^load-buffer ' "$MOCK_ARGV_LOG" | _buf_name | sort -u)
assert_eq "2" "$(printf '%s\n' "$DISTINCT_NAMES" | wc -l | tr -d ' ')" \
    "D3 (OI-1144): two deliveries build two DISTINCT buffer names (argv, not a live race)"

# --- Cleanup ---
rm -rf "$TMP_ROOT"

echo ""
echo "=== dispatch_deliver named-buffer test results: $PASS_COUNT passed, $FAIL_COUNT failed ==="
[ "$FAIL_COUNT" -eq 0 ] || exit 1
